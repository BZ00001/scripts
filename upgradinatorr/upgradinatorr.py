#!/usr/bin/env python3

# ─────────────────────────────────────────────────────────────────────────────
# Credits
# ─────────────────────────────────────────────────────────────────────────────
# Original concept and logic by Drazzilb08
# https://github.com/Drazzilb08/daps
#
# This is a standalone reimplementation of the upgradinatorr module from the
# DAPS (Drazzilb's Arr PMM Scripts) project, stripped of the daps framework
# and rewritten to run as a self-contained script with no container dependency.
#
# All credit for the original design goes to Drazzilb08. Any bugs introduced
# here are entirely the fault of the reimplementation.
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Setup: Virtual Environment
# ─────────────────────────────────────────────────────────────────────────────
#
# 1. Create the virtual environment (once, in the script's directory):
#
#       python3 -m venv /mnt/user/appdata/scripts/natorr/python-venv
#
# 2. Install dependencies (once, or after updating):
#
#       /mnt/user/appdata/scripts/natorr/python-venv/bin/pip install requests pyyaml
#
# 3. Verify:
#
#       /mnt/user/appdata/scripts/natorr/python-venv/bin/python3 \
#           -c "import requests, yaml; print('OK')"
#
# 4. Run the script:
#
#       /mnt/user/appdata/scripts/natorr/python-venv/bin/python3 \
#           /mnt/user/appdata/scripts/natorr/upgradinatorr.py
#       ... --dry-run
#       ... --debug
#       ... --config /path/to/upgradinatorr.yml
#
# Unraid User Scripts: create a script containing:
#
#       #!/bin/bash
#       /mnt/user/appdata/scripts/natorr/python-venv/bin/python3 \
#           /mnt/user/appdata/scripts/natorr/upgradinatorr.py
#
# ─────────────────────────────────────────────────────────────────────────────

"""
upgradinatorr.py – Standalone upgrade trigger for Radarr / Sonarr.

Cycles through a configurable number of items that haven't been tagged yet,
triggers a quality-upgrade search for each one, then tags them so they're
skipped on the next run.  When every item is tagged and `unattended` is true
the tags are cleared and the cycle starts over.

Configuration is read from upgradinatorr.yml in the same directory (or the
path passed with --config).

Dependencies:  pip install requests pyyaml
"""

import argparse
import datetime
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import yaml

# ─────────────────────────────────────────────────────────────────────────────
# Version check
# ─────────────────────────────────────────────────────────────────────────────

def _parse_version(v: str) -> tuple:
    """Parse a version string into a tuple of ints for comparison."""
    try:
        return tuple(int(x) for x in v.strip().split("."))
    except Exception:
        return (0,)


def check_for_update(logger) -> Optional[str]:
    """
    Fetch the latest version from GitHub and return it if newer than the
    running version, or None if already up to date or the check failed.
    """
    try:
        resp = requests.get(GITHUB_RAW_URL, timeout=10)
        resp.raise_for_status()
        for line in resp.text.splitlines():
            if line.startswith("VERSION"):
                latest = line.split("=")[1].strip().strip('"\'  ')
                if _parse_version(latest) > _parse_version(VERSION):
                    return latest
                return None
    except Exception as exc:
        logger.debug("Version check failed: %s", exc)
    return None

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

VALID_STATUSES = {"continuing", "airing", "ended", "canceled", "released"}
VERSION            = "1.6.0"
GITHUB_RAW_URL     = "https://raw.githubusercontent.com/BZ00001/scripts/main/upgradinatorr/upgradinatorr.py"
GITHUB_RELEASE_URL = "https://github.com/BZ00001/scripts/tree/main/upgradinatorr"

DEFAULT_CONFIG: Dict[str, Any] = {
    "dry_run": False,
    "log_level": "INFO",
    "instances": [],
}

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
    return logging.getLogger("upgradinatorr")


