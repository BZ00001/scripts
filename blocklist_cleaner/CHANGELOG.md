# Changelog

All notable changes to `blocklist_cleaner.py` will be documented in this file.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [1.1.0] - 2026-06-07

### Added
- Automatic version check against GitHub on each run.
- Update notification in terminal log when a newer version is available.
- Update notification field in Discord embed when a newer version is available, with a link to the release page.

---

## [1.0.1] - 2026-06-07

### Changed
- Version number moved from Discord embed title to footer, consistent with other natorr scripts.

---

## [1.0.0] - 2026-06-07

### Added
- Initial release.
- Multi-instance support for Sonarr and Radarr.
- Configurable age threshold via `days` setting.
- Bulk delete via `/blocklist/bulk` endpoint, batched in groups of 500.
- Dry-run mode defaulting to `true` for safety.
- Discord webhook notifications with per-instance field breakdown and totals in the footer.
- Startup version log line.
