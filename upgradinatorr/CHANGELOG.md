# Changelog — upgradinatorr.py

All notable changes to this standalone reimplementation are documented here.
The original module was written by [Drazzilb08](https://github.com/Drazzilb08/daps).

---

## [version 1.1]

### Added
- **Parallel instance processing** — Radarr and Sonarr instances now run simultaneously in separate threads, reducing total runtime to the duration of the slowest instance rather than the sum of all instances.
- **Buffered logging** — each instance writes to its own in-memory buffer during parallel execution; output is flushed to the log in config order (Radarr first, then Sonarr) once all instances complete, preventing interleaved log lines.
- **Grab history reporting** — after searches complete, the script queries each instance's history API for `grabbed` events since the search was triggered. Results are shown in the log and Discord notification per item. Controlled by `wait_for_commands: true`.
- **Configurable history check delay** — `history_check_delay` (base seconds) and `history_check_delay_per_item` (additional seconds per searched item) allow tuning how long the script waits before querying history. Scales with the number of items searched.
- **Configurable command timeout** — `command_timeout` sets the maximum seconds to wait for an Arr search command to report completion before moving on (default: 60).
- **`wait_for_commands` setting** — when `false` (default), searches are fired and items tagged immediately without waiting for indexer results. When `true`, the script polls command state and then checks grab history.
- **`is_radarr` field** on media items, replacing `seasons is None` checks throughout.
- **Parallel tag removal** — `remove_tag_from_all` uses a thread pool when clearing tags in unattended mode.
- **Parallel episode fetching** — Sonarr episode data for all series is fetched concurrently (10 workers) at startup, each with its own `requests.Session` for thread safety.
- **venv setup and run instructions** in the script header.

### Changed
- **History check uses the history API** instead of the download queue. Queue-based checking was unreliable due to timing — grabs appear in history after the search command completes, but the queue only shows active downloads. The history API with a `since` timestamp and per-record `seriesId`/`movieId` validation is accurate.
- **`history_check_delay` default lowered to 15s** (base) with a 10s-per-item scaling factor, replacing the previous fixed 60s delay.
- **Command state comparison is now case-insensitive** — Radarr/Sonarr v4 returns `"Completed"` (capital C); earlier versions return `"completed"`. Both are now handled.
- **`BufferingLogger.flush_to` respects log level** — buffered DEBUG records are discarded when `log_level: INFO` is configured. Previously `logger.handle()` bypassed the level filter.
- **Grab deduplication** — history records are deduplicated by source title before display. Sonarr writes one history entry per episode, so a season pack grab previously produced a line per episode in the notification.
- **Per-record `seriesId`/`movieId` validation** — the Sonarr/Radarr history API ignores the ID filter in some versions and returns all recent history. Each record is now validated in code to belong to the queried item.
- **Sequential command polling** replaces the previous `ThreadPoolExecutor`-based wait. Sharing a `requests.Session` across threads is not safe; a single polling loop checking all pending command IDs with `time.sleep(5)` between passes is both correct and simpler.
- **`filter_media` simplified** — removed the two-pass lazy fetch approach (which caused sequential episode fetches with no connection pooling). Episode data is fetched upfront in parallel at startup and filtering is a single in-memory pass.

### Fixed
- `break` outside loop — orphaned `if search_count >= count: break` left over after the search loop was rewritten. Removed (redundant since `filter_media` already caps the list at `count`).
- Sonarr instance silently failing during parallel execution due to `requests.Session` not being thread-safe when shared across `ThreadPoolExecutor` workers.
- All Sonarr items showing identical grab history — caused by the history API ignoring the `seriesId` query parameter and returning all recent grabs, which then all matched the `since` timestamp filter.
- Commands never detected as complete — `state` comparison was case-sensitive (`"completed"`) while the API returns `"Completed"`.
- DEBUG log lines appearing when `log_level: INFO` — `logger.handle()` bypasses level filtering; fixed with an `isEnabledFor` check before replaying buffered records.
- Grab history always empty — `eventType: "grabbed"` was passed as a query parameter but some Arr versions ignore or reject the string value. Filtering is now done in code on the returned records.
