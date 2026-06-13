# Changelog

All notable changes to this project will be documented in this file.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [1.6.0] – 2026-06-13

### Added
- `--version` CLI flag — prints the version number and exits, without
  triggering a run. Useful for support requests.
- `log_file` global yml setting (optional) — logs to a rotating file
  (5 MB per file, 3 backups kept) in addition to stdout. Useful for
  infrequent schedules where the terminal log is long gone by the time
  you check.
- Retry with backoff on transient API failures — `_get`/`_post`/`_put` now
  retry up to 3 times with exponential backoff (2s, 4s) on connection errors
  and timeouts. One mid-run network blip no longer aborts an entire instance
  run. HTTP error responses (4xx/5xx) are not retried since those indicate a
  request problem rather than a network issue.
- Failed instances now appear in the Discord notification as a clear
  `⚠️ Run failed` field with the error reason, instead of silently
  disappearing from the results. The notification also fires when all
  instances failed, so silence no longer means success.
- Run summary footer — every run ends with a one-line summary:
  instances processed, files renamed, folders renamed, failures, and
  total runtime. Makes scanning scheduled run logs much faster.
- `count` validation at startup — `count: 0` with `enable_batching: true`
  now clamps the chunk size to 100 with a warning telling the user to set
  50-75 in the yml. `count: 0` without batching on a library larger than
  100 items warns that the whole library will be processed in one chunk.

### Fixed
- GitHub version check silently broken — the check looked for lines starting
  with `VERSION = "` but the constant is alignment-padded
  (`VERSION           = "`), so the check would never match once pushed.
  Now uses a whitespace-tolerant regex.
- Outdated Discord webhook comment in the yml — claimed notifications were
  only sent when something was renamed, which has not been true since 1.1.0.

---

## [1.5.2] – 2026-06-09

### Added
- `file_count` field added to items returned by `get_parsed_media` — tracks
  the number of files per item (`1` per Radarr movie, `episodeFileCount` for
  Sonarr series). Used to scale timeouts by total file count rather than item
  count, ensuring a single series with 500 episodes gets proportionally more
  time than 10 single-episode folders.
- `has_file` field added to items returned by `get_parsed_media` — tracks
  whether a movie/series had files on disk before the rename. Used to skip
  `wait_for_files_found` for placeholder/upcoming entries that will never
  satisfy `hasFile: true`, preventing full timeout waits for those items.
- Large library guidance added to yml comments for `count` and
  `enable_batching` — recommends keeping `count` at 50-100 for libraries
  with 1000+ items, and warns against setting `count: 0` with
  `enable_batching: true` on large libraries.

### Changed
- `rename_media` now uses instance-aware strategies. Radarr fires all
  RenameFiles commands simultaneously then runs a single `verify_renames`
  pass across all movies — movies are single-file and Radarr processes them
  near-instantly so the cold-queue problem does not apply, and serializing
  one movie at a time was unnecessarily slow for large libraries. Sonarr
  remains serialized one series at a time with inline verification per series
  and a timeout of `max(30, file_count * 5)` — a single large series (e.g.
  The Simpsons at 339 files) can block everything queued behind it if commands
  are fired simultaneously. Radarr bulk verify timeout is
  `max(30, movie_count * 2)` seconds.
- Post-file-rename refresh is now skipped when folder renames are pending in
  the same chunk. With the serialised `rename_media` approach, Sonarr's
  episodefile records are updated directly by the RenameFiles command and
  confirmed via inline `verify_renames` — no rescan is needed before folder
  renames proceed. Firing an intermediate refresh while folder renames are
  pending caused Sonarr's rescan to collide with the folder move, producing
  spurious MissingFromDisk events. When no folder renames are pending the
  post-file-rename refresh still fires as before.
- All timeout hard caps removed. Previously `wait_for_files_found` and related
  methods were capped at fixed maximums. Caps are now removed so timeouts scale
  linearly with file counts, ensuring `count: 0` runs on very large libraries
  complete correctly without timing out.
