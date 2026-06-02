# Changelog

All notable changes to `asset_cleanup.py` are documented here.

---

## [Unreleased]

---

## [1.2] - 2026-06-02

### Fixed
- Title fallback now also applies to entries with no ID tag at all. Previously, tagless folders were only matched against Plex collections and immediately flagged as unknown if not found there. They are now also checked against Sonarr and Radarr titles before being flagged.
- `&` in Sonarr/Radarr titles is now normalised to `and` before comparison, matching the spelling Kometa uses in folder names (e.g. `Law & Order: Organized Crime` now matches `Law and Order Organized Crime (2021)`).

---

## [1.1] - 2026-05-31

### Added
- **Multiple Radarr and Sonarr instances.** `radarr` and `sonarr` config keys now accept a list of instances, each with a `name`, `url`, and `api_key`. IDs and titles are merged across all instances before scanning, so an asset is only removed if it is absent from every instance. The old single-dict format still works for backwards compatibility.
- **Per-directory reporting.** Removals and unknowns are now reported immediately after each directory is scanned rather than in a single combined list at the end, making it clear which folder each entry belongs to.
- **`delete_unknown` option.** Setting `delete_unknown: true` in the config (or passing `--delete-unknown` on the CLI) also deletes entries that have no ID tag and are not matched in Plex, instead of only warning about them.
- **tmp deduplication.** `assets/tmp` no longer needs to be listed as a separate scan directory. For every removal candidate found in a primary asset dir, the script automatically checks for a matching entry in the sibling `tmp/` folder and queues it for deletion alongside the primary. Entries with a tmp companion are marked `[+tmp]` in the log.
- **Empty folder sweep.** After all deletions, the script walks all asset directories bottom-up and removes any directories left empty. In dry-run mode the count is reported without making changes.
- **Title fallback matching.** When an entry has a resolved `{tvdb-X}` or `{tmdb-X}` ID that is not found in Sonarr or Radarr, the script falls back to normalised title matching before marking the entry for removal. This handles cases where the ID in the asset folder name is stale or mismatched.
- **Unresolved placeholder fallback.** Entries with Kometa-generated placeholders such as `{tvdb-{TvdbId}}` (where the ID was never substituted) are matched against Sonarr/Radarr by title rather than immediately flagged as unknown.

### Fixed
- `tmp/` subdirectory no longer appears as an unknown collection entry when it is not listed as a scan target. All `tmp/` subdirs of configured asset directories are automatically added to the skip list.
- Entries with unresolved Kometa placeholders (`{tvdb-{TvdbId}}`, `{tmdb-{TmdbId}}`) were previously classified as collection-style entries and always flagged as unknown. They are now correctly identified and handled separately.

### Changed
- Banner and Discord embed title changed from `Kometa Asset Cleanup` to `Asset Cleanup` for tool-agnostic use.
- ANSI colour codes are automatically disabled when stdout is not a TTY (e.g. Unraid User Scripts output window), producing clean plain-text output in those environments.
- `assets/tmp` removed from the default `asset_dirs` config. Tmp companions are now handled automatically and no longer need a separate scan entry.
