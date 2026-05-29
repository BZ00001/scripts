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

DEFAULT_CONFIG: Dict[str, Any] = {
    "dry_run": False,
    "log_level": "INFO",
    "instances": [],
}

# ─────────────────────────────────────────────────────────────────────────────
# Folder-name helpers  (module-level, shared by both formatters)
# ─────────────────────────────────────────────────────────────────────────────

def _move_the(title: str) -> str:
    """'The Foo' → 'Foo, The'  (also handles 'A' and 'An')."""
    m = re.match(r"^(The|A|An)\s+(.+)$", title, re.IGNORECASE)
    return f"{m.group(2)}, {m.group(1)}" if m else title


def _colon_replacement(naming_config: Dict) -> tuple:
    """
    Return (colon_space_replace, colon_replace) strings from the naming config.

    Sonarr's Smart mode (from FileNameBuilder.cs) replaces ': ' (colon-space)
    with ' - ' for better appearance, then bare ':' with '-'.
    Other modes apply the same replacement to all colons.

    Returns a tuple of (colon_space_replace, colon_replace) so _clean_title
    can apply them in the correct order.
    """
    fmt = naming_config.get("colonReplacementFormat", 0)
    # Map to (colon-space replacement, bare-colon replacement)
    mapping = {
        0: ("", ""),                   # delete
        1: ("-", "-"),                 # dash
        2: (" -", " -"),              # space dash
        3: (" - ", " - "),            # space dash space
        4: (" - ", "-"),              # smart: ': ' → ' - ', ':' → '-'
        "delete":         ("", ""),
        "dash":           ("-", "-"),
        "spaceDash":      (" -", " -"),
        "spaceDashSpace": (" - ", " - "),
        "smart":          (" - ", "-"),
    }
    return mapping.get(fmt, ("", ""))


def _clean_title(title: str, colon_replace: tuple = ("", ""), replace_illegal: bool = False) -> str:
    """
    Strip / replace characters that are illegal in folder names.

    *colon_replace* is a (colon_space_replace, bare_colon_replace) tuple from
    _colon_replacement(), matching Sonarr/Radarr's CleanFileName logic exactly:
    Smart mode replaces ': ' and ':' differently; other modes use the same
    string for both.

    *replace_illegal* mirrors the app's replaceIllegalCharacters setting.
    When True, each illegal character is replaced using Sonarr's exact mapping
    (from FileNameBuilder.cs):
      \\  →  +      /  →  +
      <   →  ''     >  →  ''
      ?   →  !      *  →  -
      |   →  ''     "  →  ''
    When False, all illegal characters are removed entirely.
    """
    # Normalise curly/fancy apostrophes and quotes to ASCII equivalents
    # so folder-name comparisons work regardless of what the API returns.
    title = title.replace("\u2019", "'").replace("\u2018", "'")   # ' '
    title = title.replace("\u201c", '"').replace("\u201d", '"')   # " "
    # Apply colon-space first (Smart mode distinction), then bare colon.
    colon_space_rep, bare_colon_rep = colon_replace
    title = title.replace(": ", f"{colon_space_rep} " if colon_space_rep else " ")
    title = title.replace(":", bare_colon_rep)
    if replace_illegal:
        # Per-character replacements from Sonarr/Radarr FileNameBuilder.cs
        title = title.replace("\\", "+").replace("/", "+")
        title = title.replace("<", "").replace(">", "")
        title = title.replace("?", "!").replace("*", "-")
        title = title.replace("|", "").replace('"', "")
    else:
        title = re.sub(r'[<>"/\\|?*]', "", title)
    return re.sub(r"\s{2,}", " ", title).strip()


def _with_year(base: str, year_str: str) -> str:
    """Append (year) to *base* unless it is already present."""
    if not year_str:
        return base
    if base.endswith(f"({year_str})"):
        return base
    return f"{base} ({year_str})"