- `folder_wait` for `wait_for_files_found` now scales by total file count
  across renamed folders rather than folder count, using a `files * 2`
  formula. Log message updated to show both file count and folder count.

### Fixed
- Critical: post-file-rename refresh collided with folder renames when both
  were needed in the same chunk. Sonarr's rescan triggered by the intermediate
  refresh was still running when folder renames fired, causing MissingFromDisk
  events across all affected series. Fixed by skipping the intermediate refresh
  when folder renames are pending — the post-folder-rename refresh covers both
  operations once everything has moved.
- `wait_for_files_found` now always waits at least one poll interval before
  the first check, guaranteeing a minimum gap between firing a refresh and
  proceeding even when `hasFile` was already satisfied before the refresh.
- Folder renames that had no files on disk before the rename no longer
  trigger a full `wait_for_files_found` timeout.
- Radarr bulk verify timeout calculation corrected — was iterating over
  rename-list dicts to compute file count (always equalling movie count for
  Radarr). Simplified to `len(fired)` which is semantically correct since
  Radarr movies are single-file.

### Removed
- `wait_for_renames_settled` — made redundant by the serialised `rename_media`
  approach. This method polled `GET /episodefile` until old filenames were gone
  from Sonarr's records before allowing folder renames to proceed. With the
  serialised approach, `verify_renames` (which polls `GET /rename`) confirms
  the same thing — episodefile records updated to new filenames — inline per
  series. Both endpoints reflect the same underlying state; the serialised
  verify made the separate poll redundant.
- `verify_rename_with_retry` — replaced by inline verification inside
  `rename_media`. Each series is now verified immediately after its
  RenameFiles command completes, making a separate bulk retry step redundant.

---

## [1.5.1] – 2026-06-08

### Added
- `wait_for_files_found` method — polls `GET /movie/{id}` or `GET /series/{id}` and checks
  `hasFile: true` (Radarr) or `episodeFileCount > 0` (Sonarr) until files are confirmed present.
  Accepts an optional `expected_paths` dict to also verify the record path matches the expected
  new location, used after folder renames to confirm Radarr has rescanned the new path rather
  than just seeing `hasFile: true` from before the move. Always waits at least one poll interval
  before the first check to guarantee a minimum gap between firing a refresh and proceeding.
- `verify_rename_with_retry` method — polls the rename list directly after a RenameFiles command
  and exits as soon as it comes back empty. More reliable than polling command status, which
  Radarr/Sonarr can be slow to update even when the rename finished in seconds.

### Changed
- File rename verification now uses `verify_rename_with_retry` instead of polling command status.
  The rename list being empty is the source of truth — command status polling produced misleading
  timeout warnings even when renames completed successfully.
- Post-file-rename refresh now waits via `wait_for_files_found` before the folder rename begins,
  closing the sequencing window that caused spurious MissingFromDisk/Deleted events in external
  tools like Notifiarr. The folder rename previously started at the same second as the refresh.
- Post-folder-rename refresh now waits via `wait_for_files_found` with `expected_paths`, ensuring
  Radarr/Sonarr has confirmed files at the new location before the script exits. Previous
  approaches using `wait_for_commands` timed out because Radarr never marked the refresh command
  as completed in time.
- Post-folder-rename refresh only fires for items where the folder path actually changed, not all
  items in the chunk. Path changes are detected immediately from the DB after the editor endpoint
  call rather than waiting for a refresh to complete.
- `refresh_items` simplified to `None` return type — all refresh calls are fire-and-forget,
  with `wait_for_files_found` used to confirm outcomes rather than polling command IDs.

### Fixed
- `verify_renames` was accidentally merged into `verify_rename_with_retry` as dead code after a
  `return` statement. Any call to `app.verify_renames()` would have thrown `AttributeError`.
- Verification failure log message corrected — was logging at WARNING level with the message
  "Verifying file renames…" which was confusing. Now logs a clear failure message listing the
  specific files that did not rename.

