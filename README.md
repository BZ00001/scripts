# natorr scripts

A collection of standalone Python scripts for automating media library maintenance on [Radarr](https://radarr.video/), [Sonarr](https://sonarr.tv/), [Plex](https://www.plex.tv/), and [Kometa](https://kometa.wiki/). Originally based on modules from [Drazzilb08/daps](https://github.com/Drazzilb08/daps), rewritten as self-contained scripts with no framework dependency.

---

## Scripts

### `upgradinatorr.py`

Searches for quality upgrades in Radarr and Sonarr using a tag-based cycle system. Items are tagged after being searched so they are skipped on the next run. Once every item has been tagged, all tags are cleared and the cycle starts over — ensuring every item gets searched over time without hammering the API all at once.

**Features:**
- Processes Radarr (movie-level) and Sonarr (per-season) instances
- Configurable batch size per run via `count`
- Skips Sonarr seasons below a configurable monitored-episode threshold
- Inspects the download queue after searching and reports active downloads with custom format scores
- Unattended mode: automatically clears tags and resets the cycle when all items are processed
- Dry-run mode (`--dry-run`) for safe testing
- Discord webhook notifications when items are found and searched
- Config: `upgradinatorr.yml`

---

### `renameinatorr.py`

Renames media files and folders in Radarr and Sonarr to match the configured naming format. Uses the same tag-based cycle system as upgradinatorr so items are processed in batches across runs rather than all at once.

**Features:**
- Processes Radarr and Sonarr instances independently
- Renames files and optionally the containing folder
- Configurable batch size per run via `count`
- Tag-based cycle tracking with automatic reset when all items are processed
- Dry-run mode (`--dry-run`) for safe testing
- Discord webhook notifications showing old → new names for each renamed item
- Config: `renameinatorr.yml`

---

### `asset_cleanup.py`

Scans Kometa asset directories and removes poster/background image files that no longer correspond to any item in Radarr, Sonarr, or Plex. Prevents the asset folder from growing indefinitely with orphaned images from removed media or renamed collections.

**Features:**
- Matches asset files against Radarr movies, Sonarr series, and Plex collections
- Fuzzy name normalisation to handle minor title differences
- Configurable ignore list to protect specific asset folders from deletion
- Dry-run mode (default: enabled) so nothing is deleted until you're confident
- Reports total reclaimed disk space
- Discord webhook notifications summarising what was removed
- Config: `asset_cleanup.yml`

---

## Setup

### Prerequisites

- Python 3.8 or higher
- A persistent location on your array, e.g. `/mnt/user/appdata/scripts/natorr/`

### Virtual environment (recommended)

Create a venv once in the script directory so dependencies survive reboots:

```bash
python3 -m venv /mnt/user/appdata/scripts/natorr/python-venv
```

Install dependencies:

```bash
/mnt/user/appdata/scripts/natorr/python-venv/bin/pip install requests pyyaml
```

Verify:

```bash
/mnt/user/appdata/scripts/natorr/python-venv/bin/python3 \
    -c "import requests, yaml; print('OK')"
```

---

## Configuration

Each script reads a YAML config file from the same directory. Copy and edit the example configs before running:

| Script | Config file |
|---|---|
| `upgradinatorr.py` | `upgradinatorr.yml` |
| `renameinatorr.py` | `renameinatorr.yml` |
| `asset_cleanup.py` | `asset_cleanup.yml` |

Fill in your API URLs, API keys, and any optional settings. All scripts default to `dry_run: true` on first run.

---

## Running the scripts

### Native (terminal / cron)

```bash
# upgradinatorr
/mnt/user/appdata/scripts/natorr/python-venv/bin/python3 \
    /mnt/user/appdata/scripts/natorr/upgradinatorr.py

# renameinatorr
/mnt/user/appdata/scripts/natorr/python-venv/bin/python3 \
    /mnt/user/appdata/scripts/natorr/renameinatorr.py

# asset_cleanup
/mnt/user/appdata/scripts/natorr/python-venv/bin/python3 \
    /mnt/user/appdata/scripts/natorr/asset_cleanup.py
```

Optional flags available on all scripts:

| Flag | Description |
|---|---|
| `--dry-run` | Log what would change without making any changes |
| `--debug` | Enable verbose debug logging |
| `--config /path/to/file.yml` | Use a config file at a custom path |

Example:

```bash
/mnt/user/appdata/scripts/natorr/python-venv/bin/python3 \
    /mnt/user/appdata/scripts/natorr/renameinatorr.py --dry-run --debug
```

### Unraid User Scripts

In the [User Scripts](https://forums.unraid.net/topic/48707-plugin-user-scripts/) plugin, create a new script for each and paste the following as the script body (adjust paths if needed):

**upgradinatorr:**
```bash
#!/bin/bash
/mnt/user/appdata/scripts/natorr/python-venv/bin/python3 \
    /mnt/user/appdata/scripts/natorr/upgradinatorr.py
```

**renameinatorr:**
```bash
#!/bin/bash
/mnt/user/appdata/scripts/natorr/python-venv/bin/python3 \
    /mnt/user/appdata/scripts/natorr/renameinatorr.py
```

**asset_cleanup:**
```bash
#!/bin/bash
/mnt/user/appdata/scripts/natorr/python-venv/bin/python3 \
    /mnt/user/appdata/scripts/natorr/asset_cleanup.py
```

Set your desired schedule (e.g. daily or hourly) in the User Scripts UI. Script output is captured and displayed in the Unraid web interface.

---

## Credits

Based on original modules by [Drazzilb08](https://github.com/Drazzilb08/daps). Rewritten as standalone scripts with no framework dependency.
