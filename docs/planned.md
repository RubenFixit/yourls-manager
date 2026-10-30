# yourls-manager: planned work

Work that is not built yet. Each entry stands alone (problem, proposal or options, open questions) so it can move to a GitHub issue as is. When an entry ships, delete it here; [CHANGELOG.md](../CHANGELOG.md) records what was built.

How ym works today: [README](../README.md) and the [command reference](cli.md).

The entries are grouped by how they ship:

- [Next patch release](#next-patch-release): bugs and small fixes that change no command or file format (0.9.x).
- [Features](#features): new behavior, each in a minor release (0.x.0), in priority order.
- [Still to verify](#still-to-verify): shipped work that hasn't run on real infrastructure in every variant yet.
- [Out of scope](#out-of-scope): decided against for now.

---

## Next patch release

| Item | Kind |
|---|---|
| [`ym plugin remove` leaves comments behind](#ym-plugin-remove-leaves-comments-behind) | Bug |
| [`ym plugin add` appends at the end of site.toml](#ym-plugin-add-appends-at-the-end-of-sitetoml) | Bug |

### `ym plugin remove` leaves comments behind

**Problem.** The comment lines `ym adopt` writes above a `[[plugins]]` block ("Adopted by `ym adopt`…", "Plugin URI…") stay in site.toml after `ym plugin remove` deletes the block.

**Fix.** Also remove the comment lines directly above the block, up to the previous blank line, when they belong to it (they name the plugin or were written by adopt).

### `ym plugin add` appends at the end of site.toml

**Problem.** New `[[plugins]]` blocks land at the end of the file, after the `[build]`, `[artifact]` and deploy-target sections and their comments, so the plugin list ends up split in two.

**Fix.** Insert the block after the last `[[plugins]]` block (and its trailing comments), falling back to the end of the file when there is none.

---

## Features

| Item | Status |
|---|---|
| [Run the site's pinned ym](#run-the-sites-pinned-ym) | Next |
| [Report newer ym and template releases](#report-newer-ym-and-template-releases) | Proposed; with or after the pinned ym |
| [Publish to PyPI](#publish-to-pypi) | After the pinned ym |
| [Adopt: skip leftovers from older YOURLS releases](#adopt-skip-leftovers-from-older-yourls-releases) | Proposed; found adopting redirect-services |
| [Adopt: manage plugins with a known upstream](#adopt-manage-plugins-with-a-known-upstream) | Proposed; found adopting redirect-services |
| [Deploy history: `ym release list --target`](#deploy-history-ym-release-list---target) | Proposed |
| [Rollback](#rollback) | Idea; manual today; after deploy history |
| [CI follow-ups](#ci-follow-ups) | Proposed, two small items |
| [Server-side plugin state](#server-side-plugin-state) | Undecided: own companion plugin or existing plugin |
| [More target types](#more-target-types) | Open: add a type when a site needs it |

### Run the site's pinned ym

**Problem.** A site pins ym in `pyproject.toml`, and CI runs that version (`uv run ym`), but a globally installed `ym` ignores the pin. The template and ym are coupled in both directions, so any mismatch shows up:
- **Global ym older than the pin.** Commands the template documents fail with an unhelpful argparse error: an old global ym in sms-yourls answered `ym template update` with `invalid choice: 'template'`. Newer `site.toml` keys are warned about and ignored, so their settings silently do nothing.
- **Global ym newer than the pin.** It writes files that CI then reads with the pinned ym: `site.toml` keys the pin ignores, `yourls.lock.json` (a `lock_version` bump fails CI), and vendored files that may differ from the pin's build, failing CI's drift check or breaking byte-identical artifacts. Local and CI deploys to the same target come from different versions.
- **Template ahead of the pin.** `copier update` reuses the earlier `manager_version` answer, so a site can take a template release that documents commands its pinned ym doesn't have.

**Proposal.**
- In a site repo, read the pinned version from `pyproject.toml` (the `tag` of `yourls-manager` in `[tool.uv.sources]`; later the `==X.Y.Z` requirement, see [Publish to PyPI](#publish-to-pypi)). When it differs from the running ym, re-run the command as `uv run --locked ym ...` in the site (same arguments and exit code; exec on POSIX, a child process on Windows), so the pin always decides what runs. `--locked` matches CI and never rewrites `uv.lock`.
- The first run in a fresh clone syncs the environment (network); later runs work offline from uv's cache. When uv fails (offline with an empty cache, `uv.lock` out of date), its error is shown, followed by a hint naming `YM_NO_DELEGATE=1`. Read-only commands get no exception: that would be another rule to remember, and would silently run a different version.
- No hand-off outside a site repo, when the pin can't be read, when already running from the site's environment, and for `ym new` and `ym template update --manager-version` (which moves the pin).
- Opt-out: `YM_NO_DELEGATE=1` runs the global ym as is (testing an unreleased ym against a real site). It prints one line before commands that write site files or deploy: "this is ym A, the site pins B; CI runs B".
- Missing `uv`: fall back to the old behavior with a warning that names `uv run ym`.
- Global installs from before this can't hand off, so the same release adds a hint for argparse errors: when the pin is newer than the running ym, say so and suggest `uv run ym ...` or `uv tool upgrade`.
- CI: `ym doctor` treats unknown `site.toml` keys as errors when `CI` is set, so a key the pinned ym doesn't know fails loudly.
- Template: the `manager_version` validator rejects tags older than the version that template release was written for, so `copier update` can't keep an older pin.

### Report newer ym and template releases

**Problem.** `ym update --check` and the template's check-updates job look only at YOURLS core and plugins. Nothing reports a newer ym than the site's pin or a newer template than the one it was generated from: Dependabot can't bump the git-tag pin, and template updates are noticed by hand.

**Proposal.**
- `ym update --check` also reports a newer ym tag than the pin (`ym template update --manager-version vX.Y.Z`) and a newer template tag than `_commit` in `.copier-answers.yml` (`ym template update`; also say which ym that template release pins).
- Source: `git ls-remote --tags` on the ym repo and on the template's `_src_path`, like plugin checks: no token, works on GitHub and GitLab. After [Publish to PyPI](#publish-to-pypi), ym's version can come from PyPI, and Dependabot bumps the pin as well.
- Release candidates are ignored unless `--pre` is given. A site pinned to an rc still sees the final release, which is newer.
- Exit code 10 keeps meaning "`ym update` would change something". `ym update` doesn't apply ym or template updates, so they alone don't exit 10; they are printed and included in `--json`. Existing check-updates workflows keep working unchanged.
- check-updates (GitHub and GitLab) reads the `--json` output and reports them without applying them, since a template update can conflict: list them in the update PR / MR body, or open an issue when core and plugins are up to date (one issue, updated by later runs rather than reopened weekly).
- No check on ordinary commands: that would add a network call to every run and to CI logs, and once ym runs the site's pinned version the global install's own version barely matters.

### Publish to PyPI

**Problem.** ym is installed from a git tag (`uv tool install git+https://...`) and sites pin it through `[tool.uv.sources]`. That needs git at install time, and dependency bots can't see new releases.

**Proposal.**
- Confirm the `yourls-manager` name is free on PyPI (and TestPyPI).
- A `publish.yml` workflow on `v*` tags: run the tests, check the tag equals `__version__`, `uv build` once, `uv publish` with trusted publishing (no stored token), and attach the wheel and sdist to a GitHub release. Dry run on TestPyPI with an rc tag first.
- CI: add Python 3.14 to the matrix and classifiers, check the package metadata, keep the `ym-helper.php` wheel check, build a wheel from the sdist; add a Dependabot config and bring the action versions up to the template's.
- Docs and hints: `uv tool install yourls-manager` (and `uv tool upgrade yourls-manager`); the git URL stays documented for unreleased versions.
- Template: sites pin `yourls-manager==X.Y.Z` from PyPI instead of the git source; existing sites move with `ym template update`.

Open questions:
- Keep the git source as a template option (e.g. for testing unreleased ym)?

### Adopt: skip leftovers from older YOURLS releases

**Problem.** redirect-services' install had 79 files under `includes/vendor/` that exist in older YOURLS tags but not in the installed one (its old update script copied new files over the tree and never deleted any). `ym adopt` copied them into `overlay/` as site files, where they would be deployed forever.

**Proposal.** For each file adopt would add to `overlay/`, check the core repo's history (`git log --all -- <path>` on a blobless clone; adopt already fetches the installed tag). Files that only exist in older tags are listed in `adopt-review/REPORT.md` as stale and not copied; `--keep-stale` copies them anyway.

### Adopt: manage plugins with a known upstream

**Problem.** Adopt copies every non-bundled plugin into `overlay/`, even when its header (or the old tooling's manifest) names a git repo. Converting them by hand took, per plugin: `ym plugin remove`, deleting the overlay copy, `ym plugin add URL --ref <commit>`, then diffing against the install to keep local patches in `overlay/`.

**Proposal.** `ym plugin manage NAME --repo URL [--path SUBDIR]`, also offered by adopt when the header's Plugin URI is a git repo: find the upstream commit whose tree matches the installed copy best (newest exact match, else fewest differing files), add the plugin as a managed entry pinned there, and keep only the differing files in `overlay/user/plugins/NAME/` as local patches, listing them.

Open questions:
- Pin the matching commit, or pin the branch (`ref = HEAD`) and lock the commit, so `ym update` moves it later?

### Deploy history: `ym release list --target`

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

### Rollback

**Problem.** Rolling back is manual: check out a previous release tag, `ym deploy TARGET`, and restore the DB dump by hand if a DB upgrade ran (rsync targets; sftp targets have no backups).

**Idea.** `ym deploy TARGET --ref TAG` builds the site at TAG in a temporary worktree (as `ym artifact build --ref` does) and deploys it, without a checkout. A DB restore would need a `restore` command in the PHP helper and is the risky part.

Open questions:
- Is `--ref` enough, with the restore left manual and documented?
- If restore is automated: which dump (the one taken before the deploy being undone, found via the deploy history), and what confirmation is required.

### CI follow-ups

Two independent items left over from `ym ci setup` (GitHub and GitLab):

- **Password-only SFTP hosts.** Store `YM_<TARGET>_PASSWORD` as an environment secret for hosts that don't accept keys. Today the CI key replaces the password, so such hosts can't deploy from CI.
- **Environment protection.** Required reviewers and tag policies on the `production` / `staging` environments are configured by hand (GitLab: `ym ci setup` protects the `v*` tags, nothing more). Decide whether `ym ci setup` should offer them or the site README should just document them.

### Server-side plugin state

**Problem.** Without a PHP CLI (sftp targets) ym can't read or set which plugins are active, so `ym adopt` asks the user, `ym deploy` prints manual admin steps, and `ym doctor` can't check the active set. On any target, changes made in the YOURLS admin are invisible to ym. A plugin running inside YOURLS could close that gap.

**To decide first:** build our own companion plugin (option A) or integrate with an existing plugin (option B).

#### Option A: our own companion plugin

A minimal plugin that writes YOURLS state into a JSON file in the target's state_dir whenever it changes (hooks like `activated_plugin` / `deactivated_plugin`, plus on version change): active plugins, YOURLS version, DB version. ym would read it over SFTP for adopt/doctor/deploy checks.

Open questions:
- How the plugin learns the state_dir path (a constant in the server-local config written by `ym target config`?).
- Whether it should also apply a desired active set that ym deploys (on the next admin request), which would remove the manual sftp steps.
- How it is shipped: by the template in `overlay/`, or added by `ym build`.

#### Option B: integrate with an existing plugin

Candidate: https://github.com/gioxx/YOURLS-PluginManager (manages plugins from the YOURLS admin); others may be in the awesome-yourls catalog (`ym plugin search`). ym would read and update that plugin's manifest file as well as its own `site.toml` / `yourls.lock.json`, and keep the two in sync on deploy.

Open questions, to settle first by reading that repo:
- Manifest location, format and fields (name, source URL, version/ref, active state?), and whether it lives in the web root (deployed) or the database.
- Direction of sync: is site.toml the source of truth (ym writes the manifest at build/deploy), or can changes made in the admin through PluginManager flow back into site.toml (e.g. read the live manifest on deploy/adopt and report or import differences)?
- How PluginManager installs plugins (download into user/plugins?) and how that interacts with ym deleting files it didn't deploy (rsync `--delete` vs the sftp manifest).
- Whether ym should vendor PluginManager itself as a regular `[[plugins]]` entry.

Likely shape: a `plugin_manager` adapter that maps `[[plugins]]` <-> manifest entries, writes the manifest into `dist/` at build time, reads the deployed manifest during `ym adopt` / before `ym deploy` to detect admin-side changes, and `ym plugin add/remove/enable/disable` keep both in step.

### More target types

`deploy.TARGET_TYPES` (`rsync`, `sftp`) takes further deploy target types. Release artifacts (0.8) cover services that package YOURLS themselves (they pin the published archive), but hosts that ym deploys to directly may still need new types. Add one when a site needs it.

Candidates:
- FTP/FTPS, for shared hosts without SFTP.
- Docker: build an image, or sync into a volume.

A new type needs its `site.toml` keys and validation, a transport, the deploy flow (backup, DB upgrade, plugin activation, or printed manual steps like sftp), and `adopt` / `target login` / `target config` / `doctor` support where it applies.

---

## Still to verify

### Template GitLab CI on real GitLab

Template v0.8.0 (GitLab `check` job, schema checks, a check-updates job that leaves an up-to-date branch alone) and ym 0.9.0 (`ym repo create`, `ym ci setup|status` on GitLab) were verified on gitlab.com with sms-yourls, the artifact-only site split out of redirect-services: `check` on a branch push (it caught the `.gitignore` bug that ym now warns about), `publish` on an rc and a final tag (the CI archive is byte-identical to a local `ym artifact build`), and `ym ci setup` creating the check-updates schedule and a project access token.

Not run yet:
- `check-updates` from the schedule, twice: the first run opens a merge request, the second pushes nothing.
- `check` on a merge request (e.g. the one check-updates opens).
- A CI deploy from a `ci = true` target, and `ym ci setup` for a deploy target on GitLab.

Open questions:
- Should the GitHub workflows get the same branch/pull request check job?

---

## Out of scope

Decided against for now; reopen with a concrete need.

- **Uploading artifacts from ym.** ym builds artifacts but never publishes them; publishing is a CI job in the template. (ym talks to GitHub and GitLab only to set up a site's repository and CI.)
- **Per-consumer layout options** (custom vendor paths, plugin install destinations, preserve lists). Consumers adapt to the site repo layout; anything outside `user/plugins` goes in `overlay/`.
