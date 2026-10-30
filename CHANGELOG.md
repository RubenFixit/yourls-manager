# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `ym core`, `ym plugin sync` and `ym doctor` (also `--local`) warn about vendored files under `yourls/` that Git ignores and doesn't track, naming the `.gitignore` rule (`source:line: pattern`) for each. Such files are on disk but never committed, so a clean checkout (CI) fails the drift check: the template's unanchored `*.sql` rule did this to a plugin's SQL file before template v0.8.0. `ym core` checks core, `ym plugin sync` the plugins it vendored; tracked files are never reported.

### Fixed

- `ym plugin add` inserts the new `[[plugins]]` block after the last existing one (its keys and the comments directly under it) instead of at the end of `site.toml`, after `[build]`, `[artifact]` and the targets. Commented-out examples and the comments above the next table stay where they are. `ym adopt` uses the same placement, so its entries now go before the template's commented-out examples rather than after them.
- `ym plugin remove` also deletes the comment lines directly above the block when they name the plugin or `ym adopt` wrote them ("Adopted by `ym adopt`…", "Plugin URI…"). Section comments that don't name the plugin stay, separated from what follows by a blank line.

## [0.9.0] - 2026-10-30

GitLab support for the repo and CI commands: glab for the login, python-gitlab for the API. Verified on gitlab.com with a real artifact-only site (check-updates setup, including the project access token).

### Added

- GitLab: `ym repo create`, `ym ci setup` and `ym ci status` work on sites made with the template's `ci_provider = gitlab`, for gitlab.com and self-managed GitLab. glab (the GitLab CLI) owns the login: ym offers `glab auth login` and uses glab's token (from `GITLAB_TOKEN`, glab's config, or its keyring via `glab auth status --show-token`) and `api_host` / `api_protocol`, and retries once after letting glab refresh an expired OAuth token. The API calls go through python-gitlab, a new runtime dependency imported only by these commands.
  - `ym repo create` creates the project from `origin`, else the template's `repo_url` answer, else `--host` (new) / `--owner` / `--name` (groups and subgroups work), adds `origin` (SSH, or HTTPS when glab's `git_protocol` is https) and pushes the current branch.
  - `ym ci setup` creates the `production` / `staging` environments, stores `DEPLOY_SSH_KEY` and `DEPLOY_KNOWN_HOSTS` as File-type, protected, environment-scoped variables, protects the `v*` tags, and sets `ci = true` like on GitHub. It also sets up check-updates (unless `--skip-updates`, new), with or without deploy targets: the `ym check-updates` pipeline schedule and the masked `YM_UPDATE_TOKEN` variable, from a new project access token or, where GitLab refuses to create one, a token you paste (hidden prompt).
  - `ym ci status` and `ym doctor` check the environments, variables and protected tags of `ci = true` targets on GitLab (instead of reporting "GitLab CI (not checked)"); `ym ci status` also reports the check-updates schedule and token.
- `ym ci setup --rotate` offers (on a terminal) to remove the environment's previous CI keys (`ym-ci-<slug>-<env>-*`) from the server's `authorized_keys`, over ssh or SFTP. GitHub and GitLab.

### Fixed

- Tests that only run with a real sshd (`ym plugins` instead of `ym plugin apply`, an old `target login` message) or outside UTC (artifact metadata time) were out of date.

## [0.8.1] - 2026-10-30

### Added

- Releases bump the version with `uvx bump-my-version bump patch|minor|major` (`[tool.bumpversion]` in `pyproject.toml`): the package, the PHP helper, the docs example and the changelog heading and links. `.gitattributes` keeps line endings LF, also for tools that write CRLF on Windows.

### Fixed

- `docs/cli.md` came out differently on Python 3.13 and later (argparse writes `-m, --message MESSAGE` there, `-m MESSAGE, --message MESSAGE` before), so its freshness test failed in CI. The generator now formats option names itself, and turns off Python 3.14's colored help.

## [0.8.0] - 2026-10-29

Release artifacts for sites that ym doesn't deploy, and the template's CI provider choice (template v0.7.0). The 0.7 deprecated spellings are gone.

### Added