def _format_folder_name(record: Dict, folder_format: str, prefix: str, colon_replace: tuple = ("", ""), replace_illegal: bool = False) -> str:
    """
    Expand arr folder-format tokens for *record*.

    *prefix* is ``"Movie"`` for Radarr or ``"Series"`` for Sonarr.
    *colon_replace* and *replace_illegal* are derived from the app's naming
    config and applied consistently so computed names match what the app
    would produce.  Tokens not present in the replacements dict are left
    as-is so they surface clearly in logs rather than silently disappearing.
    """
    title:   str = record.get("title", "Unknown")
    year:    int = record.get("year", 0)
    imdb_id: str = record.get("imdbId", "") or ""
    year_str     = str(year) if year else ""

    title_clean     = _clean_title(title, colon_replace, replace_illegal)
    title_the_clean = _clean_title(_move_the(title), colon_replace, replace_illegal)

    replacements: Dict[str, str] = {
        f"{{{prefix} TitleTheYear}}": _with_year(title_the_clean, year_str),
        f"{{{prefix} TitleYear}}":    _with_year(title_clean,     year_str),
        f"{{{prefix} TitleThe}}":     title_the_clean,
        f"{{{prefix} Title}}":        title_clean,
        f"{{{prefix} CleanTitle}}":   title_clean,
        f"{{{prefix} Year}}":         year_str,
        f"{{{prefix} ImdbId}}":       imdb_id,
        # Shorthand variants used in some folder-format strings
        "{ImdbId}":                   imdb_id,
        "{Year}":                     year_str,
    }

    if prefix == "Movie":
        tmdb_id: int = record.get("tmdbId", 0)
        replacements["{Movie TmdbId}"] = str(tmdb_id) if tmdb_id else ""
        replacements["{TmdbId}"]       = str(tmdb_id) if tmdb_id else ""
        # {Release Year} is Radarr's token for the theatrical release year –
        # equivalent to {Movie Year} in folder-format context.
        replacements["{Release Year}"] = year_str
        # {Edition Tags} / {Edition} expand to the edition tag (e.g. "Director's Cut").
        # We leave it empty here so folders without an edition are unaffected;
        # folders that include it will be skipped by the no-op replacement.
        edition = record.get("movieFile", {}).get("edition", "") or ""
        replacements["{Edition Tags}"] = edition
        replacements["{Edition}"]      = edition
    else:
        tvdb_id: int = record.get("tvdbId", 0)
        replacements[f"{{{prefix} TvdbId}}"] = str(tvdb_id) if tvdb_id else ""
        replacements["{TvdbId}"]             = str(tvdb_id) if tvdb_id else ""

    result = folder_format
    for token, value in replacements.items():
        result = result.replace(token, value)
    return result.strip()


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────


