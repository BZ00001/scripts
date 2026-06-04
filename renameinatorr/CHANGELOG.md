# Changelog

All notable changes to this project will be documented in this file.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [1.4.0] – 2026-06-04

### Added
- `refresh_before_rename` per-instance yml setting (default: false). When enabled, forces a metadata refresh from TVDB/TMDB for each item in the chunk before checking the rename list. This ensures title updates are reflected immediately rather than waiting for Sonarr/Radarr's own refresh schedule, at the cost of a slightly longer run time.

### Changed
- `wait_for_commands` default timeout increased from 60 to 120 seconds. The previous 60-second timeout caused a misleading warning even when the rename had completed successfully on disk, as confirmed by `verify_renames`.

### Fixed
- File renames were fire-and-forget, meaning the script had no way to confirm whether Sonarr/Radarr had actually renamed the file on disk. `rename_media` now returns command IDs, which are polled via `wait_for_commands` and then verified via `verify_renames`. If a file still needs renaming after the command completes, a warning is logged naming the specific file.
- Diagnostic logging added to `rename_media` to surface cases where no file IDs are found in the rename list, or where Sonarr/Radarr returns a command response with no ID. Both conditions log a warning in normal runs and full detail at DEBUG level.
- `refresh_before_rename` correctly skips the metadata refresh in dry-run mode.

---

## [1.3.0] – 2026-06-03

### Added
- Version number added to script header (`# version: 1.3`).
- `wait_for_commands` method — polls all RenameFiles command IDs concurrently until they complete, fail, or a 60-second timeout elapses. File renames are now confirmed before the script moves on.
- `verify_renames` method — after a RenameFiles command completes, re-checks the rename list for each affected series/movie. Any files still needing a rename are logged as warnings, making it clear when a rename did not take effect on disk.
- `rename_media` now returns a list of command IDs so the caller can poll for completion.

### Fixed
- File renames were fire-and-forget, so the script had no way to confirm they completed. RenameFiles commands are now polled and verified before continuing.
- Dead `_put_with_move` method removed — it was left over from a previous approach and was never called.
- Em-dash in Discord notification header replaced with a hyphen.

---

## [1.2.0] – 2026-06-01

### Fixed
- `datetime.datetime.utcnow()` replaced with `datetime.datetime.now(datetime.timezone.utc)` in the Discord notification — `utcnow()` was deprecated in Python 3.12 and produced a visible warning in Unraid User Scripts logs.

---

## [1.1.0] – 2026-05-31

### Added
- `--title` CLI flag for processing a single item by title substring (case-insensitive). Also available as `title_filter` per-instance in the yml for Unraid User Scripts users who cannot pass CLI arguments.
- Discord notification now always sent after every run — instances with nothing to rename show `✅ No items needed renaming (N checked)` instead of being omitted.
- Discord notification now shows total checked and total changed per instance (`X / Y changed`).
- Discord notification now splits file renames and folder renames into separate embed fields per instance for easier reading.

### Changed
- **Native folder renaming** — folder renaming now delegates entirely to Radarr / Sonarr via the `movie/editor` and `series/editor` endpoints with `moveFiles: true` and the item's existing `rootFolderPath`. The app applies its own naming format, colon replacement, illegal character handling, CleanTitle logic, and all other token expansions. The previous Python reimplementation of the naming logic has been removed (~180 lines).
- **`Using count=` log line** moved to directly below the instance header so it reads in natural order.
- **Dry-run folder preview** updated to reflect the native approach — shows how many items would be submitted for folder renaming rather than a per-item `X → Y` preview, which is no longer possible without reimplementing the naming logic.
- `rename_folders` yml comment updated to document the dry-run limitation.
- `get_effective_count` simplified — logging moved inline to `process_instance`.

### Removed
- All Python naming-logic helpers: `_format_folder_name`, `_clean_title`, `_arr_clean_title`, `_colon_replacement`, `_with_year`, `_move_the`. These were fragile reimplementations of Sonarr/Radarr's internal `FileNameBuilder.cs` logic and are superseded by the native editor endpoint approach.
- `get_naming_config()` API method — no longer needed.
- `_put_with_move()` — replaced by the editor endpoint which handles physical folder moves natively.
- `naming_config` fetch from `process_instance` — no longer fetched or passed through.

