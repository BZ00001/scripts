#!/usr/bin/env python3
"""
asset_cleanup.py
Removes Kometa asset folders/files that have no matching entry in
Radarr (TMDB), Sonarr (TVDB), or Plex collections.

Config: asset_cleanup.yml (same directory as this script)
"""

import re
import sys
import shutil
import logging
import argparse
from pathlib import Path
from datetime import datetime

import requests
import yaml

# ─── Logging ────────────────────────────────────────────────────────────────

# Colours are disabled automatically when stdout is not a TTY
# (e.g. Unraid User Scripts output window).
_IS_TTY = sys.stdout.isatty()

RESET  = "\033[0m"  if _IS_TTY else ""
BOLD   = "\033[1m"  if _IS_TTY else ""
RED    = "\033[91m" if _IS_TTY else ""
GREEN  = "\033[92m" if _IS_TTY else ""
YELLOW = "\033[93m" if _IS_TTY else ""
CYAN   = "\033[96m" if _IS_TTY else ""
DIM    = "\033[2m"  if _IS_TTY else ""

class ColourFormatter(logging.Formatter):
    COLOURS = {
        logging.DEBUG:    DIM,
        logging.INFO:     RESET,
        logging.WARNING:  YELLOW,
        logging.ERROR:    RED,
        logging.CRITICAL: RED + BOLD,
    }
    def format(self, record):
        colour = self.COLOURS.get(record.levelno, RESET)
        msg = super().format(record)
        return f"{colour}{msg}{RESET}" if _IS_TTY else msg