def setup_logging(level: str) -> logging.Logger:
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=numeric,
    )
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

    def _get(self, path: str, params: Optional[Dict] = None) -> Any:
        r = self._session.get(self._url(path), params=params, timeout=60)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: Dict) -> Any:
        r = self._session.post(self._url(path), json=body, timeout=60)
        r.raise_for_status()
        return r.json()

    def _put(self, path: str, body: Dict) -> Any:
        r = self._session.put(self._url(path), json=body, timeout=60)
        r.raise_for_status()
        return r.json()

    def _put_with_move(self, path: str, body: Dict) -> Any:
        """PUT with moveFiles=true so the app physically moves the folder on disk."""
        r = self._session.put(
            self._url(path), json=body, params={"moveFiles": "true"}, timeout=60
        )
        r.raise_for_status()
        return r.json()

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

    def rename_media(self, media_file_ids: Dict[int, List[Dict]]) -> None:
        """
        Trigger RenameFiles commands using pre-collected rename-list entries.

        *media_file_ids* maps media_id → raw rename-list dicts and is built
        during the item loop in process_instance, avoiding a second API fetch.
        """
        id_param     = "movieId"     if self.instance_type == "radarr" else "seriesId"
        file_id_key  = "movieFileId" if self.instance_type == "radarr" else "episodeFileId"

        for media_id, rename_list in media_file_ids.items():
            file_ids = [item[file_id_key] for item in rename_list if file_id_key in item]
            if not file_ids:
                continue
            body = {"name": "RenameFiles", id_param: media_id, "files": file_ids}
            self._post("command", body)

    def rename_folders(
        self,
        media_ids: List[int],
        root_folder: str,
        naming_config: Dict,
        dry_run: bool = False,
    ) -> Any:
        """
        Rename folders to match the configured naming format.

        The naming config is passed in (fetched once per instance) rather
        than fetched here to avoid a redundant API call per chunk.

        Both Radarr and Sonarr pre-check the expected folder name so items
        whose folder is already correct are silently skipped.

        Both instances update the media record's path via PUT with the
        moveFiles=true query parameter, which tells the app to physically
        move the folder on disk rather than just updating the database path.
        Returns True if at least one folder was renamed, False otherwise.
        """
        if self.instance_type == "radarr":
            folder_format: str = naming_config.get("movieFolderFormat", "{Movie TitleYear}")
            colon_replace  = _colon_replacement(naming_config)
            replace_illegal = naming_config.get("replaceIllegalCharacters", True)
            any_updated = False

            for movie_id in media_ids:
                try:
                    movie = self._get(f"movie/{movie_id}")
                except Exception as exc:
                    self._logger.warning("Could not fetch movie %d: %s", movie_id, exc)
                    continue

                if not movie.get("hasFile", False):
                    self._logger.debug(
                        "Movie %d (%s) has no files on disk, skipping folder rename.",
                        movie_id, movie.get("title", "?"),
                    )
                    continue

                new_folder     = _format_folder_name(movie, folder_format, "Movie", colon_replace, replace_illegal)
                current_folder = Path(movie.get("path", "").rstrip("/\\")).name

                if current_folder == new_folder:
                    self._logger.debug(
                        "Movie %d folder already correct (%s), skipping.",
                        movie_id, current_folder,
                    )
                else:
                    new_path = str(
                        Path(movie.get("rootFolderPath", "").rstrip("/\\")) / new_folder
                    )
                    self._logger.info(
                        "Movie folder rename: %s  →  %s", current_folder, new_folder
                    )
                    if not dry_run:
                        try:
                            movie["path"] = new_path
                            self._put_with_move(f"movie/{movie_id}", movie)
                            any_updated = True
                        except Exception as exc:
                            self._logger.warning(
                                "Failed to update movie %d path: %s", movie_id, exc
                            )

            return any_updated

        # ── Sonarr: update series path via PUT ────────────────────────────────
        folder_format   = naming_config.get("seriesFolderFormat", "{Series TitleYear}")
        colon_replace   = _colon_replacement(naming_config)
        replace_illegal = naming_config.get("replaceIllegalCharacters", True)
        any_updated     = False

        for series_id in media_ids:
            try:
                series = self._get(f"series/{series_id}")
            except Exception as exc:
                self._logger.warning("Could not fetch series %d: %s", series_id, exc)
                continue

            new_folder     = _format_folder_name(series, folder_format, "Series", colon_replace, replace_illegal)
            current_path   = series.get("path", "").rstrip("/\\")
            current_folder = Path(current_path).name

            if current_folder == new_folder:
                self._logger.debug(
                    "Series %d folder already correct (%s), skipping.",
                    series_id, current_folder,
                )
                continue

            new_path = str(Path(series.get("rootFolderPath", root_folder)) / new_folder)
            self._logger.info(
                "Series folder rename: %s  →  %s", current_folder, new_folder
            )

            if not dry_run:
                try:
                    series["path"] = new_path
                    self._put_with_move(f"series/{series_id}", series)
                    any_updated = True
                except Exception as exc:
                    self._logger.warning(
                        "Failed to update series %d path: %s", series_id, exc
                    )

        return any_updated  # True if at least one series path was changed

    # ── naming config ─────────────────────────────────────────────────────────

    def get_naming_config(self) -> Dict:
        """Return the Sonarr/Radarr naming configuration."""
        return self._get("config/naming")

    # ── refresh ───────────────────────────────────────────────────────────────

    def refresh_items(self, media_ids: List[int]) -> Dict:
        """
        Trigger a metadata refresh for *media_ids*.

        All folder renames are done via PUT before this is called, so the
        refresh is fire-and-forget — it just tells the app to rescan.
        For Sonarr a command is fired per series due to API limitations.
        """
        if self.instance_type == "radarr":
            return self._post("command", {"name": "RefreshMovie", "movieIds": media_ids})
        else:
            last: Dict = {}
            for media_id in media_ids:
                last = self._post("command", {"name": "RefreshSeries", "seriesId": media_id})
            return last


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


