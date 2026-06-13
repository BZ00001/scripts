#!/usr/bin/env python3

# ─────────────────────────────────────────────────────────────────────────────
# Credits
# ─────────────────────────────────────────────────────────────────────────────
# Original concept and logic by Drazzilb08
# https://github.com/Drazzilb08/daps
#
# This is a standalone reimplementation of the renameinatorr module from the
# DAPS (Drazzilb's Arr PMM Scripts) project, stripped of the daps framework
# and rewritten to run as a self-contained script with no container dependency.
#
# All credit for the original design goes to Drazzilb08. Any bugs introduced
# here are entirely the fault of the reimplementation.
# ─────────────────────────────────────────────────────────────────────────────

"""
renameinatorr.py – Standalone file & folder renamer for Radarr / Sonarr.

Fetches every item whose file names don't match the configured naming format,
triggers a rename, optionally renames the containing folder, and tags items so
they are skipped on the next run.  When every item has been tagged the tags
are cleared and the cycle starts over.

Supports chunked / batched processing so you can rename a few items per run
rather than hammering the API all at once.

Configuration is read from renameinatorr.yml in the same directory (or the
path supplied with --config).

─────────────────────────────────────────────────────────────────────────────
Setup (one-time)
─────────────────────────────────────────────────────────────────────────────
Create a virtual environment and install dependencies:

  python3 -m venv /path/to/venv
  /path/to/venv/bin/pip install requests pyyaml

For Unraid User Scripts, use the venv interpreter as the shebang or call it
directly:

  /path/to/venv/bin/python3 renameinatorr.py

─────────────────────────────────────────────────────────────────────────────
Usage
─────────────────────────────────────────────────────────────────────────────
  python3 renameinatorr.py                   # normal run
  python3 renameinatorr.py --dry-run         # preview only, no changes made
  python3 renameinatorr.py --debug           # verbose logging
  python3 renameinatorr.py --title "Foo"     # process one item only
  python3 renameinatorr.py --config /other/path/renameinatorr.yml
"""

import argparse
import datetime
import logging
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import yaml

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

# Strips "Season 01/" style prefixes from paths returned by the rename API –
# used only for human-readable output, not for the rename operations themselves.
SEASON_REGEX = re.compile(r"^Season \d+/", re.IGNORECASE)

DEFAULT_BATCH_SIZE = 100

VERSION           = "1.6.0"
GITHUB_RAW_URL    = "https://raw.githubusercontent.com/BZ00001/scripts/main/renameinatorr/renameinatorr.py"
GITHUB_RELEASE_URL = "https://github.com/BZ00001/scripts/tree/main/renameinatorr"

DEFAULT_CONFIG: Dict[str, Any] = {
    "dry_run": False,
    "log_level": "INFO",
    "instances": [],
}

def _parse_version(v: str) -> tuple:
    """Parse a version string into a tuple of ints for comparison."""
    try:
        return tuple(int(x) for x in v.split("."))
    except Exception:
        return (0,)


def check_for_update(logger: logging.Logger) -> Optional[str]:
    """
    Fetch the latest version from GitHub and return it if newer than the
    running version, or None if already up to date or the check failed.
    """
    try:
        resp = requests.get(GITHUB_RAW_URL, timeout=10)
        resp.raise_for_status()
        # Match VERSION = "x.y.z" regardless of alignment whitespace.
        m = re.search(r'^VERSION\s*=\s*["\']([\d.]+)["\']', resp.text, re.MULTILINE)
        if m:
            latest = m.group(1)
            if _parse_version(latest) > _parse_version(VERSION):
                return latest
    except Exception as exc:
        logger.debug("Version check failed: %s", exc)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────


def setup_logging(level: str, log_file: Optional[str] = None) -> logging.Logger:
    """
    Configure logging to stdout and optionally to a rotating log file.

    *log_file* enables a RotatingFileHandler (5 MB per file, 3 backups) so
    users on infrequent schedules keep history beyond the last terminal run.
    """
    numeric = getattr(logging, level.upper(), logging.INFO)
    fmt     = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(numeric)
    root.handlers.clear()

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    if log_file:
        try:
            from logging.handlers import RotatingFileHandler
            file_handler = RotatingFileHandler(
                log_file, maxBytes=5 * 1024 * 1024, backupCount=3
            )
            file_handler.setFormatter(fmt)
            root.addHandler(file_handler)
        except Exception as exc:
            root.warning("Could not open log file %s: %s", log_file, exc)

    return logging.getLogger("renameinatorr")


# ─────────────────────────────────────────────────────────────────────────────
# ARR API client
# ─────────────────────────────────────────────────────────────────────────────


