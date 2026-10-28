# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.4.1] - 2026-10-28

### Added

- `ym update [--commit] [--core-only | --plugins-only]`: update core to the latest release and
  plugins to their newest commits, build, and summarize (or commit) the changes.

### Fixed

- `ym release --promote` tagged the rc tag instead of its commit, so on older Git the final
  tag wasn't seen at HEAD.

## [0.4.0] - 2026-10-28

Releases and deploy policy.

### Added

- `ym release`: tags `v<yourls>-<rev>` (`--rc` for `-rc.<n>`, `--promote` to finalize the
  newest rc) on a clean, drift-free tree, with release notes from the lock diff and commit
  log; `--push`, `--dry-run`, `-m`, `--list` (releases and where they're deployed).
- Target key `accept = "any" | "release"`: `release` targets only deploy final release tags.
- `ym deploy --allow-dirty` and `--force`; release.json records `tag` and `forced`.
- After a deploy of a clean tree, the local branch `deployed/<target>` points at the deployed
  commit; `[release] push_markers = true` pushes it.

### Changed

- `ym deploy` refuses uncommitted changes unless `--allow-dirty`, and warns when HEAD isn't
  on a remote branch.

## [0.3.0] - 2026-10-28

SFTP targets: deploy and adopt sites on hosts that offer SFTP but no shell.

### Added

- `type = "sftp"` deploy targets (`host`, `user`, `port`, `path`, `config_file`, `state_dir`,
  `protect`, `url`). Login via ssh-agent, key files (`~/.ssh/config` is honored) or a
  password from `YM_<TARGET>_PASSWORD` / a terminal prompt; host keys from
  `~/.ssh/known_hosts` (unknown keys are confirmed on a terminal, changed keys always fail).
- SFTP deploys track deployed files in `<state_dir>/manifest.json`: only new and changed
  files are uploaded (atomically, entry points last), and only files ym deployed are deleted.
  `--verify-remote` re-uploads files that were changed on the server. After the deploy, ym
  checks over HTTP whether YOURLS needs `admin/install.php` or `admin/upgrade.php` and prints
  the plugins to activate in the admin UI.
- `ym adopt --from-target` for sftp targets (recursive SFTP download), recording the adopted
  install's manifest on the server. New `--active A,B,...` (also for rsync targets and
  `--from-dir`); on a terminal ym asks per plugin when the active set can't be read.
- `ym server-config` over SFTP, hashing the admin password locally with bcrypt (YOURLS
  `phpass:` format; verified with PHP `password_verify` and YOURLS'
  `yourls_check_password_hash`).
- `ym ssh-setup` for sftp targets: checks with an SFTP login and installs the key into
  `.ssh/authorized_keys` over SFTP (works for SFTP-only accounts).
- `ym setup` checks sftp targets with an SFTP login and lists the manual admin steps.
- Runtime dependencies `paramiko` and `bcrypt` (imported only when needed).

### Changed

- `ym plugins` explains the manual steps for sftp targets instead of running.
- An sftp deploy that leaves an install or upgrade pending in the browser skips verify and
  succeeds; run `ym verify TARGET` after finishing it.

## [0.2.1] - 2026-10-28

### Added

- `ym ssh-setup TARGET`: check SSH key login; create a key (`ssh-keygen`) and install it
  (`ssh-copy-id`, password asked once) when it fails.
- `ym setup` resumes: progress is saved in `.git/ym-setup.json` and the last unfinished site is
  remembered, so re-running `ym setup` (or `ym setup DIR`) continues after an error and skips
  finished steps. The wizard also checks SSH key login and offers `ym ssh-setup`.

### Changed

- SSH "Permission denied" / host key errors now point to `ym ssh-setup`.

## [0.2.0] - 2026-10-28

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

[Unreleased]: https://github.com/RubenFixit/yourls-manager/compare/v0.4.1...HEAD
[0.4.1]: https://github.com/RubenFixit/yourls-manager/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/RubenFixit/yourls-manager/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/RubenFixit/yourls-manager/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/RubenFixit/yourls-manager/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/RubenFixit/yourls-manager/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/RubenFixit/yourls-manager/releases/tag/v0.1.0