class BufferingLogger:
    """Captures log calls in memory so parallel instances don't interleave output.

    Call flush_to(logger) to replay all captured messages to a real logger
    in the order they were recorded, with their original timestamps preserved.
    """

    def __init__(self) -> None:
        self._records: List[tuple] = []

    def _store(self, level: int, msg: str, args: tuple) -> None:
        self._records.append((time.time(), level, msg, args))

    def debug(self, msg: str, *args) -> None:    self._store(logging.DEBUG,   msg, args)
    def info(self, msg: str, *args) -> None:     self._store(logging.INFO,    msg, args)
    def warning(self, msg: str, *args) -> None:  self._store(logging.WARNING, msg, args)
    def error(self, msg: str, *args) -> None:    self._store(logging.ERROR,   msg, args)
    def exception(self, msg: str, *args) -> None: self._store(logging.ERROR,  msg, args)

    def flush_to(self, logger: logging.Logger) -> None:
        for created, level, msg, args in self._records:
            if not logger.isEnabledFor(level):
                continue
            record = logger.makeRecord(
                logger.name, level, "(instance)", 0, msg, args, None
            )
            record.created = created
            record.msecs = (created - int(created)) * 1000
            logger.handle(record)


# ─────────────────────────────────────────────────────────────────────────────
# ARR API client
# ─────────────────────────────────────────────────────────────────────────────