### Fixed
- `{Series CleanTitle}` / `{Movie CleanTitle}` tokens were previously mapped to the same value as the regular title. Now handled correctly by the app itself via the native approach.
- `{Series CleanTitleWithoutYear}` and similar unhandled tokens caused literal token strings to appear in folder names (e.g. `[{Series CleanTitleWithoutYear} (2023)]`). Now handled correctly by the app.
- Illegal character replacement (`replaceIllegalCharacters` setting) was not respected — the script always stripped characters rather than replacing them per the app's configured mapping (e.g. `/` → `+`). Now handled correctly by the app.
- Colon replacement mode (`colonReplacementFormat`) was applied uniformly; Smart mode's distinction between `: ` and `:` was not implemented correctly. Now handled correctly by the app.
- `+` was incorrectly added to the illegal character strip set, causing titles like `Fate/Zero` to become `FateZero` instead of `Fate+Zero`. Now handled correctly by the app.

---

## [1.0.0] – 2026-05-25

### Added
- `ignore_tag` support — items with this tag are always skipped, even after a cycle reset.
- `title_filter` yml key per instance for testing a single item without CLI access.
- Discord webhook notifications with old → new names shown for both file and folder renames.
- Folder rename detection — after rename, `get_parsed_media()` is called to detect and log actual path changes.
- Cycling tag logic — `tag_name` tag is applied to every processed item; when all items are tagged the tag is cleared and the cycle restarts.
- Per-item error handling in folder rename — one failed PUT no longer aborts remaining items in the chunk.

### Changed
- **Radarr folder renaming** switched from `RenameMovie` command (which only renames episode files, not the folder) to `PUT /movie/{id}`, matching the Sonarr approach.
- **Command polling removed** — `wait_for_command`, `DEFAULT_CMD_TIMEOUT`, and `command_timeout` removed. All folder renames are now fire-and-forget; the rename itself is synchronous and polling the refresh command is unnecessary.
- **Post-file-rename refresh** made fire-and-forget — was previously polled, causing 2-minute timeouts when Sonarr was slow to respond.
- **Post-folder-rename refresh** only triggered when at least one folder was actually renamed.
- `get_rename_list` called once per item and results cached — previously called a second time inside `rename_media`, doubling API calls.
- `rename_media` signature changed to accept pre-collected rename-list dicts instead of re-fetching.
- `Using count=0` log message changed to `Using count=all` for clarity.
- Tag now applied to all items in the chunk, not just those with file renames — items with only folder renames or nothing to do are correctly tagged and skipped on the next run.
- Double year in log output and Discord notifications fixed — title display now checks whether the year is already present before appending.

### Fixed
- `RenameSeries` Sonarr command does not rename folders — only episode files. Replaced with `PUT /series/{id}` approach.
- `RenameMovie` Radarr command does not rename folders — only movie files. Replaced with `PUT /movie/{id}` approach.
- `moveFiles=true` query parameter added to PUT calls — without it the PUT was a DB-only update and did not physically move the folder on disk.
- `_put` method accidentally removed when `_put_with_move` was added, causing `AttributeError` in `add_tags` and `remove_tags`.
- `{Release Year}` Radarr token was unhandled, causing every movie to appear to need folder renaming.
- `{Edition Tags}` and `{Edition}` Radarr tokens added.
- Radarr folder rename previously hung indefinitely for movies with no files on disk (e.g. upcoming titles). These are now skipped.
- `ready` variable was unbound if `response.get("id")` was falsy — initialised to `False` before conditional assignment.
- Unicode apostrophe normalisation (`'` → `'`) added to prevent spurious folder renames caused by curly vs straight quote mismatches between the API and the filesystem.
- Cycling tag logic fixed — `media_list` was not filtered to untagged items after the cycle check, causing every run to reprocess the full library.
- `rename_folders` return value for Radarr now correctly tracks whether any PUT was made instead of returning `True` unconditionally.
- Folder rename decoupled from file rename — a folder can now be renamed even when all episode/movie files are already correctly named.