def get_effective_count(settings: Dict, logger: logging.Logger) -> int:
    """Return the count limit for this instance (0 = process everything)."""
    count = settings.get("count", 0)
    logger.info("Using count=%s", count if count else "all")
    return count


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
    instance_start  = time.time()
    enable_batching = settings.get("enable_batching", False)
    rename_folders  = settings.get("rename_folders", False)
    tag_name        = settings.get("tag_name")
    ignore_tag      = settings.get("ignore_tag")
    count: int      = get_effective_count(settings, logger)

    # CLI --title overrides yml title_filter; both are optional.
    title_filter = title_filter or settings.get("title_filter") or None

    logger.info("── %s (%s) ──────────────────────────────────", app.name, app.instance_type)

    # Fetch naming config once per instance run (not per chunk / per item).
    naming_config: Dict = app.get_naming_config() if rename_folders else {}

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
        chunk_size = count if count else DEFAULT_BATCH_SIZE
        chunks     = get_chunks(media_list, chunk_size)
        logger.info("Batching enabled: %d chunk(s) of up to %d items.", len(chunks), chunk_size)
    else:
        chunks = get_chunks(media_list, count)[:1] if count else [media_list]

    final_results: List[Dict] = []

    for chunk_index, chunk in enumerate(chunks, 1):
        chunk_start = time.time()
        logger.info(
            "Processing chunk %d / %d (%d items)…", chunk_index, len(chunks), len(chunk)
        )

        grouped_root_folders: Dict[str, List[int]] = defaultdict(list)
        # Maps media_id → raw rename-list entries (reused by rename_media to
        # avoid fetching the rename list a second time).
        media_rename_lists:   Dict[int, List[Dict]] = {}
        any_renamed = False

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
                any_renamed = True
                media_rename_lists[item["media_id"]] = rename_response

            if rename_folders:
                grouped_root_folders[item["root_folder"]].append(item["media_id"])

        # ── dry run: show folder renames that would happen ────────────────────
        if dry_run and rename_folders and grouped_root_folders:
            logger.info("[DRY RUN] Checking folder renames…")
            for root_folder, folder_ids in grouped_root_folders.items():
                app.rename_folders(folder_ids, root_folder, naming_config, dry_run=True)

        if not dry_run:
            # ── rename files ──────────────────────────────────────────────────
            if media_rename_lists:
                logger.info("Renaming files for %d item(s)…", len(media_rename_lists))
                app.rename_media(media_rename_lists)

            if any_renamed:
                logger.info("Triggering post-file-rename refresh…")
                app.refresh_items(list(media_rename_lists.keys()))
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
                all_folder_ids: List[int] = [
                    mid for ids in grouped_root_folders.values() for mid in ids
                ]
                any_folder_renamed = False
                for root_folder, folder_ids in grouped_root_folders.items():
                    result = app.rename_folders(folder_ids, root_folder, naming_config)
                    if result is True:
                        any_folder_renamed = True

                if any_folder_renamed:
                    logger.info("Triggering post-folder-rename refresh…")
                    app.refresh_items(all_folder_ids)

                    # Detect what actually changed.
                    updated = {m["media_id"]: m for m in app.get_parsed_media()}
                    for item in chunk:
                        new_item = updated.get(item["media_id"])
                        if new_item and new_item["path_name"] != item["path_name"]:
                            item["new_path_name"] = new_item["path_name"]
                            logger.info(
                                "Folder renamed: %s  →  %s",
                                item["path_name"],
                                item["new_path_name"],
                            )
                else:
                    logger.info("All folders already correctly named.")

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

        if not renamed_items:
            continue

        lines = []
        for item in renamed_items:
            year_str = str(item["year"]) if item["year"] else ""
            title_display = item["title"]
            if year_str and not title_display.endswith(f"({year_str})"):
                title_display = f"{title_display} ({year_str})"
            line = f"**{title_display}**"

            if item.get("new_path_name"):
                old_folder = Path(item["path_name"]).name
                new_folder = Path(item["new_path_name"]).name
                line += f"\n　📁 `{old_folder}`"
                line += f"\n　　→ `{new_folder}`"

            for old_file, new_file in item.get("file_info", {}).items():
                # Truncate long filenames so the embed stays readable
                old_str = old_file if len(old_file) <= 60 else old_file[:57] + "…"
                new_str = new_file if len(new_file) <= 60 else new_file[:57] + "…"
                line += f"\n　📄 `{old_str}`"
                line += f"\n　　→ `{new_str}`"

            lines.append(line)

        value = "\n\n".join(lines)
        if len(value) > 1024:
            value = value[:1020] + "\n…"

        total_files   = sum(len(i.get("file_info", {})) for i in renamed_items)
        total_folders = sum(1 for i in renamed_items if i.get("new_path_name"))
        parts = []
        if total_files:
            parts.append(f"{total_files} file{'s' if total_files != 1 else ''}")
        if total_folders:
            parts.append(f"{total_folders} folder{'s' if total_folders != 1 else ''}")
        field_name = f"{data['server_name']}  ({', '.join(parts)} renamed)"

        fields.append({"name": field_name, "value": value, "inline": False})

    if not fields:
        logger.debug("Discord: nothing to report, skipping notification.")
        return

    title = "✏️ Renameinatorr"
    if dry_run:
        title += "  `[DRY RUN]`"

    embed = {
        "title":     title,
        "color":     EMBED_COLOR,
        "fields":    fields,
        "footer":    {"text": "renameinatorr"},
        "timestamp": datetime.datetime.utcnow().isoformat(),
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

    config    = load_config(args.config)
    log_level = "DEBUG" if args.debug else config.get("log_level", "INFO")
    logger    = setup_logging(log_level)

    dry_run: bool = args.dry_run or config.get("dry_run", False)

    if dry_run:
        logger.info("═" * 50)
        logger.info("DRY RUN – no changes will be made")
        logger.info("═" * 50)

    instances = config.get("instances", [])
    if not instances:
        logger.error("No instances defined in config. Exiting.")
        sys.exit(1)

    all_results: Dict[str, Dict[str, Any]] = {}

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
            continue

        try:
            data = process_instance(app, inst, dry_run, logger, title_filter=args.title)
            all_results[name] = {"server_name": name, "data": data}
        except Exception:
            logger.exception("Error processing instance %s", name)

    if all_results:
        print_output(all_results, logger)
    else:
        logger.info("No results to display.")

    webhook_url = config.get("discord_webhook")
    if webhook_url and all_results:
        send_discord_notification(webhook_url, all_results, dry_run, logger)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(0)