class ArrClient:
    """Minimal Radarr / Sonarr v3 API client."""

    def __init__(self, url: str, api_key: str, instance_type: str, name: str) -> None:
        self.base          = url.rstrip("/")
        self.api_key       = api_key
        self.instance_type = instance_type.lower()   # "radarr" or "sonarr"
        self.name          = name
        self._session      = requests.Session()
        self._session.headers.update(
            {"X-Api-Key": api_key, "Content-Type": "application/json"}
        )
        self._logger = logging.getLogger("renameinatorr")

    # ── low-level helpers ─────────────────────────────────────────────────────

    def _url(self, path: str) -> str:
        return f"{self.base}/api/v3/{path.lstrip('/')}"

    def _request_with_retry(self, method: str, path: str, **kwargs) -> Any:
        """
        Issue an HTTP request, retrying up to 3 times with exponential
        backoff on transient failures (connection errors and timeouts).

        HTTP error responses (4xx/5xx) are not retried — those indicate a
        request problem, not a network blip. One mid-run hiccup should not
        abort an entire instance run.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                r = self._session.request(method, self._url(path), timeout=60, **kwargs)
                r.raise_for_status()
                return r.json()
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as exc:
                last_exc = exc
                if attempt < 3:
                    wait = 2 ** attempt  # 2s, 4s
                    self._logger.warning(
                        "Transient %s error on %s (attempt %d/3), "
                        "retrying in %ds: %s",
                        method, path, attempt, wait, exc,
                    )
                    time.sleep(wait)
        raise last_exc  # type: ignore[misc]

    def _get(self, path: str, params: Optional[Dict] = None) -> Any:
        return self._request_with_retry("GET", path, params=params)

    def _post(self, path: str, body: Dict) -> Any:
        return self._request_with_retry("POST", path, json=body)

    def _put(self, path: str, body: Dict) -> Any:
        return self._request_with_retry("PUT", path, json=body)

    # ── connection ────────────────────────────────────────────────────────────

    def ping(self) -> bool:
        try:
            self._get("system/status")
            return True
        except Exception as exc:
            self._logger.error(
                "Cannot connect to %s (%s): %s", self.name, self.base, exc
            )
            return False

    # ── tags ─────────────────────────────────────────────────────────────────

    def get_tag_id(self, label: str) -> int:
        """Return the tag ID for *label*, creating the tag if it doesn't exist."""
        for t in self._get("tag"):
            if t["label"].lower() == label.lower():
                return t["id"]
        return self._post("tag", {"label": label})["id"]

    def _editor_endpoint(self) -> str:
        return "movie/editor" if self.instance_type == "radarr" else "series/editor"

    def _ids_key(self) -> str:
        return "movieIds" if self.instance_type == "radarr" else "seriesIds"

    def add_tags(self, media_ids: List[int], tag_id: int) -> None:
        """Bulk-add *tag_id* to all items in *media_ids*."""
        if not media_ids:
            return
        self._put(
            self._editor_endpoint(),
            {self._ids_key(): media_ids, "tags": [tag_id], "applyTags": "add"},
        )

    def remove_tags(self, media_ids: List[int], tag_id: int) -> None:
        """Bulk-remove *tag_id* from all items in *media_ids*."""
        if not media_ids:
            return
        self._put(
            self._editor_endpoint(),
            {self._ids_key(): media_ids, "tags": [tag_id], "applyTags": "remove"},
        )

    # ── media retrieval ───────────────────────────────────────────────────────

    def get_parsed_media(self) -> List[Dict]:
        """
        Return a normalised list of media items.

        Each dict contains:
          media_id, title, year, tags, path_name, root_folder
        """
        if self.instance_type == "radarr":
            raw = self._get("movie")
            return [
                {
                    "media_id":    m["id"],
                    "title":       m.get("title", "Unknown"),
                    "year":        m.get("year", 0),
                    "tags":        m.get("tags", []),
                    "path_name":   m.get("path", ""),
                    "root_folder": m.get("rootFolderPath", ""),
                    "has_file":    m.get("hasFile", False),
                    "file_count":  1 if m.get("hasFile") else 0,
                }
                for m in raw
            ]
        else:  # sonarr
            raw = self._get("series")
            return [
                {
                    "media_id":    s["id"],
                    "title":       s.get("title", "Unknown"),
                    "year":        s.get("year", 0),
                    "tags":        s.get("tags", []),
                    "path_name":   s.get("path", ""),
                    "root_folder": s.get("rootFolderPath", ""),
                    "has_file":    s.get("statistics", {}).get("episodeFileCount", 0) > 0,
                    "file_count":  s.get("statistics", {}).get("episodeFileCount", 0),
                }
                for s in raw
            ]

    # ── rename list ───────────────────────────────────────────────────────────

    def get_rename_list(self, media_id: int) -> List[Dict]:
        """
        Return items that Radarr/Sonarr says need renaming for *media_id*.

        Each dict has at minimum: existingPath, newPath, and the relevant
        file ID key.
        """
        id_param = "movieId" if self.instance_type == "radarr" else "seriesId"
        return self._get("rename", params={id_param: media_id})

    # ── rename execution ──────────────────────────────────────────────────────

    def rename_media(self, media_file_ids: Dict[int, List[Dict]]) -> List[int]:
        """
        Trigger RenameFiles commands and verify completion.

        Radarr: fires all commands simultaneously then verifies all at once.
        Movies are single-file and Radarr processes them near-instantly, so
        the cold-queue problem that affects Sonarr does not apply. Bulk firing
        is significantly faster for large movie libraries.

        Sonarr: fires one series at a time and verifies each before moving on.
        A single large series (e.g. The Simpsons at 339 files) can block
        everything queued behind it if commands are fired simultaneously,
        making bulk timeouts unpredictable.

        Returns a list of media_ids that failed verification. An empty list
        means all renames succeeded.
        """
        id_param    = "movieId"     if self.instance_type == "radarr" else "seriesId"
        file_id_key = "movieFileId" if self.instance_type == "radarr" else "episodeFileId"
        failed: List[int] = []

        if self.instance_type == "radarr":
            # ── Radarr: bulk fire, bulk verify ────────────────────────────────
            fired: List[int] = []
            for media_id, rename_list in media_file_ids.items():
                file_ids = [item[file_id_key] for item in rename_list if file_id_key in item]
                if not file_ids:
                    self._logger.warning(
                        "No %s found in rename list for media %d — skipping.",
                        file_id_key, media_id,
                    )
                    continue
                body = {"name": "RenameFiles", id_param: media_id, "files": file_ids}
                self._logger.debug("RenameFiles command body: %s", body)
                resp = self._post("command", body)
                self._logger.debug("RenameFiles command response: %s", resp)
                if not resp.get("id"):
                    self._logger.warning(
                        "RenameFiles command for media %d returned no command ID: %s",
                        media_id, resp,
                    )
                fired.append(media_id)

            if fired:
                # For Radarr each movie has exactly one file, so total_files
                # equals the number of fired commands. The * 2 multiplier
                # gives 2 seconds per movie, which is generous for near-instant
                # processing but keeps the timeout proportional to batch size.
                # Note: with count: 0 all movies fire simultaneously — for very
                # large libraries consider keeping count at 50-100 to avoid
                # sending hundreds of commands at once.
                bulk_wait = max(30, len(fired) * 2)
                self._logger.info(
                    "Verifying %d movie rename(s) (timeout %ds)…",
                    len(fired), bulk_wait,
                )
                deadline = time.time() + bulk_wait
                remaining = self.verify_renames(fired)
                while remaining and time.time() < deadline:
                    time.sleep(3)
                    remaining = self.verify_renames(list(remaining.keys()))
                if remaining:
                    failed.extend(remaining.keys())

        else:
            # ── Sonarr: serialize one series at a time ────────────────────────
            total = len(media_file_ids)
            for idx, (media_id, rename_list) in enumerate(media_file_ids.items(), 1):
                file_ids = [item[file_id_key] for item in rename_list if file_id_key in item]
                if not file_ids:
                    self._logger.warning(
                        "No %s found in rename list for media %d — skipping.",
                        file_id_key, media_id,
                    )
                    self._logger.debug(
                        "Rename list keys for media %d: %s",
                        media_id, [list(item.keys()) for item in rename_list],
                    )
                    continue
                body = {"name": "RenameFiles", id_param: media_id, "files": file_ids}
                self._logger.debug("RenameFiles command body: %s", body)
                resp = self._post("command", body)
                self._logger.debug("RenameFiles command response: %s", resp)
                if not resp.get("id"):
                    self._logger.warning(
                        "RenameFiles command for media %d returned no command ID: %s",
                        media_id, resp,
                    )
                per_item_wait = max(30, len(file_ids) * 5)
                self._logger.info(
                    "Renaming %d/%d: media %d (%d file(s), timeout %ds)…",
                    idx, total, media_id, len(file_ids), per_item_wait,
                )
                remaining = self.verify_renames([media_id])
                deadline  = time.time() + per_item_wait
                while remaining and time.time() < deadline:
                    time.sleep(3)
                    remaining = self.verify_renames([media_id])
                if remaining:
                    self._logger.warning(
                        "Media %d: rename did not complete within %ds.",
                        media_id, per_item_wait,
                    )
                    failed.append(media_id)
                else:
                    self._logger.debug("Media %d: rename verified.", media_id)

        return failed

    def wait_for_files_found(
        self,
        media_ids: List[int],
        expected_paths: Optional[Dict[int, str]] = None,
        max_wait: int = 120,
        interval: int = 3,
    ) -> bool:
        """
        Poll until Radarr/Sonarr confirms files are present for all *media_ids*.

        If *expected_paths* is provided (media_id -> expected path), the check
        requires both that the record path matches the expected value AND that
        hasFile / episodeFileCount confirms a file is present. This is used
        after folder renames to ensure Radarr has rescanned the new location
        rather than just seeing hasFile=True from before the move.

        Without *expected_paths*, only hasFile / episodeFileCount is checked.
        """
        deadline  = time.time() + max_wait
        remaining = set(media_ids)
        # Always wait at least one interval before the first poll — this
        # ensures a minimum gap between firing the refresh and proceeding,
        # even when hasFile / episodeFileCount was already satisfied before
        # the refresh started.
        time.sleep(interval)
        while time.time() < deadline and remaining:
            for media_id in list(remaining):
                try:
                    if self.instance_type == "radarr":
                        record   = self._get(f"movie/{media_id}")
                        has_file = record.get("hasFile", False)
                        if expected_paths:
                            expected = expected_paths.get(media_id, "")
                            if has_file and record.get("path", "").rstrip("/\\") == expected.rstrip("/\\"):
                                remaining.discard(media_id)
                        elif has_file:
                            remaining.discard(media_id)
                    else:
                        record     = self._get(f"series/{media_id}")
                        file_count = record.get("statistics", {}).get("episodeFileCount", 0)
                        if expected_paths:
                            expected = expected_paths.get(media_id, "")
                            if file_count > 0 and record.get("path", "").rstrip("/\\") == expected.rstrip("/\\"):
                                remaining.discard(media_id)
                        elif file_count > 0:
                            remaining.discard(media_id)
                except Exception:
                    pass
            if remaining:
                time.sleep(interval)
        if remaining:
            self._logger.warning(
                "%d item(s) did not confirm files found within %ds.",
                len(remaining), max_wait,
            )
            return False
        return True

    def verify_renames(self, media_ids: List[int]) -> Dict[int, List[Dict]]:
        """
        Re-check the rename list for *media_ids* after a rename command.

        Returns a dict of media_id to remaining rename-list entries for any
        items that still have files needing renaming, indicating the rename
        did not complete successfully.
        """
        remaining: Dict[int, List[Dict]] = {}
        for media_id in media_ids:
            leftover = self.get_rename_list(media_id)
            if leftover:
                remaining[media_id] = leftover
        return remaining

    def rename_folders(self, media_ids: List[int], root_folder: str) -> bool:
        """
        Ask the app to rename folders to match its configured naming format.

        Uses the native movie/editor or series/editor endpoint with
        moveFiles=true and the same rootFolderPath the items are already in.
        This delegates all naming logic (token expansion, colon replacement,
        illegal character handling, CleanTitle, etc.) entirely to Radarr /
        Sonarr, so the script never needs to reimplement it.

        Returns True if the editor call succeeded, False on error.
        """
        if self.instance_type == "radarr":
            body = {
                "movieIds":       media_ids,
                "rootFolderPath": root_folder,
                "moveFiles":      True,
            }
        else:
            body = {
                "seriesIds":      media_ids,
                "rootFolderPath": root_folder,
                "moveFiles":      True,
            }
        try:
            self._put(self._editor_endpoint(), body)
            return True
        except Exception as exc:
            self._logger.warning("Folder rename via editor failed: %s", exc)
            return False

    # ── refresh ───────────────────────────────────────────────────────────────

    def refresh_items(self, media_ids: List[int]) -> None:
        """
        Trigger a fire-and-forget metadata refresh for *media_ids*.

        For Sonarr a command is fired per series due to API limitations.
        Callers use wait_for_files_found to confirm files are present after
        the refresh rather than polling command status.
        """
        if self.instance_type == "radarr":
            self._post("command", {"name": "RefreshMovie", "movieIds": media_ids})
        else:
            for media_id in media_ids:
                self._post("command", {"name": "RefreshSeries", "seriesId": media_id})


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _clean_path(path: Optional[str]) -> str:
    """Strip leading Season directories and slashes – display only."""
    if not path:
        return ""
    path = SEASON_REGEX.sub("", path)
    return path.lstrip("/")