- `ym artifact build [--ref REF] [--out DIR] [--format F ...] [--allow-untagged]`: build a release tag's web root into reproducible `.tar.gz` / `.zip` archives (no top-level directory; sorted entries, fixed times, owners and modes, so rebuilding a tag gives the same bytes), with a `<archive>.sha256` per archive and a `<name>.json` with the release metadata (site, version, tag, rc, commit, core, plugins, ym version, file hashes). Refuses commits without a release tag and dirty trees. Default output: `dist-artifacts/`. Nothing is uploaded: publishing is a CI job.
- `[artifact]` in `site.toml`: `enabled`, `formats` (`tar.gz`, `zip`) and `name` (`{slug}`, `{version}`, `{tag}`).
- `ym template update --data K=V` (repeatable): change a template answer, e.g. `--data ci_provider=gitlab` or `--data release_artifact=true` (passed to `copier update`).
- `ym new` prints release and artifact next steps for an artifact-only site (`[artifact] enabled = true`, no targets).
- `docs/cli.md`: command reference generated from the argparse help texts by `python -m yourls_manager.clidoc` (`--check` exits 1 when it is stale; a test enforces it).
- `docs/planned.md`: work that is not built yet, one self-contained entry per item.
- Docs pages: `docs/site-repo.md` (layout, ownership, the full `site.toml` reference), `docs/onboarding.md`, `docs/deploying.md`, `docs/releases.md`.
- `ym doctor`: sites with `[artifact] enabled = true` and no deploy targets are reported as artifact-only (ok, not a warning), and doctor warns when no CI file of the site's `ci_provider` runs `ym artifact build`.

### Changed

- `ym repo create`, `ym ci setup` and `ym ci status` refuse sites made with the template's `ci_provider = gitlab` and point to the site README's manual GitLab setup; `ym ci` also refuses `ci_provider = none`. `ym doctor` reports `ci = true` targets on GitLab sites as GitLab CI (not checked) and warns about them when the site has no CI.
- Every command and argument has help text; `--help` descriptions carry the details from the README's command tables, and `ym --help` lists the exit codes.
- The README covers only the summary and getting started; the command tables and the topic sections moved to `docs/`.

### Removed

- The pre-0.7 spellings that 0.7 still accepted with a warning: `ym setup`, `ym ssh-setup`, `ym server-config`, `ym plugins`, `ym sync`, `ym check`, `ym status`, `ym template-update`, `ym core --version V` and `ym release --promote` / `--list`. `ym init` no longer reads the `ym setup` progress files (`.git/ym-setup.json`, `setup.json`).

  | Before 0.7 | Now |
  |---|---|
  | `ym setup [DIR]` | `ym init [DIR]` |
  | `ym ssh-setup TARGET` | `ym target login TARGET` |
  | `ym server-config TARGET` | `ym target config TARGET` |
  | `ym plugins TARGET` | `ym plugin apply TARGET` |
  | `ym sync [--plugin NAME ...]` | `ym plugin sync [NAME ...]` |
  | `ym core --version V` | `ym core V` |
  | `ym check [--json]` | `ym update --check [--json]` |
  | `ym status` | `ym doctor --local` (plugin details: `ym plugin list`) |
  | `ym template-update` | `ym template update` |
  | `ym release --promote` / `--list` | `ym release promote` / `ym release list` |

- `docs/design.md`: the design notes for what is built are replaced by the command reference, the docs pages and code comments; unbuilt work moved to `docs/planned.md`.

### Fixed

- Two hints still described the pre-0.7 manual steps: a missing server-local config on an rsync target now points at `ym target config`, and `ym new` without a deploy host points at `ym target add prod`.

## [0.7.0] - 2026-10-29

One consistent command set: noun + verb groups (`target`, `plugin`, `template`, `release`). Old spellings keep working in 0.7 with a warning.

### Added