class ArrClient:
    """Minimal Radarr / Sonarr API client."""

    MAX_RETRIES = 3

    def __init__(self, url: str, api_key: str, instance_type: str, name: str) -> None:
        self.base = url.rstrip("/")
        self.api_key = api_key
        self.instance_type = instance_type.lower()   # "radarr" or "sonarr"
        self.name = name
        self._local = threading.local()

    # ── internal helpers ──────────────────────────────────────────────────────

    @property
    def session(self) -> requests.Session:
        """A per-thread session so the client is safe to use from thread pools."""
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update({"X-Api-Key": self.api_key, "Content-Type": "application/json"})
            self._local.session = s
        return s

    def _url(self, path: str) -> str:
        return f"{self.base}/api/v3/{path.lstrip('/')}"

    def _request(self, method: str, path: str, **kwargs) -> Any:
        """Issue an HTTP request with retry and exponential back-off."""
        kwargs.setdefault("timeout", 30)
        last_exc: Exception = RuntimeError("no attempts made")
        for attempt in range(self.MAX_RETRIES):
            try:
                r = self.session.request(method, self._url(path), **kwargs)
                r.raise_for_status()
                if r.content:
                    return r.json()
                return None
            except Exception as exc:
                last_exc = exc
                if attempt < self.MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)  # 1s, 2s, 4s
        raise last_exc

    def _get(self, path: str, params: Optional[Dict] = None) -> Any:
        return self._request("GET", path, params=params)

    def _post(self, path: str, body: Dict) -> Any:
        return self._request("POST", path, json=body)

    def _put(self, path: str, body: Dict) -> Any:
        return self._request("PUT", path, json=body)

    # ── connection check ──────────────────────────────────────────────────────

    def ping(self) -> bool:
        try:
            self._get("system/status")
            return True
        except Exception as exc:
            logging.getLogger("upgradinatorr").error(
                "Cannot connect to %s (%s): %s", self.name, self.base, exc
            )
            return False

    # ── tags ─────────────────────────────────────────────────────────────────

    def get_tag_id(self, label: str) -> int:
        """Return tag ID for *label*, creating the tag if it doesn't exist."""
        tags = self._get("tag")
        for t in tags:
            if t["label"].lower() == label.lower():
                return t["id"]
        # Create it
        new_tag = self._post("tag", {"label": label})
        return new_tag["id"]

    def _media_endpoint(self) -> str:
        return "movie" if self.instance_type == "radarr" else "series"

    def _get_item(self, media_id: int) -> Dict:
        return self._get(f"{self._media_endpoint()}/{media_id}")

    def _put_item(self, media_id: int, body: Dict) -> None:
        self._put(f"{self._media_endpoint()}/{media_id}", body)

    def add_tag(self, media_id: int, tag_id: int) -> None:
        item = self._get_item(media_id)
        if tag_id not in item.get("tags", []):
            item["tags"].append(tag_id)
            self._put_item(media_id, item)

    def remove_tag(self, media_id: int, tag_id: int) -> None:
        item = self._get_item(media_id)
        if tag_id in item.get("tags", []):
            item["tags"] = [t for t in item["tags"] if t != tag_id]
            self._put_item(media_id, item)

    def remove_tag_from_all(self, media_ids: List[int], tag_id: int) -> None:
        def _remove(mid: int) -> None:
            self.remove_tag(mid, tag_id)
        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(_remove, media_ids))

    # ── media retrieval ───────────────────────────────────────────────────────

    def get_parsed_media(self) -> List[Dict]:
        """
        Return a normalised list of media items.

        Each item has:
          media_id, title, year, monitored, status, tags, is_radarr,
          seasons (None for Radarr, list of dicts for Sonarr)
        """
        if self.instance_type == "radarr":
            raw = self._get("movie")
            return [
                {
                    "media_id": m["id"],
                    "title": m.get("title", "Unknown"),
                    "year": m.get("year", 0),
                    "monitored": m.get("monitored", False),
                    "status": m.get("status", ""),
                    "tags": m.get("tags", []),
                    "is_radarr": True,
                    "seasons": None,
                }
                for m in raw
            ]
        else:  # sonarr – fetch all episode data upfront in parallel
            raw = self._get("series")
            result = [None] * len(raw)
            with ThreadPoolExecutor(max_workers=10) as pool:
                future_to_index = {
                    pool.submit(self.fetch_episode_data, s["id"], s.get("seasons", [])): (i, s)
                    for i, s in enumerate(raw)
                }
                for future in as_completed(future_to_index):
                    i, s = future_to_index[future]
                    try:
                        seasons = future.result()
                    except Exception as exc:
                        logging.getLogger("upgradinatorr").warning(
                            "Could not fetch episodes for %s: %s", s.get("title"), exc
                        )
                        seasons = []
                    result[i] = {
                        "media_id": s["id"],
                        "title": s.get("title", "Unknown"),
                        "year": s.get("year", 0),
                        "monitored": s.get("monitored", False),
                        "status": s.get("status", ""),
                        "tags": s.get("tags", []),
                        "is_radarr": False,
                        "seasons": seasons,
                    }
            return result

    def fetch_episode_data(self, series_id: int, seasons_raw: List[Dict]) -> List[Dict]:
        """Fetch episode list for one series and structure it by season.

        Safe to call from multiple threads (per-thread session) and retries
        on transient failures via the shared _get helper.
        """
        episodes = self._get("episode", params={"seriesId": series_id})

        episodes_by_season: Dict[int, List[Dict]] = {}
        for ep in episodes:
            sn = ep.get("seasonNumber", 0)
            episodes_by_season.setdefault(sn, []).append(ep)

        seasons = []
        for season in seasons_raw:
            sn = season["seasonNumber"]
            if sn == 0:   # skip specials
                continue
            season_episodes = episodes_by_season.get(sn, [])
            seasons.append(
                {
                    "season_number": sn,
                    "monitored": season.get("monitored", False),
                    "episode_data": [
                        {"monitored": ep.get("monitored", False)}
                        for ep in season_episodes
                    ],
                }
            )
        return seasons

    # ── commands / search ─────────────────────────────────────────────────────

    def search_media(self, media_id: int) -> Dict:
        if self.instance_type == "radarr":
            body = {"name": "MoviesSearch", "movieIds": [media_id]}
        else:
            body = {"name": "SeriesSearch", "seriesId": media_id}
        return self._post("command", body)

    def search_season(self, series_id: int, season_number: int) -> Dict:
        body = {
            "name": "SeasonSearch",
            "seriesId": series_id,
            "seasonNumber": season_number,
        }
        return self._post("command", body)

    # ── history ───────────────────────────────────────────────────────────────

    def get_history_grabs(
        self, media_id: int, since: datetime.datetime
    ) -> List[Dict]:
        """Return releases grabbed for this item since *since* (timezone-aware UTC)."""
        id_key = "movieId" if self.instance_type == "radarr" else "seriesId"
        # Don't filter by eventType in the API call — Radarr/Sonarr versions
        # differ on whether they accept a string or numeric value. Filter in code.
        params = {id_key: media_id, "pageSize": 50}
        try:
            data = self._get("history", params=params)
        except Exception:
            return []
        result = []
        seen: set = set()
        for r in data.get("records", []):
            if r.get("eventType") != "grabbed":
                continue
            if r.get(id_key) != media_id:
                continue
            date_str = r.get("date", "")
            try:
                dt = datetime.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=datetime.timezone.utc)
            except Exception:
                continue
            if dt < since:
                continue
            quality = (
                r.get("quality", {}).get("quality", {}).get("name") or "?"
            )
            title = r.get("sourceTitle", "Unknown")
            if title in seen:
                continue
            seen.add(title)
            result.append({
                "title": title,
                "quality": quality,
            })
        return result


