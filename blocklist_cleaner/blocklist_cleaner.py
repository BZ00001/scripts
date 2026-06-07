#!/usr/bin/env python3
"""
blocklist_cleaner.py
Removes blocklist entries older than a configured number of days from Sonarr and Radarr.

Part of the natorr script collection.
https://github.com/YOUR_USERNAME/natorr
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

# ============================================================
# CONFIGURATION - edit blocklist_cleaner.yml instead
# ============================================================

DEFAULT_CONFIG_PATH = Path(__file__).parent / "blocklist_cleaner.yml"
VERSION = "1.0.1"

# ============================================================
# LOGGING
# ============================================================

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt=DATE_FORMAT)
log = logging.getLogger("blocklist_cleaner")


# ============================================================
# ARR CLIENT
# ============================================================

class ArrClient:
    def __init__(self, url: str, api_key: str, name: str):
        self.base_url = url.rstrip("/")
        self.api_key = api_key
        self.name = name
        self.session = requests.Session()
        self.session.headers.update({"X-Api-Key": api_key})

    def _get(self, endpoint: str, params: dict = None) -> dict:
        url = f"{self.base_url}/api/v3/{endpoint}"
        response = self.session.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def _delete_bulk(self, ids: list[int]) -> None:
        url = f"{self.base_url}/api/v3/blocklist/bulk"
        response = self.session.delete(url, json={"ids": ids}, timeout=30)
        response.raise_for_status()

    def get_blocklist(self) -> list[dict]:
        """Fetch all blocklist entries, handling pagination."""
        entries = []
        page = 1
        page_size = 1000

        while True:
            log.debug("[%s] Fetching blocklist page %d", self.name, page)
            data = self._get("blocklist", params={
                "page": page,
                "pageSize": page_size,
                "sortKey": "date",
                "sortDirection": "descending",
            })

            records = data.get("records", [])
            entries.extend(records)

            total = data.get("totalRecords", 0)
            log.debug("[%s] Retrieved %d / %d entries", self.name, len(entries), total)

            if len(entries) >= total or not records:
                break

            page += 1

        return entries

    def delete_bulk(self, ids: list[int], dry_run: bool) -> int:
        """Delete entries by ID. Returns number deleted (or would delete)."""
        if not ids:
            return 0

        if dry_run:
            log.info("[%s] DRY-RUN: would delete %d blocklist entries", self.name, len(ids))
            return len(ids)

        # Sonarr/Radarr bulk delete has no hard limit documented, but batch to be safe
        batch_size = 500
        deleted = 0
        for i in range(0, len(ids), batch_size):
            batch = ids[i:i + batch_size]
            self._delete_bulk(batch)
            deleted += len(batch)
            log.debug("[%s] Deleted batch of %d entries", self.name, len(batch))

        return deleted


# ============================================================
# DISCORD
# ============================================================

def send_discord_notification(webhook_url: str, results: list[dict], dry_run: bool, days: int) -> None:
    if not webhook_url:
        return

    dry_run_prefix = "[DRY-RUN] " if dry_run else ""
    color = 0xF5A623 if dry_run else 0x2ECC71  # orange for dry-run, green for real

    fields = []
    total_deleted = 0
    total_skipped = 0

    for r in results:
        deleted = r["deleted"]
        skipped = r["skipped"]
        total = r["total"]
        total_deleted += deleted
        total_skipped += skipped

        verb = "Would remove" if dry_run else "Removed"
        fields.append({
            "name": r["name"],
            "value": (
                f"{verb} **{deleted}** entr{'y' if deleted == 1 else 'ies'}\n"
                f"Kept: {skipped} | Total scanned: {total}"
            ),
            "inline": True,
        })

    embed = {
        "title": f"{dry_run_prefix}Blocklist Cleaner",
        "description": f"Entries older than **{days} day{'s' if days != 1 else ''}** processed.",
        "color": color,
        "fields": fields,
        "footer": {"text": f"blocklist_cleaner v{VERSION} - Total removed: {total_deleted} | Total kept: {total_skipped}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    payload = {"embeds": [embed]}

    try:
        response = requests.post(webhook_url, json=payload, timeout=10)
        response.raise_for_status()
        log.debug("Discord notification sent")
    except requests.RequestException as exc:
        log.warning("Failed to send Discord notification: %s", exc)


# ============================================================
# CORE LOGIC
# ============================================================

def parse_date(date_str: str) -> datetime:
    """Parse ISO 8601 date string from Sonarr/Radarr API, returning UTC-aware datetime."""
    # API returns e.g. "2024-03-15T12:34:56Z" or "2024-03-15T12:34:56.123456Z"
    date_str = date_str.rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Unable to parse date: {date_str!r}")


def process_instance(client: ArrClient, cutoff: datetime, dry_run: bool) -> dict:
    """Fetch blocklist, identify old entries, and delete them. Returns summary dict."""
    log.info("[%s] Fetching blocklist...", client.name)

    try:
        entries = client.get_blocklist()
    except requests.RequestException as exc:
        log.error("[%s] Failed to fetch blocklist: %s", client.name, exc)
        return {"name": client.name, "total": 0, "deleted": 0, "skipped": 0, "error": str(exc)}

    log.info("[%s] Found %d total blocklist entries", client.name, len(entries))

    old_ids = []
    skipped = 0

    for entry in entries:
        date_str = entry.get("date") or entry.get("Date")
        if not date_str:
            log.debug("[%s] Entry %s has no date, skipping", client.name, entry.get("id"))
            skipped += 1
            continue

        try:
            entry_date = parse_date(date_str)
        except ValueError as exc:
            log.debug("[%s] Could not parse date for entry %s: %s", client.name, entry.get("id"), exc)
            skipped += 1
            continue

        if entry_date < cutoff:
            title = entry.get("sourceTitle") or entry.get("movie", {}).get("title") or entry.get("series", {}).get("title") or "Unknown"
            log.debug("[%s] Marking for deletion: [%s] %s", client.name, entry_date.date(), title)
            old_ids.append(entry["id"])
        else:
            skipped += 1

    log.info("[%s] %d entries older than cutoff, %d within threshold", client.name, len(old_ids), skipped)

    try:
        deleted = client.delete_bulk(old_ids, dry_run)
    except requests.RequestException as exc:
        log.error("[%s] Failed to delete entries: %s", client.name, exc)
        return {"name": client.name, "total": len(entries), "deleted": 0, "skipped": skipped, "error": str(exc)}

    if not dry_run and deleted:
        log.info("[%s] Deleted %d old blocklist entries", client.name, deleted)

    return {
        "name": client.name,
        "total": len(entries),
        "deleted": deleted,
        "skipped": skipped,
        "error": None,
    }


# ============================================================
# CONFIG
# ============================================================

def load_config(path: Path) -> dict:
    if not path.exists():
        log.error("Config file not found: %s", path)
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="Remove old blocklist entries from Sonarr/Radarr")
    parser.add_argument("--dry-run", action="store_true", help="Log what would be deleted without making changes")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug logging")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Path to YAML config file")
    args = parser.parse_args()

    if args.debug:
        log.setLevel(logging.DEBUG)

    config = load_config(args.config)

    dry_run = args.dry_run or config.get("dry_run", True)
    days = int(config.get("days", 30))
    discord_webhook = config.get("discord_webhook", "")

    log.info("blocklist_cleaner v%s", VERSION)

    if dry_run:
        log.info("DRY-RUN mode enabled - no changes will be made")

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    log.info("Removing blocklist entries older than %d day%s (before %s)", days, "s" if days != 1 else "", cutoff.date())

    instances_cfg = config.get("instances", [])
    if not instances_cfg:
        log.error("No instances configured. Check your blocklist_cleaner.yml.")
        sys.exit(1)

    results = []

    for inst in instances_cfg:
        name = inst.get("name", "Unknown")
        url = inst.get("url", "")
        api_key = inst.get("api_key", "")

        if not url or not api_key:
            log.warning("Instance '%s' missing url or api_key, skipping", name)
            continue

        client = ArrClient(url=url, api_key=api_key, name=name)
        result = process_instance(client, cutoff, dry_run)
        results.append(result)

    # Summary
    log.info("=" * 50)
    log.info("SUMMARY")
    log.info("=" * 50)
    for r in results:
        if r.get("error"):
            log.error("[%s] Error: %s", r["name"], r["error"])
        else:
            verb = "Would delete" if dry_run else "Deleted"
            log.info("[%s] %s %d of %d entries", r["name"], verb, r["deleted"], r["total"])

    if discord_webhook:
        send_discord_notification(discord_webhook, results, dry_run, days)


if __name__ == "__main__":
    main()
