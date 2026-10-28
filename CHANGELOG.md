# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-10-27

### Added

- `ym` / `yourls-manager` CLI with `status`, `check`, `core`, `sync`, `build`, `deploy`,
  `verify` and `plugins` commands.
- `site.toml` loading and validation with friendly, key-specific errors and unknown-key
  warnings.
- `yourls.lock.json` with exact core/plugin commits and per-tree sha256 hashes; stable output.
- Vendoring via `git ls-remote` + shallow fetch of exact commits (full-clone fallback), atomic
  directory swaps, anchored core exclude globs, symlink-escape protection.
- `rsync` deploy target (remote over ssh, or local when `host` is omitted) with preflight,
  database backup and pruning, rsync `--delete` with protected paths, YOURLS DB upgrade,
  exact active-plugin set, release record outside the web root, and verification.
- `ym-helper.php` server-side helper (`info`, `dump`, `upgrade`, `plugins`, `version`) with a
  JSON-on-stdout contract.
- Redirect verification from `tests/redirects.toml` plus a JSON health check.

[Unreleased]: https://github.com/RubenFixit/yourls-manager/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/RubenFixit/yourls-manager/releases/tag/v0.1.0