- `ym target login TARGET` and `ym target config TARGET`, the new names of `ym ssh-setup` and `ym server-config` (same options). For sftp targets, `target login` accepts a working password login (`YM_<TARGET>_PASSWORD`) and only offers a key instead of requiring one.
- `ym target add NAME [--type] [--host] [--user] [--port] [--path] [--php] [--url] [--accept] [--protect]`: add a deploy target to `site.toml` without hand-editing (asks for missing values on a terminal; `prod` defaults to `accept = "release"`; `protect` defaults to `.well-known/` as in the template).
- `ym target list [--json]`: the targets with type, location, URL, policy and CI flag.
- The `ym init` wizard offers `ym target add prod` when the site has no `prod` target.
- `ym plugin sync [NAME ...]`, the new name of `ym sync` (plugin names are positional).
- `ym core VERSION`: the version is positional (`ym core latest`).
- `ym update --force`: passed to the core and plugin steps (uncommitted changes in `yourls/`).
- `ym update --check [--json]`, the new name of `ym check` (read-only, exit 10 on updates).
- `ym init [DIR]`, the new name of the `ym setup` wizard. Its progress files are now `.git/ym-init.json` and `$XDG_STATE_HOME/yourls-manager/init.json`; an unfinished `ym setup` is still found and resumed (the old files are replaced on the next save).
- `ym template update`, the new name of `ym template-update` (same options).
- `ym release promote` and `ym release list`, replacing the `--promote` / `--list` flags. Plain `ym release [--rc]` still tags the next release.
- `ym doctor --local`: only the tools and the site repo, no target checks (offline). The report now names the site and shows the locked core commit; drift problems are listed one per line.

### Changed

- Hints and next steps printed by `doctor`, `ci`, `adopt`, `new`, `deploy` and the wizard use the new names. The module `sshsetup` is now `login` (`check_ssh` → `check_login`).
- `ym deploy --help` groups the options by target type (all / rsync / sftp) and summarizes what each type does.

### Deprecated

Old command spellings still work in 0.7 but print a warning naming the new command; they are hidden from `ym --help` and will be removed in 0.8.

- `ym plugins TARGET`: use `ym plugin apply TARGET`.
- `ym ssh-setup TARGET`: use `ym target login TARGET`.
- `ym server-config TARGET`: use `ym target config TARGET`.
- `ym sync [--plugin NAME ...]`: use `ym plugin sync [NAME ...]`.
- `ym core --version V`: use `ym core V` (the option clashed with the global `ym --version`).
- `ym check`: use `ym update --check`.
- `ym setup [DIR]`: use `ym init [DIR]`.
- `ym template-update`: use `ym template update`.
- `ym release --promote` / `ym release --list`: use `ym release promote` / `ym release list`.
- `ym status`: use `ym doctor --local` (and `ym plugin list` for plugin details). The alias runs both, and now exits 1 on drift.

## [0.6.0] - 2026-10-29

GitHub: store the site there, and opt-in deploys from GitHub Actions.

### Added

- `ym repo create [--name] [--owner] [--public] [--dry-run]`: create the site's GitHub repository with the GitHub CLI (private by default), add it as `origin` (or create the one `origin` already names) and push the current branch, without tags.
- `ym ci setup [TARGET ...] [--repo] [--rotate] [--dry-run]`: enable deploys from GitHub Actions: create the `production` / `staging` environment, generate a dedicated CI key, install it on the server over the working login and check it, store it as `DEPLOY_SSH_KEY`, store the host keys from `~/.ssh/known_hosts` (else `ssh-keyscan` with a fingerprint confirmation) as `DEPLOY_KNOWN_HOSTS`, and set `ci = true` on the target. Offers `gh auth login`, and to create the repository when there is none.
- `ym ci status [TARGET ...]`: check the environments and secrets of `ci = true` targets.
- Target key `ci = true`: the site template's deploy workflow only deploys these targets. `ym doctor` checks their GitHub environment and secrets (when `gh` is logged in).

### Changed

- `ym release --push` stops before tagging when there is no `origin` remote, and warns when the released commit isn't on a remote branch.
- A site without any git remote is a normal setup: `ym doctor` reports it as ok ("local only"), and `ym deploy` no longer warns that HEAD isn't pushed (it still does when a remote exists). `[release] push_markers` without `origin` warns instead of failing.

### Fixed

- `ym doctor` crashed instead of reporting when a target's server couldn't be queried.

## [0.5.0] - 2026-10-28

Plugin catalog and `ym doctor`.

### Added

