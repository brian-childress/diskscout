# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-11-23

### Added

- `scan`: read-only scanners for git worktrees, dependency/build folders, package-manager and app caches, iOS Simulator runtimes, duplicate folders, `*-bak` style backups, duplicate files and large files.
- Markdown and JSON reports with stable finding IDs, risk levels and totals that don't double-count nested folders.
- Hardlink-aware folder sizes and APFS clone detection for duplicate files.
- Protection for active AI-agent runs (`.agent/v2c/runs`), user-defined protected paths, and worktrees with unpushed or uncommitted work.
- `apply` (dry run by default), `restore` and `purge`, with live re-validation, Trash-first staging and a manifest for every run.
- `report` command, optional `~/.config/diskscout/config.json`, XDG state directory for reports.

[0.1.0]: https://github.com/brian-childress/diskscout/releases/tag/v0.1.0