# ─────────────────────────────────────────────────────────────────────────────
# Core logic  (ported from the daps module, minus the daps framework)
# ─────────────────────────────────────────────────────────────────────────────

def filter_media(
    media_list: List[Dict],
    checked_tag_id: int,
    ignore_tag_id: Optional[int],
    count: int,
    season_monitored_threshold: float,
    logger: logging.Logger,
) -> List[Dict]:
    filtered: List[Dict] = []

    for item in media_list:
        if len(filtered) >= count:
            break

        # Skip tagged / ignored / unmonitored / bad status
        reasons = []
        if checked_tag_id in item["tags"]:
            reasons.append("already tagged")
        if ignore_tag_id and ignore_tag_id in item["tags"]:
            reasons.append("ignore tag")
        if not item["monitored"]:
            reasons.append("unmonitored")
        if item["status"] not in VALID_STATUSES:
            reasons.append(f"status={item['status']!r}")
        if reasons:
            logger.debug(
                "Skipping %s (%s): %s", item["title"], item["year"], ", ".join(reasons)
            )
            continue

        # For Sonarr: apply season-level monitored threshold
        if not item["is_radarr"]:
            any_monitored_season = False
            for i, season in enumerate(item.get("seasons") or []):
                eps = season["episode_data"]
                if not eps:
                    continue
                monitored_pct = (
                    sum(1 for e in eps if e["monitored"]) / len(eps)
                ) * 100
                if monitored_pct < season_monitored_threshold:
                    item["seasons"][i]["monitored"] = False
                    logger.debug(
                        "%s S%02d: unmonitored (%.0f%% < threshold %.0f%%)",
                        item["title"],
                        season["season_number"],
                        monitored_pct,
                        season_monitored_threshold,
                    )
                if item["seasons"][i]["monitored"]:
                    any_monitored_season = True

            if not any_monitored_season:
                logger.debug(
                    "Skipping %s (%s): no monitored seasons above threshold",
                    item["title"],
                    item["year"],
                )
                continue

        filtered.append(item)
        logger.info(
            "Queued: %s (%s) [ID %s]", item["title"], item["year"], item["media_id"]
        )

    return filtered


