# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - Unreleased (date TBD)

Onboarding commands that work from the globally installed tool, before a site repo exists.

### Added

- `ym new`: create a site repo from `yourls-site-template` (`--template` / `YM_TEMPLATE`,
  `--vcs-ref`, `--data`, `--defaults`), then vendor core and plugins (`--no-vendor` skips this),
  `git init` with an initial commit (`--no-git` skips this), and try `uv lock`.
- `ym template-update`: `copier update` in a clean site repo, with optional
  `--manager-version` to bump the yourls-manager pin. Then `uv lock` and a diff summary.
- `ym server-config TARGET`: write the server-local config (DB settings, site URL, fresh
  cookie key, one admin user hashed by the target's PHP) with mode 0600. Passwords come from
  `YM_DB_PASSWORD` / `YM_ADMIN_PASSWORD` or a prompt. `--force` keeps a timestamped backup
  before overwriting. `--from-existing` relocates an existing install's config.
- `ym adopt (--from-target TARGET | --from-dir PATH)`: bring an existing YOURLS install under
  management. Detects the core version, copies modified and extra files and unmanaged plugins
  into `overlay/`, records the active plugins, checks build parity and writes
  `adopt-review/REPORT.md`.
- `ym setup`: interactive wizard for a new site or an adoption. It prints the equivalent
  command for each step.

### Changed

- README restructured around a Quick start (new site / adopt existing install) and an
  update how-to.

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
[0.2.0]: https://github.com/RubenFixit/yourls-manager/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/RubenFixit/yourls-manager/releases/tag/v0.1.0