def get_chunks(items: List[Any], size: int) -> List[List[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def get_effective_count(settings: Dict) -> int:
    """Return the count limit for this instance (0 = process everything)."""
    return settings.get("count", 0)


# ─────────────────────────────────────────────────────────────────────────────
# Core processing
# ─────────────────────────────────────────────────────────────────────────────


def process_instance(
    app: ArrClient,
    settings: Dict,
    dry_run: bool,
    logger: logging.Logger,
    title_filter: Optional[str] = None,
) -> List[Dict]:
    """
    Rename media (and optionally folders) for a single instance.

    Returns a list of processed item dicts for summary output.
    """
    instance_start        = time.time()
    enable_batching       = settings.get("enable_batching", False)
    rename_folders        = settings.get("rename_folders", False)
    refresh_before_rename = settings.get("refresh_before_rename", False)
    tag_name              = settings.get("tag_name")
    ignore_tag            = settings.get("ignore_tag")
    count: int            = get_effective_count(settings)

    # CLI --title overrides yml title_filter; both are optional.
    title_filter = title_filter or settings.get("title_filter") or None

    logger.info("── %s (%s) ──────────────────────────────────", app.name, app.instance_type)
    logger.info("Using count=%s", count if count else "all")

    media_list = app.get_parsed_media()

    # ── title filter (--title CLI flag or title_filter in yml) ───────────────
    if title_filter:
        needle = title_filter.lower()
        before = len(media_list)
        media_list = [m for m in media_list if needle in m["title"].lower()]
        logger.info(
            "Title filter %r matched %d / %d item(s).",
            title_filter, len(media_list), before,
        )
        if not media_list:
            logger.warning("No items matched title filter %r – nothing to do.", title_filter)
            return []

    # ── ignore-tag filtering ──────────────────────────────────────────────────
    if ignore_tag:
        ignore_tag_id = app.get_tag_id(ignore_tag)
        before        = len(media_list)
        media_list    = [m for m in media_list if ignore_tag_id not in m["tags"]]
        skipped       = before - len(media_list)
        if skipped:
            logger.info("Skipped %d item(s) due to ignore tag '%s'.", skipped, ignore_tag)

    # ── cycling tag logic ─────────────────────────────────────────────────────
    tag_id: Optional[int] = None
    if tag_name:
        tag_id   = app.get_tag_id(tag_name)
        untagged = [m for m in media_list if tag_id not in m["tags"]]
        if not untagged:
            all_ids = [m["media_id"] for m in media_list]
            logger.info("All media tagged – clearing tags to start new cycle.")
            if not dry_run:
                app.remove_tags(all_ids, tag_id)
            media_list = app.get_parsed_media()
            if ignore_tag:
                ignore_tag_id = app.get_tag_id(ignore_tag)
                media_list    = [m for m in media_list if ignore_tag_id not in m["tags"]]
        else:
            tagged_count = len(media_list) - len(untagged)
            media_list   = untagged
            logger.info(
                "%d / %d item(s) untagged this cycle (%d already tagged, skipping).",
                len(media_list), len(media_list) + tagged_count, tagged_count,
            )

    # ── build chunks to process ───────────────────────────────────────────────
    if enable_batching:
        if not count:
            # count: 0 with batching would mean one giant chunk. Clamp to the
            # default chunk size and warn so the user fixes their config.
            logger.warning(
                "count: 0 with enable_batching: true is not supported — "
                "clamping chunk size to %d. Set count to 50-75 in the yml.",
                DEFAULT_BATCH_SIZE,
            )
        chunk_size = count if count else DEFAULT_BATCH_SIZE
        chunks     = get_chunks(media_list, chunk_size)
        logger.info("Batching enabled: %d chunk(s) of up to %d items.", len(chunks), chunk_size)
    else:
        if not count and len(media_list) > DEFAULT_BATCH_SIZE:
            logger.warning(
                "count: 0 with %d items — the whole library will be processed "
                "in one chunk. For large libraries set count to 50-75 to keep "
                "timeouts proportional and API load manageable.",
                len(media_list),
            )
        chunks = get_chunks(media_list, count)[:1] if count else [media_list]

    final_results: List[Dict] = []

    for chunk_index, chunk in enumerate(chunks, 1):
        chunk_start = time.time()
        logger.info(
            "Processing chunk %d / %d (%d items)…", chunk_index, len(chunks), len(chunk)
        )

        grouped_root_folders: Dict[str, List[int]] = defaultdict(list)
        # Maps media_id to raw rename-list entries (reused by rename_media to
        # avoid fetching the rename list a second time).
        media_rename_lists: Dict[int, List[Dict]] = {}

        # ── optional metadata refresh ─────────────────────────────────────────
        # When refresh_before_rename is enabled, force a metadata refresh for
        # each item in the chunk and wait for it to complete before checking
        # the rename list. This ensures Sonarr/Radarr has the latest episode
        # titles from TVDB/TMDB before we ask what needs renaming, preventing
        # the case where a title update is not yet reflected in the rename list.
        if refresh_before_rename and not dry_run:
            chunk_ids = [item["media_id"] for item in chunk]
            logger.info("Refreshing metadata for %d item(s) before rename check…", len(chunk_ids))
            app.refresh_items(chunk_ids)
            # Give the app a moment to queue and start processing the refresh
            # before we begin polling.
            time.sleep(5)
            logger.info("Metadata refresh triggered — proceeding with rename check.")

        for item in chunk:
            rename_response = app.get_rename_list(item["media_id"])

            file_info: Dict[str, str] = {}
            for r in rename_response:
                existing = _clean_path(r.get("existingPath"))
                new      = _clean_path(r.get("newPath"))
                if existing:
                    file_info[existing] = new

            item["file_info"]    = file_info
            item["new_path_name"] = None

            if file_info:
                media_rename_lists[item["media_id"]] = rename_response

            if rename_folders:
                grouped_root_folders[item["root_folder"]].append(item["media_id"])

        # ── dry run: show folder renames that would happen ────────────────────
        if dry_run and rename_folders and grouped_root_folders:
            logger.info(
                "[DRY RUN] Would trigger native folder rename for %d item(s).",
                sum(len(v) for v in grouped_root_folders.values()),
            )

        if not dry_run:
            # ── rename files ──────────────────────────────────────────────────
            if media_rename_lists:
                logger.info(
                    "Renaming files for %d item(s) (one at a time)…",
                    len(media_rename_lists),
                )
                failed_ids = app.rename_media(media_rename_lists)
                if failed_ids:
                    logger.warning(
                        "%d item(s) did not complete rename within timeout: %s",
                        len(failed_ids), failed_ids,
                    )
                else:
                    logger.info("All file renames completed successfully.")

                # Only fire the post-file-rename refresh when no folder
                # renames are pending in this chunk. When both file and folder
                # renames are needed, firing an intermediate refresh causes
                # Sonarr's rescan to collide with the folder rename — the
                # rescan runs while folders are moving, producing
                # MissingFromDisk events. The post-folder-rename refresh
                # covers the full state update after both operations complete.
                has_pending_folder_renames = rename_folders and grouped_root_folders
                if not has_pending_folder_renames:
                    logger.info("Triggering post-file-rename refresh…")
                    app.refresh_items(list(media_rename_lists.keys()))
                else:
                    logger.info(
                        "Skipping post-file-rename refresh — folder renames "
                        "pending, post-folder-rename refresh will cover both."
                    )
            else:
                logger.info("No files need renaming in this chunk.")

            # ── tag items ─────────────────────────────────────────────────────
            # Tag every item in the chunk so it is skipped on the next run,
            # regardless of whether files or folders needed renaming.
            if tag_id:
                all_chunk_ids = [item["media_id"] for item in chunk]
                logger.info(
                    "Applying tag '%s' to %d item(s)…", tag_name, len(all_chunk_ids)
                )
                app.add_tags(all_chunk_ids, tag_id)

            # ── rename folders ────────────────────────────────────────────────
            if rename_folders and grouped_root_folders:
                logger.info("Renaming folders in %s…", app.name)
                for root_folder, folder_ids in grouped_root_folders.items():
                    app.rename_folders(folder_ids, root_folder)

                # The editor endpoint updates the DB immediately, so path
                # changes can be detected without waiting for a refresh.
                updated = {m["media_id"]: m for m in app.get_parsed_media()}
                actually_renamed: List[int] = []
                for item in chunk:
                    new_item = updated.get(item["media_id"])
                    if new_item and new_item["path_name"] != item["path_name"]:
                        item["new_path_name"] = new_item["path_name"]
                        actually_renamed.append(item["media_id"])
                        logger.info(
                            "Folder renamed: %s  →  %s",
                            item["path_name"],
                            item["new_path_name"],
                        )

                # Only fire a rescan refresh when folders actually moved so
                # Radarr/Sonarr picks up files at the new location. Wait for
                # the refresh to complete before finishing — this closes the
                # window where external tools (e.g. Notifiarr) can pick up a
                # spurious MissingFromDisk event before Radarr has rescanned
                # the new path and confirmed the files are there.
                if actually_renamed:
                    logger.info("Triggering post-folder-rename refresh…")
                    app.refresh_items(actually_renamed)
                    total_folder_files = sum(
                        item.get("file_count", 1)
                        for item in chunk
                        if item.get("new_path_name")
                    )
                    folder_wait = max(30, total_folder_files * 2)
                    expected_paths = {
                        item["media_id"]: item["new_path_name"]
                        for item in chunk if item.get("new_path_name")
                    }
                    # Only wait for items that had files on disk before the
                    # rename — items with no files (e.g. upcoming/placeholder
                    # entries) will never satisfy hasFile=True and would wait
                    # the full timeout unnecessarily.
                    has_file_ids = [
                        item["media_id"] for item in chunk
                        if item.get("new_path_name") and item.get("has_file")
                    ]
                    if has_file_ids:
                        logger.info(
                            "Waiting for Radarr/Sonarr to confirm files found "
                            "at new location (%d file(s) across %d folder(s), timeout %ds)…",
                            total_folder_files, len(has_file_ids), folder_wait,
                        )
                        app.wait_for_files_found(
                            has_file_ids,
                            expected_paths=expected_paths,
                            max_wait=folder_wait,
                        )

        # ── collect results ───────────────────────────────────────────────────
        total_files   = sum(len(i.get("file_info", {})) for i in chunk)
        total_folders = sum(bool(i.get("new_path_name")) for i in chunk)
        elapsed       = time.time() - chunk_start
        logger.info(
            "Chunk %d done in %.1fs | files renamed: %d | folders renamed: %d",
            chunk_index, elapsed, total_files, total_folders,
        )

        final_results.extend(chunk)

    logger.info("Finished %s in %.1fs.", app.name, time.time() - instance_start)

    # ── sort and trim for output ──────────────────────────────────────────────
    final_results.sort(key=lambda i: i.get("new_path_name") or i["path_name"])
    return [
        {
            "title":        i["title"],
            "year":         i["year"],
            "path_name":    i["path_name"],
            "new_path_name": i.get("new_path_name"),
            "file_info":    dict(sorted(i.get("file_info", {}).items())),
        }
        for i in final_results
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Output
# ─────────────────────────────────────────────────────────────────────────────


def print_output(results: Dict[str, Dict[str, Any]], logger: logging.Logger) -> None:
    for instance_name, data in results.items():
        items  = data.get("data", [])
        server = data.get("server_name", instance_name)

        has_file_renames   = any(i["file_info"]    for i in items)
        has_folder_renames = any(i["new_path_name"] for i in items)

        if not has_file_renames and not has_folder_renames:
            logger.info("[%s] No items needed renaming.", server)
            continue

        logger.info("━" * 60)
        logger.info("  %s – Rename Results", server.upper())
        logger.info("━" * 60)

        for item in items:
            if not item["file_info"] and not item["new_path_name"]:
                continue
            year_str = str(item["year"]) if item["year"] else ""
            title_display = item["title"]
            if year_str and not title_display.endswith(f"({year_str})"):
                title_display = f"{title_display} ({year_str})"
            logger.info("%s", title_display)
            if item["new_path_name"]:
                logger.info(
                    "  Folder:  %s  →  %s", item["path_name"], item["new_path_name"]
                )
            for old_path, new_path in item["file_info"].items():
                logger.info("  File:")
                logger.info("    Old: %s", old_path)
                logger.info("    New: %s", new_path)
            logger.info("")

        total              = len(items)
        total_file_items   = sum(1 for i in items if i["file_info"])
        total_folder_items = sum(1 for i in items if i["new_path_name"])

        logger.info("─" * 60)
        logger.info("  Summary for %s", server)
        logger.info("  Total processed : %d", total)
        if has_file_renames:
            logger.info("  Items with file renames   : %d", total_file_items)
        if has_folder_renames:
            logger.info("  Items with folder renames : %d", total_folder_items)
        logger.info("─" * 60)
        logger.info("")


# ─────────────────────────────────────────────────────────────────────────────
# Discord notifications
# ─────────────────────────────────────────────────────────────────────────────

EMBED_COLOR = 0x2ECC71


def send_discord_notification(
    webhook_url: str,
    results: Dict[str, Dict[str, Any]],
    dry_run: bool,
    logger: logging.Logger,
    latest_version: Optional[str] = None,
    failed_instances: Optional[Dict[str, str]] = None,
) -> None:
    """
    Post a rename summary embed to a Discord webhook.

    One embed field per instance. Only instances where something was actually
    renamed are included.  Skips entirely if nothing changed.
    """
    fields = []

    for instance_name, data in results.items():
        items         = data.get("data", [])
        renamed_items = [i for i in items if i.get("file_info") or i.get("new_path_name")]
        total_checked = len(items)

        if not renamed_items:
            fields.append({
                "name":   data["server_name"],
                "value":  f"✅ No items needed renaming  ({total_checked} checked)",
                "inline": False,
            })
            continue

        lines_files   = []
        lines_folders = []
        for item in renamed_items:
            year_str = str(item["year"]) if item["year"] else ""
            title_display = item["title"]
            if year_str and not title_display.endswith(f"({year_str})"):
                title_display = f"{title_display} ({year_str})"

            if item.get("new_path_name"):
                old_folder = Path(item["path_name"]).name
                new_folder = Path(item["new_path_name"]).name
                lines_folders.append(
                    f"**{title_display}**"
                    f"\n　📁 `{old_folder}`"
                    f"\n　　→ `{new_folder}`"
                )

            if item.get("file_info"):
                file_lines = [f"**{title_display}**"]
                for old_file, new_file in item["file_info"].items():
                    old_str = old_file if len(old_file) <= 60 else old_file[:57] + "…"
                    new_str = new_file if len(new_file) <= 60 else new_file[:57] + "…"
                    file_lines.append(f"　📄 `{old_str}`\n　　→ `{new_str}`")
                lines_files.append("\n".join(file_lines))

        total_files   = sum(len(i.get("file_info", {})) for i in renamed_items)
        total_folders = sum(1 for i in renamed_items if i.get("new_path_name"))
        parts = []
        if total_files:
            parts.append(f"{total_files} file{'s' if total_files != 1 else ''}")
        if total_folders:
            parts.append(f"{total_folders} folder{'s' if total_folders != 1 else ''}")
        header = (
            f"{data['server_name']}  -  "
            f"{len(renamed_items)} / {total_checked} changed"
            + (f"  ({', '.join(parts)})" if parts else "")
        )

        if lines_files:
            value = "\n\n".join(lines_files)
            if len(value) > 1024:
                value = value[:1020] + "\n…"
            fields.append({"name": f"{header}  ·  📄 Files", "value": value, "inline": False})

        if lines_folders:
            value = "\n\n".join(lines_folders)
            if len(value) > 1024:
                value = value[:1020] + "\n…"
            fields.append({"name": f"{header}  ·  📁 Folders", "value": value, "inline": False})

    # Failed instances get a clear error field so users notice at a glance
    # rather than wondering why an instance is missing from the notification.
    if failed_instances:
        for inst_name, reason in failed_instances.items():
            reason_short = reason if len(reason) <= 200 else reason[:197] + "…"
            fields.append({
                "name":   f"⚠️ {inst_name}",
                "value":  f"Run failed: {reason_short}",
                "inline": False,
            })

    if not fields:
        logger.debug("Discord: nothing to report, skipping notification.")
        return

    if latest_version:
        fields.insert(0, {
            "name":   "🆕 Update available",
            "value":  f"v{VERSION} → v{latest_version}\n[Download from GitHub]({GITHUB_RELEASE_URL})",
            "inline": False,
        })

    title = "✏️ Renameinatorr"
    if dry_run:
        title += "  `[DRY RUN]`"

    embed = {
        "title":     title,
        "color":     EMBED_COLOR,
        "fields":    fields,
        "footer":    {"text": f"renameinatorr v{VERSION}"},
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

    try:
        resp = requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)
        resp.raise_for_status()
        logger.info("Discord notification sent.")
    except Exception as exc:
        logger.warning("Discord notification failed: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Config loading
# ─────────────────────────────────────────────────────────────────────────────


def load_config(path: Path) -> Dict:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return {**DEFAULT_CONFIG, **data}


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="renameinatorr – standalone Radarr/Sonarr file & folder renamer"
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"renameinatorr v{VERSION}",
        help="Show version number and exit",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).with_name("renameinatorr.yml"),
        help="Path to YAML config (default: renameinatorr.yml next to this script)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would be renamed without making any changes",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "--title",
        metavar="SUBSTRING",
        default=None,
        help=(
            "Only process items whose title contains SUBSTRING "
            "(case-insensitive).  Useful for testing a single item."
        ),
    )
    args = parser.parse_args()

    run_start = time.time()
    config    = load_config(args.config)
    log_level = "DEBUG" if args.debug else config.get("log_level", "INFO")
    logger    = setup_logging(log_level, log_file=config.get("log_file"))

    dry_run: bool = args.dry_run or config.get("dry_run", False)

    logger.info("renameinatorr v%s", VERSION)

    latest_version = check_for_update(logger)
    if latest_version:
        logger.warning(
            "Update available: v%s → v%s  %s",
            VERSION, latest_version, GITHUB_RELEASE_URL,
        )
    else:
        logger.debug("Version check: already up to date.")

    if dry_run:
        logger.info("═" * 50)
        logger.info("DRY RUN – no changes will be made")
        logger.info("═" * 50)

    instances = config.get("instances", [])
    if not instances:
        logger.error("No instances defined in config. Exiting.")
        sys.exit(1)

    all_results: Dict[str, Dict[str, Any]] = {}
    failed_instances: Dict[str, str] = {}

    for inst in instances:
        name      = inst.get("name", "Unknown")
        inst_type = inst.get("type", "").lower()
        url       = inst.get("url", "")
        api_key   = inst.get("api_key", "")

        if inst_type not in ("radarr", "sonarr"):
            logger.warning("Instance %s: unknown type %r – skipping.", name, inst_type)
            continue
        if not url or not api_key:
            logger.warning("Instance %s: missing url or api_key – skipping.", name)
            continue

        app = ArrClient(url, api_key, inst_type, name)
        if not app.ping():
            failed_instances[name] = "connection failed"
            continue

        try:
            data = process_instance(app, inst, dry_run, logger, title_filter=args.title)
            all_results[name] = {"server_name": name, "data": data}
        except Exception as exc:
            logger.exception("Error processing instance %s", name)
            failed_instances[name] = f"{type(exc).__name__}: {exc}"

    if all_results:
        print_output(all_results, logger)
    else:
        logger.info("No results to display.")

    webhook_url = config.get("discord_webhook")
    if webhook_url and (all_results or failed_instances):
        send_discord_notification(
            webhook_url, all_results, dry_run, logger,
            latest_version, failed_instances,
        )

    # ── run summary footer ────────────────────────────────────────────────────
    total_files = sum(
        sum(len(i.get("file_info", {})) for i in d.get("data", []))
        for d in all_results.values()
    )
    total_folders = sum(
        sum(1 for i in d.get("data", []) if i.get("new_path_name"))
        for d in all_results.values()
    )
    elapsed = time.time() - run_start
    logger.info(
        "Done: %d instance(s) | %d file(s) renamed | %d folder(s) renamed "
        "| %d failure(s) | %.1fs",
        len(all_results), total_files, total_folders,
        len(failed_instances), elapsed,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(0)
