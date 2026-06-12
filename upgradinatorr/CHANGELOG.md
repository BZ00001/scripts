# Changelog - upgradinatorr.py

All notable changes to this standalone reimplementation are documented here.
The original module was written by [Drazzilb08](https://github.com/Drazzilb08/daps).

---

## [1.4.0] - 2026-06-12

### Changed
- **Unattended reset condition refined.** Previously the reset only fired when *every* item in the library was tagged, which meant items permanently or long-term ineligible (unmonitored, status `announced`/`inCinemas`, ignore tag) could block the cycle from ever restarting - in one case Radarr got stuck at 33/48 indefinitely because the remaining 15 movies were not yet released. The reset now fires whenever nothing is left to search, *unless* some untagged Sonarr items are excluded only by the season monitored threshold (which could still become eligible later). New `has_threshold_blocked_items()` helper implements this check.

### Removed
- Dead code from the old queue-based grab-checking approach, fully superseded by `get_history_grabs()`: `ArrClient.wait_for_command()`, `ArrClient.get_queue()`, and the module-level `process_queue()` function.
- Unused `search_count` counter in `process_instance` (incremented but never read).

---

## [1.3.4] - 2026-06-12

### Added
- Discord notifications now show a "Nothing to process" field for an instance when all remaining untagged items are unmonitored, not yet available (announced/in cinemas/upcoming), or fail the season monitored threshold. Previously this case produced no field at all, making it look like the instance was skipped or broken.

### Changed
- Instance separator in Discord notifications changed from a blank line (`\u2800`) to a thin divider (`▬▬▬▬▬...`), reducing the large empty gap between instance blocks.

### Fixed
- `process_instance` returning `None` when there was nothing to process, instead of the populated output dict. This caused the instance to be silently dropped from the Discord notification entirely, even though the terminal log showed it correctly.

---

## [1.3.3] - 2026-06-11

### Fixed
- Instance separator field in Discord notifications not rendering on some clients (e.g. macOS). `\u200b` (zero-width space) is collapsed by certain Discord clients since it has no visible glyph. Replaced with `\u2800` (Braille Pattern Blank), which renders consistently across all clients.

---

## [1.3.2] - 2026-06-11

### Added
- Blank line separator between instance blocks in Discord notifications, preventing Radarr and Sonarr results from running together visually.

### Fixed
- Unattended mode triggering a full tag reset when items remained untagged but all failed the season threshold filter. The reset now only fires when every item in the library actually carries the checked tag.

---

## [1.3.1] - 2026-06-09

### Fixed
- Sonarr occasionally processing fewer items than `count` specifies. Parallel episode fetching (10 workers) could produce transient failures under API load. Each failed fetch silently set `seasons = []`, causing the series to be skipped by `filter_media` with "no monitored seasons above threshold" at DEBUG level - invisible at the default INFO log level. `fetch_episode_data` now retries up to 3 times with exponential back-off (1s, 2s, 4s) before giving up.

---

## [1.3.0] - 2026-06-07

### Added
- **Version check at startup.** The script fetches its own source from GitHub on each run and compares the `VERSION` constant. If a newer version is available, a notice is printed to the terminal log and prepended to the Discord notification as a dedicated "Update available" field with a link to the release page.
- `GITHUB_RAW_URL` and `GITHUB_RELEASE_URL` constants for the version check and update link.
- `_parse_version()` helper to compare semver strings as integer tuples.
- `check_for_update()` function; failures are logged at DEBUG level and silently skipped so a network hiccup never interrupts a run.

---

## [1.2.1] - 2026-06-07

### Added
- `VERSION` constant (`"1.2.1"`) in the constants section. The version is logged at startup (`upgradinatorr v1.2.1`) and shown in the Discord notification footer, making it easy to confirm which version is running from either the log or a notification.

---

## [1.2] - 2026-05-26

### Added
- **Parallel instance processing.** Radarr and Sonarr instances now run simultaneously in separate threads, reducing total runtime to the duration of the slowest instance rather than the sum of all instances.
- **Buffered logging.** Each instance writes to its own in-memory buffer during parallel execution; output is flushed to the log in config order (Radarr first, then Sonarr) once all instances complete, preventing interleaved log lines.
- **Grab history reporting.** After searches complete, the script queries each instance's history API for `grabbed` events since the search was triggered. Results are shown in the log and Discord notification per item. Controlled by `wait_for_commands: true`.
- **Configurable history check delay.** `history_check_delay` (base seconds) and `history_check_delay_per_item` (additional seconds per searched item) allow tuning how long the script waits before querying history. Scales with the number of items searched.
- **Configurable command timeout.** `command_timeout` sets the maximum seconds to wait for an Arr search command to report completion before moving on (default: 60).
- **`wait_for_commands` setting.** When `false` (default), searches are fired and items tagged immediately without waiting for indexer results. When `true`, the script polls command state and then checks grab history.
- **`is_radarr` field** on media items, replacing `seasons is None` checks throughout.
- **Parallel tag removal.** `remove_tag_from_all` uses a thread pool when clearing tags in unattended mode.
- **Parallel episode fetching.** Sonarr episode data for all series is fetched concurrently (10 workers) at startup, each with its own `requests.Session` for thread safety.
- **venv setup and run instructions** in the script header.

### Changed
- **History check uses the history API** instead of the download queue. Queue-based checking was unreliable due to timing. Grabs appear in history after the search command completes, but the queue only shows active downloads. The history API with a `since` timestamp and per-record `seriesId`/`movieId` validation is accurate.
- **`history_check_delay` default lowered to 15s** (base) with a 10s-per-item scaling factor, replacing the previous fixed 60s delay.
- **Command state comparison is now case-insensitive.** Radarr/Sonarr v4 returns `"Completed"` (capital C); earlier versions return `"completed"`. Both are now handled.
- **`BufferingLogger.flush_to` respects log level.** Buffered DEBUG records are discarded when `log_level: INFO` is configured. Previously `logger.handle()` bypassed the level filter.
- **Grab deduplication.** History records are deduplicated by source title before display. Sonarr writes one history entry per episode, so a season pack grab previously produced a line per episode in the notification.
- **Per-record `seriesId`/`movieId` validation.** The Sonarr/Radarr history API ignores the ID filter in some versions and returns all recent history. Each record is now validated in code to belong to the queried item.
- **Sequential command polling** replaces the previous `ThreadPoolExecutor`-based wait. Sharing a `requests.Session` across threads is not safe; a single polling loop checking all pending command IDs with `time.sleep(5)` between passes is both correct and simpler.
- **`filter_media` simplified.** Removed the two-pass lazy fetch approach (which caused sequential episode fetches with no connection pooling). Episode data is fetched upfront in parallel at startup and filtering is a single in-memory pass.

### Fixed
- `break` outside loop - orphaned `if search_count >= count: break` left over after the search loop was rewritten. Removed (redundant since `filter_media` already caps the list at `count`).
- Sonarr instance silently failing during parallel execution due to `requests.Session` not being thread-safe when shared across `ThreadPoolExecutor` workers.
- All Sonarr items showing identical grab history - caused by the history API ignoring the `seriesId` query parameter and returning all recent grabs, which then all matched the `since` timestamp filter.
- Commands never detected as complete - `state` comparison was case-sensitive (`"completed"`) while the API returns `"Completed"`.
- DEBUG log lines appearing when `log_level: INFO` - `logger.handle()` bypasses level filtering; fixed with an `isEnabledFor` check before replaying buffered records.
- Grab history always empty - `eventType: "grabbed"` was passed as a query parameter but some Arr versions ignore or reject the string value. Filtering is now done in code on the returned records.

---

## [1.1] - 2026-04-04

### Added
- Initial standalone conversion from the [DAPS framework](https://github.com/Drazzilb08/daps).
- Self-contained `ArrClient` class replacing the `daps` `arrpy` utility, communicating directly with the Radarr/Sonarr v3 API via `requests`.
- YAML configuration via `upgradinatorr.yml`, replacing framework-level config handling.
- Discord webhook notifications when items are searched.
- Tag-based cycle tracking (`tag_name`, `ignore_tag`).
- Unattended mode - clears all tags and restarts the cycle when every item has been processed.
- Season monitored threshold - skips Sonarr seasons where fewer than the configured percentage of episodes are monitored.
- Per-season searching for Sonarr; movie-level searching for Radarr.
- Download queue inspection after searches, reporting active downloads with custom format scores.
- Dry-run mode (`--dry-run`) for safe testing without making any changes.
- `--debug` flag for verbose logging.
- `--config` flag to specify a custom config file path.