- `ym plugin search [QUERY] [--category SLUG] [--refresh] [--json]`: search the awesome-yourls plugin catalog (cached for a day); plugins already in site.toml are marked.
- `ym plugin add NAME`: add a plugin by its catalog name (exact, case-insensitive or unique prefix). For names and URLs, plugin.php is now also found one directory down (asks on a terminal when there are several, else `--path`), the plugin header (name, version, description, requirements) is shown, and `--no-sync` only edits site.toml. Directory names used by core's bundled plugins or `overlay/` are refused.
- `ym doctor [TARGET]`: read-only report of tools, site repo (site.toml, lock and drift, uncommitted and unpushed changes) and targets (login, PHP, server config, https, install / upgrade state, deployed commit vs HEAD); exits 1 on failures.
- `ym plugin list` shows each plugin's header version; `--json`, and `--check` (asks upstream for newer commits, exit 10 if any).
- `ym plugin apply TARGET`, the new name of `ym plugins TARGET` (kept as an alias).

### Changed

- New dependency: PyYAML (to read the plugin catalog).

## [0.4.3] - 2026-10-28

### Added

- `ym plugin add URL [--name] [--ref] [--path] [--inactive]`: add a plugin from a clone URL or a GitHub/GitLab/Codeberg/Bitbucket project page (`/tree/<ref>/<dir>` sets ref and path); rolled back if the plugin can't be vendored.

## [0.4.2] - 2026-10-28

### Added

- `ym plugin list`, `ym plugin enable NAME`, `ym plugin disable NAME` (edits `active` in site.toml, keeping comments; applied on the next deploy).
- `ym plugin remove NAME [--keep-files]`: delete the plugin's `[[plugins]]` block and its vendored copy; the next deploy deactivates it and removes its files (its data stays).

## [0.4.1] - 2026-10-28

### Added

- `ym update [--commit] [--core-only | --plugins-only]`: update core to the latest release and plugins to their newest commits, build, and summarize (or commit) the changes.

### Fixed

- `ym release --promote` tagged the rc tag instead of its commit, so on older Git the final tag wasn't seen at HEAD.

## [0.4.0] - 2026-10-28

Releases and deploy policy.

### Added