def has_threshold_blocked_items(
    media_list: List[Dict],
    checked_tag_id: int,
    ignore_tag_id: Optional[int],
    season_monitored_threshold: float,
) -> bool:
    """
    Return True if any untagged Sonarr item passes the basic eligibility
    checks (monitored, valid status, not ignored) but is excluded solely
    because no season currently meets the monitored threshold.

    These items could become eligible later as more episodes are monitored,
    so an unattended reset should be deferred while they exist - resetting
    would not help them and could cause premature/early cycle restarts.
    """
    for item in media_list:
        if checked_tag_id in item["tags"]:
            continue
        if ignore_tag_id and ignore_tag_id in item["tags"]:
            continue
        if not item["monitored"]:
            continue
        if item["status"] not in VALID_STATUSES:
            continue
        if item["is_radarr"]:
            continue

        any_monitored_season = False
        for season in item.get("seasons") or []:
            eps = season["episode_data"]
            if not eps:
                continue
            monitored_pct = (sum(1 for e in eps if e["monitored"]) / len(eps)) * 100
            if monitored_pct >= season_monitored_threshold and season["monitored"]:
                any_monitored_season = True
                break

        if not any_monitored_season:
            return True

    return False


def process_instance(
    app: ArrClient,
    settings: Dict,
    dry_run: bool,
    logger: logging.Logger,
) -> Optional[Dict]:
    count: int = settings.get("count", 2)
    checked_tag_name: str = settings.get("tag_name", "checked")
    ignore_tag_name: Optional[str] = settings.get("ignore_tag")
    unattended: bool = settings.get("unattended", False)
    season_threshold: float = settings.get("season_monitored_threshold", 1.0) or 1.0
    wait_for_commands: bool = settings.get("wait_for_commands", False)
    command_timeout: int = settings.get("command_timeout", 60)
    history_check_delay: int = settings.get("history_check_delay", 15)
    history_check_delay_per_item: int = settings.get("history_check_delay_per_item", 10)
    reset_when_blocked: bool = settings.get("reset_when_blocked", True)

    logger.info("── %s (%s) ──────────────────────────────────", app.name, app.instance_type)

    checked_tag_id = app.get_tag_id(checked_tag_name)
    ignore_tag_id = app.get_tag_id(ignore_tag_name) if ignore_tag_name else None

    media_list = app.get_parsed_media()

    filtered = filter_media(
        media_list, checked_tag_id, ignore_tag_id, count, season_threshold, logger,
    )

    # Unattended: if nothing left to search, wipe tags and start fresh.
    #
    # An empty `filtered` result can mean different things:
    #  1. Every item has been tagged this cycle -> safe to reset.
    #  2. Some untagged items remain but are permanently/long-term ineligible
    #     (unmonitored, wrong status like 'announced', ignore tag) -> also
    #     safe to reset, since these items will be skipped again regardless
    #     and would otherwise block the cycle from ever restarting.
    #  3. Some untagged Sonarr items remain that are excluded *only* because
    #     no season currently meets the season_monitored_threshold.
    #
    # By default (reset_when_blocked: true) the cycle resets in all three
    # cases so it never stalls. Set reset_when_blocked: false to defer the
    # reset while case 3 items exist (they may become eligible later as more
    # episodes are monitored).
    blocking = (
        not reset_when_blocked
        and has_threshold_blocked_items(
            media_list, checked_tag_id, ignore_tag_id, season_threshold
        )
    )
    if not filtered and unattended and not blocking:
        logger.info("Nothing left to search – clearing tags for unattended cycle.")
        all_ids = [m["media_id"] for m in media_list]
        if not dry_run:
            app.remove_tag_from_all(all_ids, checked_tag_id)
        media_list = app.get_parsed_media()
        filtered = filter_media(
            media_list, checked_tag_id, ignore_tag_id, count, season_threshold, logger,
        )

    tagged_count = sum(1 for m in media_list if checked_tag_id in m["tags"])
    output = {
        "server_name": app.name,
        "tagged_count": tagged_count,
        "untagged_count": len(media_list) - tagged_count,
        "total_count": len(media_list),
        "data": [],
    }

    if not filtered:
        logger.info("Nothing to process for %s.", app.name)
        return output

    searched_ids: List[int] = []

    if not dry_run:
        pending_commands: List[Dict] = []   # {command_id, media_id, title, year}
        search_start = datetime.datetime.now(datetime.timezone.utc)

        # ── Phase 1: fire all search commands ─────────────────────────────────
        for item in filtered:
            mid = item["media_id"]
            logger.debug("━" * 60)
            logger.debug("Processing: %s (%s) [ID %s]", item["title"], item["year"], mid)

            if item["is_radarr"]:
                # Radarr
                resp = app.search_media(mid)
                if resp:
                    logger.debug("Command ID %s – dispatched", resp.get("id"))
                    pending_commands.append(
                        {"command_id": resp["id"], "media_id": mid,
                         "title": item["title"], "year": item["year"]}
                    )
                searched_ids.append(mid)
            else:
                # Sonarr – one command per monitored season
                searched = False
                for season in item["seasons"]:
                    if season["monitored"]:
                        logger.debug("  Searching S%02d…", season["season_number"])
                        resp = app.search_season(mid, season["season_number"])
                        if resp:
                            pending_commands.append(
                                {"command_id": resp["id"], "media_id": mid,
                                 "title": item["title"], "year": item["year"]}
                            )
                        searched = True
                if searched:
                    searched_ids.append(mid)

        # ── Phase 2: optionally wait for commands (single-threaded poll) ──────
        # A single polling loop avoids sharing the requests.Session across
        # threads (which is not safe).
        if wait_for_commands and pending_commands:
            remaining = {cmd["command_id"] for cmd in pending_commands}
            deadline = time.time() + command_timeout
            while remaining and time.time() < deadline:
                for cmd_id in list(remaining):
                    try:
                        state = app._get(f"command/{cmd_id}").get("state", "").lower()
                        if state in ("completed", "failed", "aborted"):
                            remaining.discard(cmd_id)
                    except Exception:
                        remaining.discard(cmd_id)
                if remaining:
                    time.sleep(5)

        # ── Phase 3: tag all searched items ───────────────────────────────────
        for item in filtered:
            if item["media_id"] in searched_ids:
                app.add_tag(item["media_id"], checked_tag_id)
                logger.info("Done: %s (%s)", item["title"], item["year"])

        # ── Phase 4: check history for grabbed releases (only when waited) ────
        grabs_by_id: Dict[int, List[Dict]] = {}
        if wait_for_commands and pending_commands:
            scaled_delay = history_check_delay + history_check_delay_per_item * len(searched_ids)
            if scaled_delay > 0:
                logger.debug("Waiting %ds before checking grab history…", scaled_delay)
                time.sleep(scaled_delay)
            for mid in searched_ids:
                grabs_by_id[mid] = app.get_history_grabs(mid, since=search_start)

        for item in filtered:
            if item["media_id"] not in searched_ids:
                continue
            grabs = grabs_by_id.get(item["media_id"]) if wait_for_commands else None
            output["data"].append(
                {
                    "media_id": item["media_id"],
                    "title": item["title"],
                    "year": item["year"],
                    "grabs": grabs,   # None = not checked; [] = nothing grabbed; [...] = grabbed
                }
            )
    else:
        # Dry-run: just list what would be processed
        for item in filtered:
            output["data"].append(
                {
                    "media_id": item["media_id"],
                    "title": item["title"],
                    "year": item["year"],
                    "grabs": None,
                }
            )

    return output


