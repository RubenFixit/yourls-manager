# yourls-manager: planned work

Work that is not built yet. Each entry stands alone (problem, proposal or options, open questions) so it can move to a GitHub issue as is. When an entry ships, delete it here; [CHANGELOG.md](../CHANGELOG.md) records what was built.

How ym works today: [README](../README.md) and the [command reference](cli.md).

Rows are in priority order.

| Item | Status |
|---|---|
| [Template: GitLab CI hardening](#template-gitlab-ci-hardening) | Released in template v0.8.0; a check-updates run remains (set up with `ym ci setup`) |
| [Publish to PyPI](#publish-to-pypi) | Next |
| [Adopt and plugin-edit follow-ups](#adopt-and-plugin-edit-follow-ups) | Proposed; found adopting redirect-services into sms-yourls |
| [Deploy history: `ym release list --target`](#deploy-history-ym-release-list---target) | Proposed |
| [Rollback](#rollback) | Idea; manual today; after deploy history |
| [CI follow-ups](#ci-follow-ups) | Proposed, two small items |
| [Server-side plugin state](#server-side-plugin-state) | Undecided: own companion plugin or existing plugin |
| [More target types](#more-target-types) | Open: add a type when a site needs it |
| [Out of scope](#out-of-scope) | Decided against |

---

## Template: GitLab CI hardening

**Problem.** The template's `ci_provider = gitlab` pipeline (deploy, check-updates, publish) has only been rendered and checked in the template's CI; it has never run on GitLab.

**Done in yourls-site-template (`[Unreleased]`):** a `check` job (`ym doctor --local`, `ym build`) on merge requests and branch pushes; schema validation of the rendered CI files (`check-jsonschema`); `check-updates` leaves an up-to-date `ym/upstream-updates` branch alone; the CI files moved to their real paths with `_exclude` in `copier.yml`.

**Verified on gitlab.com** with sms-yourls (the YOURLS site split out of redirect-services; artifact-only): `check` on a branch push (it caught a real bug: the template's `.gitignore` kept a vendored plugin file out of Git, fixed with `!/yourls/**`), `publish` on an rc and a final tag (packages + release; the CI archive is byte-identical to a local `ym artifact build`).

**Remaining.**
- `check-updates` from a pipeline schedule, twice (the second run must not push): `ym ci setup` creates the schedule and token (ym `[Unreleased]`).
- `check` on a merge request; a CI deploy (`ci = true` target) once a site has one.

Open questions:
- Should the GitHub workflows get the same branch/pull request check job?

---

## Publish to PyPI

**Problem.** ym is installed from a git tag (`uv tool install git+https://...`) and sites pin it through `[tool.uv.sources]`. That needs git at install time, and dependency bots can't see new releases.

**Proposal.**
- Confirm the `yourls-manager` name is free on PyPI (and TestPyPI).
- A `publish.yml` workflow on `v*` tags: run the tests, check the tag equals `__version__`, `uv build` once, `uv publish` with trusted publishing (no stored token), and attach the wheel and sdist to a GitHub release. Dry run on TestPyPI with an rc tag first.
- CI: add Python 3.14 to the matrix and classifiers, check the package metadata, keep the `ym-helper.php` wheel check, build a wheel from the sdist; add a Dependabot config and bring the action versions up to the template's.
- Docs and hints: `uv tool install yourls-manager`; the git URL stays documented for unreleased versions.
- Template: sites pin `yourls-manager==X.Y.Z` from PyPI instead of the git source; existing sites move with `ym template update`.

Open questions:
- Keep the git source as a template option (e.g. for testing unreleased ym)?

---

## Server-side plugin state

**Problem.** Without a PHP CLI (sftp targets) ym can't read or set which plugins are active, so `ym adopt` asks the user, `ym deploy` prints manual admin steps, and `ym doctor` can't check the active set. On any target, changes made in the YOURLS admin are invisible to ym. A plugin running inside YOURLS could close that gap.

**To decide first:** build our own companion plugin (option A) or integrate with an existing plugin (option B).

### Option A: our own companion plugin

A minimal plugin that writes YOURLS state into a JSON file in the target's state_dir whenever it changes (hooks like `activated_plugin` / `deactivated_plugin`, plus on version change): active plugins, YOURLS version, DB version. ym would read it over SFTP for adopt/doctor/deploy checks.

Open questions:
- How the plugin learns the state_dir path (a constant in the server-local config written by `ym target config`?).
- Whether it should also apply a desired active set that ym deploys (on the next admin request), which would remove the manual sftp steps.
- How it is shipped: by the template in `overlay/`, or added by `ym build`.

### Option B: integrate with an existing plugin

Candidate: https://github.com/gioxx/YOURLS-PluginManager (manages plugins from the YOURLS admin); others may be in the awesome-yourls catalog (`ym plugin search`). ym would read and update that plugin's manifest file as well as its own `site.toml` / `yourls.lock.json`, and keep the two in sync on deploy.

Open questions, to settle first by reading that repo:
- Manifest location, format and fields (name, source URL, version/ref, active state?), and whether it lives in the web root (deployed) or the database.
- Direction of sync: is site.toml the source of truth (ym writes the manifest at build/deploy), or can changes made in the admin through PluginManager flow back into site.toml (e.g. read the live manifest on deploy/adopt and report or import differences)?
- How PluginManager installs plugins (download into user/plugins?) and how that interacts with ym deleting files it didn't deploy (rsync `--delete` vs the sftp manifest).
- Whether ym should vendor PluginManager itself as a regular `[[plugins]]` entry.

Likely shape: a `plugin_manager` adapter that maps `[[plugins]]` <-> manifest entries, writes the manifest into `dist/` at build time, reads the deployed manifest during `ym adopt` / before `ym deploy` to detect admin-side changes, and `ym plugin add/remove/enable/disable` keep both in step.

---

## Adopt and plugin-edit follow-ups

Found while adopting redirect-services' YOURLS tree into the sms-yourls site (`ym adopt --from-dir`).

- **Leftovers from older YOURLS releases.** The install had 79 files under `includes/vendor/` that are in older YOURLS tags but not in the installed one (its old update script never deleted files). Adopt copied them into `overlay/` as site files. Proposal: check added files against the core repo's history; files that only exist in older tags are reported as stale and not copied.
- **Adopted plugins with a known upstream.** Adopt copies every non-bundled plugin into `overlay/`, even when its header names a git repo. Converting them by hand took `ym plugin remove`, deleting the overlay copy, `ym plugin add URL --ref <commit>`, then diffing to keep local patches in `overlay/`. Proposal: an adopt option (or a `ym plugin manage NAME --repo URL`) that finds the upstream commit matching the installed copy and keeps only the differing files in `overlay/`.
- **Vendored files ignored by Git.** The template's unanchored `*.sql` rule kept a plugin's SQL file out of Git; `ym doctor` passed locally and only the CI checkout showed the drift. Proposal: `ym core` / `ym plugin sync` and `ym doctor --local` warn about vendored files that `git check-ignore` matches. (Template fixed: `!/yourls/**`.)
- **`ym plugin remove` leaves comments behind.** The comment lines adopt writes above a `[[plugins]]` block stay in site.toml after the block is removed.
- **`ym plugin add` appends at the end of site.toml**, after the deploy-target comments, instead of after the last `[[plugins]]` block.
- **Template wording:** the commented-out prod target in `site.toml` mentions GitHub Actions and `ym ci setup` on GitLab sites.

---

## Deploy history: `ym release list --target`

**Problem.** `ym release list` shows where each release is deployed from the local `deployed/<target>` branches. Those only exist in the clone that deployed (or where they were pushed with `push_markers`), skip dirty deploys, and keep only the latest deploy per target. The server's `release.json` also holds only the latest deploy.

**Proposal.**
- Every deploy, rsync and sftp, appends one line to `history.jsonl` next to `release.json` on the server (same fields as `release.json`).
- `ym release list --target T` (repeatable) reads it and lists each deploy to T: time, tag or commit, dirty / forced, ym version, and backup for rsync targets.
- `ym release list` without `--target` stays as it is (local, offline).
- Both forms get `--json`, like `target list`, `plugin list` and `update --check`.

This replaces the earlier plan for a separate `ym releases TARGET` command, which didn't fit the noun + verb command structure from 0.7.

Open questions:
- Retention: keep every line, or cap like `backup_keep`?
- Targets deployed before this exists: seed the history from the current `release.json`?
- Should `ym doctor TARGET` use it (e.g. the last deploy was forced or dirty)?

---

## Rollback

**Problem.** Rolling back is manual: check out a previous release tag, `ym deploy TARGET`, and restore the DB dump by hand if a DB upgrade ran (rsync targets; sftp targets have no backups).

**Idea.** `ym deploy TARGET --ref TAG` builds the site at TAG in a temporary worktree (as `ym artifact build --ref` does) and deploys it, without a checkout. A DB restore would need a `restore` command in the PHP helper and is the risky part.

Open questions:
- Is `--ref` enough, with the restore left manual and documented?
- If restore is automated: which dump (the one taken before the deploy being undone, found via the deploy history), and what confirmation is required.

---

## CI follow-ups

Two independent items left over from `ym ci setup` (GitHub and GitLab):

- **Password-only SFTP hosts.** Store `YM_<TARGET>_PASSWORD` as an environment secret for hosts that don't accept keys. Today the CI key replaces the password, so such hosts can't deploy from CI.
- **Environment protection.** Required reviewers and tag policies on the `production` / `staging` environments are configured by hand (GitLab: `ym ci setup` protects the `v*` tags, nothing more). Decide whether `ym ci setup` should offer them or the site README should just document them.

---

## More target types

`deploy.TARGET_TYPES` (`rsync`, `sftp`) takes further deploy target types. Release artifacts (0.8) cover services that package YOURLS themselves (they pin the published archive), but hosts that ym deploys to directly may still need new types. Add one when a site needs it.

Candidates:
- FTP/FTPS, for shared hosts without SFTP.
- Docker: build an image, or sync into a volume.

A new type needs its `site.toml` keys and validation, a transport, the deploy flow (backup, DB upgrade, plugin activation, or printed manual steps like sftp), and `adopt` / `target login` / `target config` / `doctor` support where it applies.

---

## Out of scope

Decided against for now; reopen with a concrete need.

- **Uploading artifacts from ym.** ym builds artifacts but never publishes them; publishing is a CI job in the template, so ym needs no forge APIs or tokens.
- **Per-consumer layout options** (custom vendor paths, plugin install destinations, preserve lists). Consumers adapt to the site repo layout; anything outside `user/plugins` goes in `overlay/`.