- `ym release`: tags `v<yourls>-<rev>` (`--rc` for `-rc.<n>`, `--promote` to finalize the newest rc) on a clean, drift-free tree, with release notes from the lock diff and commit log; `--push`, `--dry-run`, `-m`, `--list` (releases and where they're deployed).
- Target key `accept = "any" | "release"`: `release` targets only deploy final release tags.
- `ym deploy --allow-dirty` and `--force`; release.json records `tag` and `forced`.
- After a deploy of a clean tree, the local branch `deployed/<target>` points at the deployed commit; `[release] push_markers = true` pushes it.

### Changed

- `ym deploy` refuses uncommitted changes unless `--allow-dirty`, and warns when HEAD isn't on a remote branch.

## [0.3.0] - 2026-10-28

SFTP targets: deploy and adopt sites on hosts that offer SFTP but no shell.

### Added

- `type = "sftp"` deploy targets (`host`, `user`, `port`, `path`, `config_file`, `state_dir`, `protect`, `url`). Login via ssh-agent, key files (`~/.ssh/config` is honored) or a password from `YM_<TARGET>_PASSWORD` / a terminal prompt; host keys from `~/.ssh/known_hosts` (unknown keys are confirmed on a terminal, changed keys always fail).
- SFTP deploys track deployed files in `<state_dir>/manifest.json`: only new and changed files are uploaded (atomically, entry points last), and only files ym deployed are deleted. `--verify-remote` re-uploads files that were changed on the server. After the deploy, ym checks over HTTP whether YOURLS needs `admin/install.php` or `admin/upgrade.php` and prints the plugins to activate in the admin UI.
- `ym adopt --from-target` for sftp targets (recursive SFTP download), recording the adopted install's manifest on the server. New `--active A,B,...` (also for rsync targets and `--from-dir`); on a terminal ym asks per plugin when the active set can't be read.
- `ym server-config` over SFTP, hashing the admin password locally with bcrypt (YOURLS `phpass:` format; verified with PHP `password_verify` and YOURLS' `yourls_check_password_hash`).
- `ym ssh-setup` for sftp targets: checks with an SFTP login and installs the key into `.ssh/authorized_keys` over SFTP (works for SFTP-only accounts).
- `ym setup` checks sftp targets with an SFTP login and lists the manual admin steps.
- Runtime dependencies `paramiko` and `bcrypt` (imported only when needed).

### Changed

- `ym plugins` explains the manual steps for sftp targets instead of running.
- An sftp deploy that leaves an install or upgrade pending in the browser skips verify and succeeds; run `ym verify TARGET` after finishing it.

## [0.2.1] - 2026-10-28

### Added

- `ym ssh-setup TARGET`: check SSH key login; create a key (`ssh-keygen`) and install it (`ssh-copy-id`, password asked once) when it fails.
- `ym setup` resumes: progress is saved in `.git/ym-setup.json` and the last unfinished site is remembered, so re-running `ym setup` (or `ym setup DIR`) continues after an error and skips finished steps. The wizard also checks SSH key login and offers `ym ssh-setup`.

### Changed

- SSH "Permission denied" / host key errors now point to `ym ssh-setup`.

## [0.2.0] - 2026-10-28

Onboarding commands that work from the globally installed tool, before a site repo exists.

### Added

- `ym new`: create a site repo from `yourls-site-template` (`--template` / `YM_TEMPLATE`, `--vcs-ref`, `--data`, `--defaults`), then vendor core and plugins (`--no-vendor` skips this), `git init` with an initial commit (`--no-git` skips this), and try `uv lock`.
- `ym template-update`: `copier update` in a clean site repo, with optional `--manager-version` to bump the yourls-manager pin. Then `uv lock` and a diff summary.
- `ym server-config TARGET`: write the server-local config (DB settings, site URL, fresh cookie key, one admin user hashed by the target's PHP) with mode 0600. Passwords come from `YM_DB_PASSWORD` / `YM_ADMIN_PASSWORD` or a prompt. `--force` keeps a timestamped backup before overwriting. `--from-existing` relocates an existing install's config.
- `ym adopt (--from-target TARGET | --from-dir PATH)`: bring an existing YOURLS install under management. Detects the core version, copies modified and extra files and unmanaged plugins into `overlay/`, records the active plugins, checks build parity and writes `adopt-review/REPORT.md`.
- `ym setup`: interactive wizard for a new site or an adoption. It prints the equivalent command for each step.

### Changed

- README restructured around a Quick start (new site / adopt existing install) and an update how-to.

## [0.1.0] - 2026-10-27

### Added

- `ym` / `yourls-manager` CLI with `status`, `check`, `core`, `sync`, `build`, `deploy`, `verify` and `plugins` commands.
- `site.toml` loading and validation with friendly, key-specific errors and unknown-key warnings.
- `yourls.lock.json` with exact core/plugin commits and per-tree sha256 hashes; stable output.
- Vendoring via `git ls-remote` + shallow fetch of exact commits (full-clone fallback), atomic directory swaps, anchored core exclude globs, symlink-escape protection.
- `rsync` deploy target (remote over ssh, or local when `host` is omitted) with preflight, database backup and pruning, rsync `--delete` with protected paths, YOURLS DB upgrade, exact active-plugin set, release record outside the web root, and verification.
- `ym-helper.php` server-side helper (`info`, `dump`, `upgrade`, `plugins`, `version`) with a JSON-on-stdout contract.
- Redirect verification from `tests/redirects.toml` plus a JSON health check.

[Unreleased]: https://github.com/RubenFixit/yourls-manager/compare/v0.9.0...HEAD
[0.9.0]: https://github.com/RubenFixit/yourls-manager/compare/v0.8.1...v0.9.0
[0.8.1]: https://github.com/RubenFixit/yourls-manager/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/RubenFixit/yourls-manager/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/RubenFixit/yourls-manager/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/RubenFixit/yourls-manager/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/RubenFixit/yourls-manager/compare/v0.4.3...v0.5.0
[0.4.3]: https://github.com/RubenFixit/yourls-manager/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/RubenFixit/yourls-manager/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/RubenFixit/yourls-manager/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/RubenFixit/yourls-manager/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/RubenFixit/yourls-manager/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/RubenFixit/yourls-manager/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/RubenFixit/yourls-manager/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/RubenFixit/yourls-manager/releases/tag/v0.1.0