def print_output(results: Dict[str, Optional[Dict]], logger: logging.Logger) -> None:
    for instance_name, data in results.items():
        if not data:
            logger.info("[%s] No results.", instance_name)
            continue
        logger.info(
            "[%s] Tagged: %d / %d total",
            data["server_name"],
            data["tagged_count"],
            data["total_count"],
        )
        for item in data.get("data", []):
            logger.info("  %s (%s)", item["title"], item["year"])
            grabs = item.get("grabs")
            if grabs is None:
                pass  # not checked (wait_for_commands: false or dry-run)
            elif grabs:
                for grab in grabs:
                    logger.info("    ↳ Grabbed: %s  [%s]", grab["title"], grab["quality"])
            else:
                logger.info("    ↳ Nothing grabbed (no upgrade found).")


# ─────────────────────────────────────────────────────────────────────────────
# Discord notifications
# ─────────────────────────────────────────────────────────────────────────────

EMBED_COLOR = 0x4F91C7   # Radarr blue-ish
DIVIDER = "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬"
NOTHING_TO_PROCESS = (
    "*Nothing to process.* All remaining untagged items are either "
    "unmonitored, not yet available (announced/in cinemas/upcoming), "
    "or don't meet the season monitored threshold."
)


def build_instance_fields(data: Dict) -> List[Dict]:
    """Build the Discord embed field for a single instance result.

    Returns a single-field list. The divider is appended to the field value
    so it renders directly under the content with no blank line on either side;
    the caller strips the divider from the final field.
    """
    tagged = data.get("tagged_count", 0)
    total = data.get("total_count", 0)
    name = f"{data['server_name']}  ({tagged}/{total} tagged)"

    if not data.get("data"):
        value = NOTHING_TO_PROCESS
    else:
        lines = []
        for item in data["data"]:
            line = f"**{item['title']}** ({item['year']})"
            grabs = item.get("grabs")
            if grabs is None:
                pass  # not checked (wait_for_commands: false or dry-run)
            elif grabs:
                for grab in grabs:
                    short = grab["title"][:60] + "…" if len(grab["title"]) > 60 else grab["title"]
                    line += f"\n　↳ Grabbed: {short}  `{grab['quality']}`"
            else:
                line += "\n　↳ *Nothing grabbed (no upgrade found)*"
            lines.append(line)
        value = "\n\n".join(lines)

    # Append divider so it sits directly under the content. Reserve room
    # for it within the 1024-char field limit.
    divider_suffix = f"\n{DIVIDER}"
    max_content = 1024 - len(divider_suffix)
    if len(value) > max_content:
        value = value[:max_content - 2] + "\n…"
    value += divider_suffix

    return [{"name": name, "value": value, "inline": False}]