### Removed
- `wait_for_commands` — removed as dead code. All command polling has been replaced by outcome-
  based checks (`verify_rename_with_retry` for file renames, `wait_for_files_found` for refreshes).
- `any_renamed` variable — set but never read after refresh gating logic changed.
- `all_folder_ids` variable — computed but replaced by `actually_renamed` in all call sites.

---

## [1.5.0] – 2026-06-07

### Added
- Version check at startup. The script fetches the latest version from GitHub on every run and
  logs a warning if a newer version is available. The update notice also appears as a field at
  the top of the Discord notification with a link to the release page.

---

## [1.4.2] – 2026-06-07

### Added
- `VERSION` constant and startup log line — version is logged at startup (`renameinatorr vX.X.X`)
  so it is immediately visible in logs and support reports.

---

## [1.4.1] – 2026-06-04

### Changed
- File rename polling timeout made dynamic: `min(120, 30 + file_count * 2)` seconds. The previous
  fixed 120-second base was too aggressive for large batches.

---

## [1.4.0] – 2026-06-04

### Added
- `refresh_before_rename` per-instance yml setting (default: false). When enabled, forces a
  metadata refresh from TVDB/TMDB for each item in the chunk before checking the rename list,
  ensuring title updates are reflected immediately rather than waiting for the app's own schedule.
- `rename_media` now returns command IDs for polling.
- `verify_renames` method — re-checks the rename list after a RenameFiles command to confirm
  files were actually renamed on disk.
- Diagnostic logging in `rename_media` to surface cases where no file IDs are extracted from the
  rename list or where the command response contains no ID.

### Fixed
- File renames were fire-and-forget with no confirmation. RenameFiles commands are now polled
  and the rename list is verified before continuing.

---

## [1.3.0] – 2026-06-03

### Fixed
- Dead `_put_with_move` method removed.
- Em-dash in Discord notification header replaced with a hyphen.

---

## [1.2.0] – 2026-06-01

### Fixed
- `datetime.datetime.utcnow()` replaced with `datetime.datetime.now(datetime.timezone.utc)` —
  `utcnow()` was deprecated in Python 3.12 and produced a visible warning in Unraid User Scripts.

---

## [1.1.0] – 2026-05-31

### Added
- `--title` CLI flag and `title_filter` yml setting for processing a single item by title
  substring, useful for testing before a full library run.
- Discord notification now always sent — instances with nothing to rename show
  `✅ No items needed renaming (N checked)`.
- Discord notification shows total checked and total changed per instance (`X / Y changed`).
- Discord notification splits file renames and folder renames into separate embed fields.

### Changed
- **Native folder renaming** — folder renaming now delegates entirely to Radarr/Sonarr via the
  `movie/editor` and `series/editor` endpoints with `moveFiles: true`. The app applies its own
  naming format, token expansion, colon replacement, and illegal character handling. The previous
  Python reimplementation of the naming logic (~180 lines) has been removed.
- `Using count=` log line moved to directly below the instance header.
- Dry-run folder preview shows item count only — per-item `X → Y` preview is not possible
  without reimplementing the naming logic.

### Fixed
- Naming tokens (`{Series CleanTitle}`, `{Series CleanTitleWithoutYear}`, `{Release Year}`, etc.)
  were not expanded correctly — now handled natively by the app.
- Illegal character replacement (`replaceIllegalCharacters` setting) was not respected.
- Colon replacement mode (`colonReplacementFormat`) Smart mode not handled correctly.
- Cycling tag logic — `media_list` was not filtered to untagged items, causing every run to
  reprocess the full library.
- Tag now applied to all items in the chunk regardless of whether files or folders were renamed.

---

## [1.0.0] – 2026-05-25

### Added
- Initial standalone release. Radarr and Sonarr file and folder renaming with cycling tag logic,
  chunked processing, Discord webhook notifications, dry-run mode, ignore tag support, and
  per-instance configuration via yml.
