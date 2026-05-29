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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import yaml

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

VALID_STATUSES = {"continuing", "airing", "ended", "canceled", "released"}

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

    def __init__(self, url: str, api_key: str, instance_type: str, name: str) -> None:
        self.base = url.rstrip("/")
        self.api_key = api_key
        self.instance_type = instance_type.lower()   # "radarr" or "sonarr"
        self.name = name
        self.session = requests.Session()
        self.session.headers.update({"X-Api-Key": api_key, "Content-Type": "application/json"})

    # ── internal helpers ──────────────────────────────────────────────────────

    def _url(self, path: str) -> str:
        return f"{self.base}/api/v3/{path.lstrip('/')}"

    def _get(self, path: str, params: Optional[Dict] = None) -> Any:
        r = self.session.get(self._url(path), params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: Dict) -> Any:
        r = self.session.post(self._url(path), json=body, timeout=30)
        r.raise_for_status()
        return r.json()

    def _put(self, path: str, body: Dict) -> Any:
        r = self.session.put(self._url(path), json=body, timeout=30)
        r.raise_for_status()
        return r.json()

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
        """Fetch episode list for one series.

        Uses its own session so it is safe to call from multiple threads.
        """
        session = requests.Session()
        session.headers.update({"X-Api-Key": self.api_key, "Content-Type": "application/json"})
        url = f"{self.base}/api/v3/episode"
        r = session.get(url, params={"seriesId": series_id}, timeout=30)
        r.raise_for_status()
        episodes = r.json()

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

    def wait_for_command(self, command_id: int, timeout: int = 120) -> bool:
        """Poll until the command completes or timeout (seconds) is reached."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                cmd = self._get(f"command/{command_id}")
                state = cmd.get("state", "")
                if state == "completed":
                    return True
                if state in ("failed", "aborted"):
                    return False
            except Exception:
                pass
            time.sleep(3)
        return False

    # ── history ───────────────────────────────────────────────────────────────

    def get_history_grabs(
        self, media_id: int, since: datetime.datetime
    ) -> List[Dict]:
        """Return releases grabbed for this item since *since* (UTC naive)."""
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
            date_str = r.get("date", "")
            try:
                dt = datetime.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                dt_naive = dt.replace(tzinfo=None)
            except Exception:
                continue
            if dt_naive < since:
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

    # ── queue ─────────────────────────────────────────────────────────────────

    def get_queue(self) -> Dict:
        params = {
            "pageSize": 1000,
            "includeUnknownMovieItems": "false",
            "includeUnknownSeriesItems": "false",
        }
        return self._get("queue", params=params)


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


def process_queue(
    queue: Dict, instance_type: str, media_ids: List[int]
) -> List[Dict]:
    id_key = "movieId" if instance_type == "radarr" else "seriesId"
    seen = set()
    result = []
    for record in queue.get("records", []):
        mid = record.get(id_key)
        if mid not in media_ids or "downloadId" not in record:
            continue
        key = (record["downloadId"], mid)
        if key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "download_id": record["downloadId"],
                "media_id": mid,
                "download": record.get("title"),
                "custom_format_score": record.get("customFormatScore"),
            }
        )
    return result


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

    logger.info("── %s (%s) ──────────────────────────────────", app.name, app.instance_type)

    checked_tag_id = app.get_tag_id(checked_tag_name)
    ignore_tag_id = app.get_tag_id(ignore_tag_name) if ignore_tag_name else None

    media_list = app.get_parsed_media()

    filtered = filter_media(
        media_list, checked_tag_id, ignore_tag_id, count, season_threshold, logger,
    )

    # Unattended: if nothing left, wipe tags and start fresh
    if not filtered and unattended:
        logger.info("All media tagged – clearing tags for unattended cycle.")
        all_ids = [m["media_id"] for m in media_list]
        if not dry_run:
            app.remove_tag_from_all(all_ids, checked_tag_id)
        media_list = app.get_parsed_media()
        filtered = filter_media(
            media_list, checked_tag_id, ignore_tag_id, count, season_threshold, logger,
        )

    if not filtered:
        logger.info("Nothing to process for %s.", app.name)
        return None

    tagged_count = sum(1 for m in media_list if checked_tag_id in m["tags"])
    output = {
        "server_name": app.name,
        "tagged_count": tagged_count,
        "untagged_count": len(media_list) - tagged_count,
        "total_count": len(media_list),
        "data": [],
    }

    searched_ids: List[int] = []

    if not dry_run:
        search_count = 0
        pending_commands: List[Dict] = []   # {command_id, media_id, title, year}
        search_start = datetime.datetime.utcnow()

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
                search_count += 1
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
                    search_count += 1
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

# Embed accent colour (Radarr blue-ish)
EMBED_COLOR = 0x4F91C7

def send_discord_notification(
    webhook_url: str,
    results: Dict[str, Optional[Dict]],
    dry_run: bool,
    logger: logging.Logger,
) -> None:
    """
    Post a summary embed to a Discord webhook.

    One embed field per instance. Only instances that actually searched
    something are included. Skips the notification entirely if nothing
    was searched across all instances.
    """
    fields = []

    for instance_name, data in results.items():
        if not data or not data.get("data"):
            continue

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

        # Discord field value limit is 1024 chars – truncate gracefully
        if len(value) > 1024:
            value = value[:1020] + "\n…"

        tagged = data.get("tagged_count", 0)
        total = data.get("total_count", 0)
        name = f"{data['server_name']}  ({tagged}/{total} tagged)"

        fields.append({"name": name, "value": value, "inline": False})

    if not fields:
        logger.debug("Discord: nothing to report, skipping notification.")
        return

    title = "🔍 Upgradinatorr"
    if dry_run:
        title += "  `[DRY RUN]`"

    embed = {
        "title": title,
        "color": EMBED_COLOR,
        "fields": fields,
        "footer": {"text": "upgradinatorr"},
        "timestamp": __import__("datetime").datetime.utcnow().isoformat(),
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
    args = parser.parse_args()

    config = load_config(args.config)

    log_level = "DEBUG" if args.debug else config.get("log_level", "INFO")
    logger = setup_logging(log_level)

    dry_run: bool = args.dry_run or config.get("dry_run", False)

    if dry_run:
        logger.info("═" * 50)
        logger.info("DRY RUN – no changes will be made")
        logger.info("═" * 50)

    instances = config.get("instances", [])
    if not instances:
        logger.error("No instances defined in config. Exiting.")
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
        if not url or not api_key:
            logger.warning("Instance %s: missing url or api_key – skipping.", name)
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
        send_discord_notification(webhook_url, all_results, dry_run, logger)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(0)