def send_discord_notification(
    webhook_url: str,
    results: Dict[str, Optional[Dict]],
    dry_run: bool,
    logger: logging.Logger,
    latest_version: Optional[str] = None,
) -> None:
    """
    Post a summary embed to a Discord webhook.

    One embed field per instance. Skips the notification entirely if there
    is nothing to report across all instances.
    """
    fields: List[Dict] = []

    for instance_name, data in results.items():
        if not data:
            continue
        fields.extend(build_instance_fields(data))

    if not fields:
        logger.debug("Discord: nothing to report, skipping notification.")
        return

    # Remove trailing divider from the last instance field
    if fields:
        last = fields[-1]
        if last.get("value", "").endswith(f"\n{DIVIDER}"):
            last["value"] = last["value"][: -len(f"\n{DIVIDER}")]

    if latest_version:
        fields.insert(0, {
            "name":   "🆕 Update available",
            "value":  f"v{VERSION} → v{latest_version}\n[Download from GitHub]({GITHUB_RELEASE_URL})",
            "inline": False,
        })

    title = "🔍 Upgradinatorr"
    if dry_run:
        title += "  `[DRY RUN]`"

    # Discord allows at most 25 fields per embed. If we exceed that (many
    # instances, dividers, update notice), trim and append a notice.
    if len(fields) > 25:
        fields = fields[:24]
        fields.append({
            "name": "\u2800",
            "value": "*Output truncated - too many fields for one Discord message.*",
            "inline": False,
        })

    embed = {
        "title": title,
        "color": EMBED_COLOR,
        "fields": fields,
        "footer": {"text": f"upgradinatorr v{VERSION}"},
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

    payload = {"embeds": [embed]}

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
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
    merged = {**DEFAULT_CONFIG, **data}
    return merged


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Upgradinatorr – standalone upgrade searcher")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).with_name("upgradinatorr.yml"),
        help="Path to YAML config file (default: upgradinatorr.yml next to this script)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Log actions without making any changes")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "--instance",
        action="append",
        metavar="NAME",
        help="Only process the named instance (repeatable). Matches the 'name' field, case-insensitive.",
    )
    args = parser.parse_args()

    config = load_config(args.config)

    log_level = "DEBUG" if args.debug else config.get("log_level", "INFO")
    logger = setup_logging(log_level)
    logger.info("upgradinatorr v%s", VERSION)

    latest_version = check_for_update(logger)
    if latest_version:
        logger.info("═" * 50)
        logger.info("Update available: v%s → v%s", VERSION, latest_version)
        logger.info("Download: %s", GITHUB_RELEASE_URL)
        logger.info("═" * 50)

    dry_run: bool = args.dry_run or config.get("dry_run", False)

    if dry_run:
        logger.info("═" * 50)
        logger.info("DRY RUN – no changes will be made")
        logger.info("═" * 50)

    instances = config.get("instances", [])
    if not instances:
        logger.error("No instances defined in config. Exiting.")
        sys.exit(1)

    # Optional --instance filter
    only = {n.lower() for n in args.instance} if args.instance else None
    if only:
        instances = [i for i in instances if i.get("name", "").lower() in only]
        if not instances:
            logger.error("No configured instances match --instance %s. Exiting.", ", ".join(sorted(only)))
            sys.exit(1)

    # Build the list of valid, reachable instances first
    clients: List[tuple] = []
    for inst in instances:
        name = inst.get("name", "Unknown")
        inst_type = inst.get("type", "").lower()
        url = inst.get("url", "")
        api_key = inst.get("api_key", "")

        if inst_type not in ("radarr", "sonarr"):
            logger.warning("Instance %s: unknown type %r – skipping.", name, inst_type)
            continue
        if not url:
            logger.warning("Instance %s: missing url – skipping.", name)
            continue
        if not api_key:
            logger.warning("Instance %s: missing api_key – skipping.", name)
            continue

        app = ArrClient(url, api_key, inst_type, name)
        if not app.ping():
            continue

        clients.append((name, app, inst))

    all_results: Dict[str, Optional[Dict]] = {}

    def _run(name: str, app: ArrClient, inst: Dict) -> tuple:
        buf = BufferingLogger()
        try:
            result = process_instance(app, inst, dry_run, buf)
        except Exception:
            buf.exception("Error processing instance %s", name)
            result = None
        return name, result, buf

    # Submit all instances in parallel; each writes to its own buffer
    completed: Dict[int, tuple] = {}
    with ThreadPoolExecutor(max_workers=len(clients) or 1) as pool:
        ordered_futures = [pool.submit(_run, name, app, inst) for name, app, inst in clients]
        future_index = {f: i for i, f in enumerate(ordered_futures)}
        for future in as_completed(ordered_futures):
            name, result, buf = future.result()
            completed[future_index[future]] = (name, result, buf)

    # Flush buffered output in config order (Radarr first, then Sonarr, etc.)
    for i in range(len(clients)):
        name, result, buf = completed[i]
        buf.flush_to(logger)
        all_results[name] = result

    if all_results:
        logger.info("")
        logger.info("─" * 50)
        logger.info("Summary")
        logger.info("─" * 50)
        print_output(all_results, logger)

    webhook_url = config.get("discord_webhook")
    if webhook_url and all_results:
        send_discord_notification(webhook_url, all_results, dry_run, logger, latest_version)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(0)