def setup_logging(verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger("asset_cleanup")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(ColourFormatter("%(asctime)s  %(levelname)-8s  %(message)s",
                                         datefmt="%H:%M:%S"))
    logger.addHandler(handler)
    return logger

log = setup_logging()

# ─── Config ─────────────────────────────────────────────────────────────────

SCRIPT_DIR  = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "asset_cleanup.yml"

DEFAULT_CONFIG = {
    "dry_run": True,
    "delete_unknown": False,
    "verbose": False,
    "asset_dirs": [
        "/mnt/user/appdata/kometa/assets",
        "/mnt/user/appdata/kometa/assets/tmp",
    ],
    "radarr":  {"url": "http://localhost:7878", "api_key": "YOUR_RADARR_API_KEY"},
    "sonarr":  {"url": "http://localhost:8989", "api_key": "YOUR_SONARR_API_KEY"},
    "plex":    {"url": "http://localhost:32400", "token": "YOUR_PLEX_TOKEN"},
    "discord": {"webhook_url": "", "notify_on_dry_run": True},
}

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        log.warning(f"Config not found at {CONFIG_PATH}, writing defaults.")
        with open(CONFIG_PATH, "w") as f:
            yaml.dump(DEFAULT_CONFIG, f, default_flow_style=False, sort_keys=False)
        log.error("Please edit the config file and re-run.")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    for key, val in DEFAULT_CONFIG.items():
        cfg.setdefault(key, val)
    return cfg

# ─── Helpers ────────────────────────────────────────────────────────────────

def _normalise_name(name: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation for fuzzy matching."""
    name = name.lower()
    name = re.sub(r"[^\w\s]", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name

def _entry_size(path: Path) -> str:
    """Human-readable size of a file or directory."""
    try:
        if path.is_dir():
            total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        else:
            total = path.stat().st_size
        for unit in ("B", "KB", "MB", "GB"):
            if total < 1024:
                return f"{total:.0f} {unit}"
            total /= 1024
        return f"{total:.1f} TB"
    except Exception:
        return "?"

# ─── API helpers ────────────────────────────────────────────────────────────

def get_radarr_data(url: str, api_key: str) -> tuple[set[int], set[str]]:
    """Return (tmdb_ids, normalised_titles) for all movies in Radarr."""
    resp = requests.get(
        f"{url.rstrip('/')}/api/v3/movie",
        headers={"X-Api-Key": api_key},
        timeout=30,
    )
    resp.raise_for_status()
    movies = resp.json()
    ids    = {m["tmdbId"] for m in movies if m.get("tmdbId")}
    titles = {_normalise_name(m["title"]) for m in movies if m.get("title")}
    log.info(f"Radarr: {len(ids):,} movies loaded")
    return ids, titles


def get_sonarr_data(url: str, api_key: str) -> tuple[set[int], set[str]]:
    """Return (tvdb_ids, normalised_titles) for all series in Sonarr."""
    resp = requests.get(
        f"{url.rstrip('/')}/api/v3/series",
        headers={"X-Api-Key": api_key},
        timeout=30,
    )
    resp.raise_for_status()
    series = resp.json()
    ids    = {s["tvdbId"] for s in series if s.get("tvdbId")}
    titles = {_normalise_name(s["title"]) for s in series if s.get("title")}
    log.info(f"Sonarr: {len(ids):,} series loaded")
    return ids, titles


def get_plex_collection_names(url: str, token: str) -> set[str]:
    """Return a normalised set of all collection names across all libraries."""
    base    = url.rstrip("/")
    headers = {"X-Plex-Token": token, "Accept": "application/json"}

    resp = requests.get(f"{base}/library/sections", headers=headers, timeout=30)
    resp.raise_for_status()
    sections = resp.json()["MediaContainer"].get("Directory", [])

    names: set[str] = set()
    for section in sections:
        key  = section["key"]
        resp = requests.get(
            f"{base}/library/sections/{key}/collections",
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        for col in resp.json()["MediaContainer"].get("Metadata", []):
            title = col.get("title", "").strip()
            if title:
                names.add(_normalise_name(title))

    log.info(f"Plex: {len(names):,} collections loaded")
    return names

# ─── Asset scanning ─────────────────────────────────────────────────────────

# Numeric ID tags  →  {tmdb-12345}  /  {tvdb-12345}
RE_TMDB = re.compile(r"\{tmdb-(\d+)\}", re.IGNORECASE)
RE_TVDB = re.compile(r"\{tvdb-(\d+)\}", re.IGNORECASE)

# Unresolved Kometa placeholders  →  {tmdb-{TmdbId}}  /  {tvdb-{TvdbId}}
RE_TMDB_UNRESOLVED = re.compile(r"\{tmdb-\{[^}]+\}\}", re.IGNORECASE)
RE_TVDB_UNRESOLVED = re.compile(r"\{tvdb-\{[^}]+\}\}", re.IGNORECASE)

# Extract bare title (everything before the year bracket or tag)
RE_TITLE = re.compile(r"^(.+?)(?:\s*\(\d{4}\)|\s*\{)", re.IGNORECASE)


def _extract_title(name: str) -> str:
    """Pull the bare title out of a folder name, normalised."""
    m = RE_TITLE.match(name)
    return _normalise_name(m.group(1) if m else name)


def classify_entry(name: str) -> tuple[str, int | None]:
    """
    Returns one of:
      ('tmdb', id)            — resolved TMDB ID
      ('tvdb', id)            — resolved TVDB ID
      ('tmdb_unresolved', None) — Kometa placeholder {tmdb-{TmdbId}}
      ('tvdb_unresolved', None) — Kometa placeholder {tvdb-{TvdbId}}
      ('collection', None)    — no ID tag at all
    """
    m = RE_TMDB.search(name)
    if m:
        return "tmdb", int(m.group(1))
    m = RE_TVDB.search(name)
    if m:
        return "tvdb", int(m.group(1))
    if RE_TMDB_UNRESOLVED.search(name):
        return "tmdb_unresolved", None
    if RE_TVDB_UNRESOLVED.search(name):
        return "tvdb_unresolved", None
    return "collection", None


def build_ignore_set(ignore_list: list[str]) -> set[str]:
    """Normalise the ignore list from config for consistent matching."""
    return {_normalise_name(name) for name in ignore_list if name}


def scan_asset_dir(
    directory: Path,
    skip_dirs: set[Path],
    radarr_ids: set[int],
    radarr_titles: set[str],
    sonarr_ids: set[int],
    sonarr_titles: set[str],
    plex_names: set[str],
    ignore_set: set[str],
) -> tuple[list[Path], list[Path], list[Path], list[Path]]:
    """
    Scan one asset directory (top level only).

    Returns (keep, remove, unknown, ignored).
      keep    — matched in Radarr, Sonarr or Plex
      remove  — has an ID tag but no match in the relevant app
      unknown — no ID tag, not in Plex, not ignored (kept, flagged for review)
      ignored — explicitly listed in the ignore config (kept silently)
    """
    keep:    list[Path] = []
    remove:  list[Path] = []
    unknown: list[Path] = []
    ignored: list[Path] = []

    if not directory.exists():
        log.warning(f"Asset dir not found, skipping: {directory}")
        return keep, remove, unknown, ignored

    for entry in sorted(directory.iterdir()):
        name = entry.name

        # Skip subdirectories that are themselves scanned as asset_dirs
        if entry.is_dir() and entry.resolve() in skip_dirs:
            log.debug(f"  Skipping nested scan dir: {name}")
            continue

        # Check ignore list first (strip extension for bare-file matches)
        name_stem = Path(name).stem if entry.is_file() else name
        if _normalise_name(name_stem) in ignore_set:
            ignored.append(entry)
            log.debug(f"  Ignored (config): {name}")
            continue

        kind, eid = classify_entry(name)

        if kind == "tmdb":
            (keep if eid in radarr_ids else remove).append(entry)

        elif kind == "tvdb":
            (keep if eid in sonarr_ids else remove).append(entry)

        elif kind == "tmdb_unresolved":
            # Kometa didn't fill in the ID — fall back to title matching
            title = _extract_title(name)
            if title in radarr_titles:
                keep.append(entry)
                log.debug(f"  Matched by title (Radarr): {name}")
            else:
                # Can't determine origin reliably → treat as unknown
                unknown.append(entry)

        elif kind == "tvdb_unresolved":
            # Kometa didn't fill in the ID — fall back to title matching
            title = _extract_title(name)
            if title in sonarr_titles:
                keep.append(entry)
                log.debug(f"  Matched by title (Sonarr): {name}")
            else:
                unknown.append(entry)

        else:  # plain collection name
            normalised = _normalise_name(name)
            (keep if normalised in plex_names else unknown).append(entry)

    return keep, remove, unknown, ignored

# ─── Deletion ───────────────────────────────────────────────────────────────

def delete_entry(path: Path, dry_run: bool) -> bool:
    try:
        if not dry_run:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        return True
    except Exception as exc:
        log.error(f"Failed to delete {path}: {exc}")
        return False

# ─── Discord ────────────────────────────────────────────────────────────────

def send_discord(webhook_url: str, dry_run: bool, delete_unknown: bool,
                 per_dir: list[dict],
                 all_remove: list[Path], all_unknown: list[Path]) -> None:
    if not webhook_url:
        return

    mode   = "🔍 DRY RUN" if dry_run else "🗑️ CLEANUP"
    colour = 0xF4A460 if dry_run else 0xE74C3C
    action = "Would remove" if dry_run else "Removed"

    def _bullet_list(paths: list[Path], limit: int) -> str:
        lines = [f"• {p.name}" for p in paths[:limit]]
        if len(paths) > limit:
            lines.append(f"… and {len(paths) - limit} more")
        return "\n".join(lines) or "—"

    fields = [
        {"name": f"{action} (orphaned)", "value": str(len(all_remove)),   "inline": True},
        {"name": "⚠️ Unknown (" + ("would remove" if delete_unknown and dry_run else "removed" if delete_unknown else "kept") + ")",
         "value": str(len(all_unknown)), "inline": True},
    ]

    # Per-directory breakdown
    for d in per_dir:
        label    = d["label"]
        removed  = d["remove"]
        unknown  = d["unknown"]
        if not removed and not unknown:
            continue
        lines = []
        for p in removed[:10]:
            lines.append(f"✗ {p.name}")
        for p in (unknown if delete_unknown else [])[:5]:
            lines.append(f"✗ {p.name} (unknown)")
        for p in (unknown if not delete_unknown else [])[:5]:
            lines.append(f"? {p.name} (unknown)")
        extra_r = max(0, len(removed) - 10)
        extra_u = max(0, len(unknown) - 5)
        if extra_r or extra_u:
            lines.append(f"… and {extra_r + extra_u} more")
        if lines:
            fields.append({"name": f"📁 [{label}]", "value": "\n".join(lines), "inline": False})

    payload = {
        "embeds": [{
            "title": f"Kometa Asset Cleanup  {mode}",
            "color": colour,
            "fields": fields,
            "footer": {"text": datetime.now().strftime("%Y-%m-%d %H:%M")},
        }]
    }
    try:
        r = requests.post(webhook_url, json=payload, timeout=10)
        r.raise_for_status()
    except Exception as exc:
        log.warning(f"Discord notification failed: {exc}")

# ─── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Remove orphaned Kometa asset folders.")
    parser.add_argument("--dry-run",    action="store_true",  help="Show what would be removed (default from config).")
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false", help="Actually delete orphaned assets.")
    parser.add_argument("--delete-unknown", dest="delete_unknown", action="store_true", default=None,
                        help="Also delete unknown entries (no ID tag, not in Plex).")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.set_defaults(dry_run=None, delete_unknown=None)
    args = parser.parse_args()

    cfg     = load_config()
    dry_run        = cfg.get("dry_run", True) if args.dry_run is None else args.dry_run
    delete_unknown = cfg.get("delete_unknown", False) if args.delete_unknown is None else args.delete_unknown
    verbose        = cfg.get("verbose", False) or args.verbose

    if verbose:
        log.setLevel(logging.DEBUG)

    # ── Banner ──────────────────────────────────────────────────────────────
    mode_label = f"{YELLOW}{BOLD}DRY RUN{RESET}" if dry_run else f"{RED}{BOLD}LIVE MODE — files will be deleted{RESET}"
    print(f"\n{CYAN}{BOLD}{'─'*60}{RESET}")
    unknown_label = f"  {YELLOW}(+unknown entries){RESET}" if delete_unknown else ""
    print(f"{CYAN}{BOLD}  Kometa Asset Cleanup{RESET}  {mode_label}{unknown_label}")
    print(f"{CYAN}{BOLD}{'─'*60}{RESET}\n")

    # ── Fetch API data ───────────────────────────────────────────────────────
    log.info("Fetching data from Radarr, Sonarr and Plex …")
    try:
        radarr_ids, radarr_titles = get_radarr_data(cfg["radarr"]["url"], cfg["radarr"]["api_key"])
        sonarr_ids, sonarr_titles = get_sonarr_data(cfg["sonarr"]["url"], cfg["sonarr"]["api_key"])
        plex_names                = get_plex_collection_names(cfg["plex"]["url"], cfg["plex"]["token"])
    except requests.RequestException as exc:
        log.error(f"API error: {exc}")
        sys.exit(1)

    print()

    # ── Ignore list ──────────────────────────────────────────────────────────
    ignore_set = build_ignore_set(cfg.get("ignore") or [])
    if ignore_set:
        log.info(f"Ignoring {len(ignore_set)} entries from config")

    # ── Build set of dirs to skip when encountered as sub-entries ───────────
    asset_dirs = [Path(d) for d in cfg.get("asset_dirs", [])]
    skip_dirs  = {d.resolve() for d in asset_dirs}

    # ── Scan & report per directory ──────────────────────────────────────────
    all_keep:    list[Path] = []
    all_remove:  list[Path] = []
    all_unknown: list[Path] = []
    all_ignored: list[Path] = []
    dirs_scanned: list[str] = []
    per_dir:      list[dict] = []

    for asset_dir in asset_dirs:
        dir_label = asset_dir.name  # e.g. "assets" or "tmp"
        dirs_scanned.append(str(asset_dir))
        log.info(f"Scanning {asset_dir} …")

        keep, remove, unknown, ignored = scan_asset_dir(
            asset_dir, skip_dirs,
            radarr_ids, radarr_titles,
            sonarr_ids, sonarr_titles,
            plex_names, ignore_set,
        )
        all_keep    += keep
        all_remove  += remove
        all_unknown += unknown
        all_ignored += ignored
        per_dir.append({"label": asset_dir.name, "remove": remove, "unknown": unknown})

        log.info(
            f"  {GREEN}Keep: {len(keep)}{RESET}  "
            f"{RED}Remove: {len(remove)}{RESET}  "
            f"{YELLOW}Unknown: {len(unknown)}{RESET}"
            + (f"  {DIM}Ignored: {len(ignored)}{RESET}" if ignored else "")
        )

        # Report removals for this directory
        if remove:
            label = "Would remove" if dry_run else "Removing"
            log.info(f"  {label} {len(remove)} orphaned entries:")
            for path in sorted(remove, key=lambda p: p.name.lower()):
                log.info(f"    {RED}✗ {path.name}  {DIM}({_entry_size(path)}){RESET}")
        else:
            log.info(f"  {GREEN}No orphaned entries.{RESET}")

        # Report unknowns for this directory
        if unknown:
            if delete_unknown:
                label_u = "Would remove" if dry_run else "Removing"
                log.info(f"  {label_u} {len(unknown)} unknown entries (no ID tag, not in Plex):")
                for path in sorted(unknown, key=lambda p: p.name.lower()):
                    log.info(f"    {RED}✗ {path.name}  {DIM}({_entry_size(path)}){RESET}")
            else:
                log.warning(f"  {len(unknown)} entries have no ID tag and are not in Plex — review manually:")
                for path in sorted(unknown, key=lambda p: p.name.lower()):
                    log.warning(f"    {YELLOW}? {path.name}{RESET}")

        print()

    # ── Execute deletions ────────────────────────────────────────────────────
    deleted = 0
    failed  = 0
    for path in all_remove:
        if delete_entry(path, dry_run):
            deleted += 1
            if not dry_run:
                log.debug(f"Deleted: {path}")
        else:
            failed += 1

    # ── Execute unknown deletions ────────────────────────────────────────────
    deleted_unknown = 0
    failed_unknown  = 0
    if delete_unknown:
        for path in all_unknown:
            if delete_entry(path, dry_run):
                deleted_unknown += 1
                if not dry_run:
                    log.debug(f"Deleted unknown: {path}")
            else:
                failed_unknown += 1

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"\n{CYAN}{BOLD}{'─'*60}{RESET}")
    if dry_run:
        print(f"{YELLOW}{BOLD}  DRY RUN complete — nothing was deleted.{RESET}")
        print(f"  Would remove : {RED}{len(all_remove)}{RESET} entries")
    else:
        print(f"{GREEN}{BOLD}  Cleanup complete.{RESET}")
        print(f"  Deleted : {RED}{deleted}{RESET} entries"
              + (f"  {RED}  Failed: {failed}{RESET}" if failed else ""))
    print(f"  Kept    : {GREEN}{len(all_keep)}{RESET} entries")
    if all_ignored:
        print(f"  Ignored : {DIM}{len(all_ignored)}{RESET} entries (config)")
    if delete_unknown:
        if dry_run:
            print(f"  Would remove (unknown) : {RED}{len(all_unknown)}{RESET} entries")
        else:
            print(f"  Deleted (unknown) : {RED}{deleted_unknown}{RESET} entries"
                  + (f"  {RED}  Failed: {failed_unknown}{RESET}" if failed_unknown else ""))
    else:
        print(f"  Unknown : {YELLOW}{len(all_unknown)}{RESET} entries (kept, review manually)")
    print(f"{CYAN}{BOLD}{'─'*60}{RESET}\n")

    # ── Discord ──────────────────────────────────────────────────────────────
    discord_cfg = cfg.get("discord", {})
    webhook     = discord_cfg.get("webhook_url", "")
    notify_dry  = discord_cfg.get("notify_on_dry_run", True)

    if webhook and (not dry_run or notify_dry):
        send_discord(webhook, dry_run, delete_unknown, per_dir, all_remove, all_unknown)


if __name__ == "__main__":
    main()
