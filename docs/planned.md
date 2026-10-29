# yourls-manager: planned work

Work that is not built yet. Each entry stands alone (problem, proposal or options, open questions) so it can move to a GitHub issue as is. When an entry ships, delete it here; [CHANGELOG.md](../CHANGELOG.md) records what was built.

How ym works today: [README](../README.md) and the [command reference](cli.md).

| Item | Status |
|---|---|
| [Server-side plugin state](#server-side-plugin-state) | Undecided: own companion plugin or existing plugin |
| [Deploy history: `ym release list --target`](#deploy-history-ym-release-list---target) | Proposed |
| [Rollback](#rollback) | Idea; manual today |
| [CI follow-ups](#ci-follow-ups) | Proposed, three small items |
| [More target types](#more-target-types) | Probably superseded by release artifacts |
| [Out of scope](#out-of-scope) | Decided against |

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

Three independent items left over from `ym ci setup`:

- **Old CI keys.** `ym ci setup --rotate` leaves the old `ym-ci-<slug>-<env>-<date>` key in the server's `authorized_keys` and says so. Offer to remove keys with that comment other than the new one.
- **Password-only SFTP hosts.** Store `YM_<TARGET>_PASSWORD` as an environment secret for hosts that don't accept keys. Today the CI key replaces the password, so such hosts can't deploy from CI.
- **Environment protection.** Required reviewers and tag policies on the `production` / `staging` environments are configured in GitHub by hand. Decide whether `ym ci setup` should offer them or the site README should just document them.

---

## More target types

`deploy.TARGET_TYPES` (`rsync`, `sftp`) was meant to take further types such as `docker` and `windows-release`. Release artifacts (0.8) now serve services that package YOURLS themselves: they pin the published archive instead of letting ym deploy. Keep this entry only if a concrete site needs ym to deploy to such a host.

---

## Out of scope

Decided against for now; reopen with a concrete need.

- **GitLab backend for `ym repo create` / `ym ci setup`.** GitLab sites set up the project, environments and CI/CD variables by hand (site README, "GitLab CI").
- **Uploading artifacts from ym.** ym builds artifacts but never publishes them; publishing is a CI job in the template, so ym needs no forge APIs or tokens.
- **Per-consumer layout options** (custom vendor paths, plugin install destinations, preserve lists). Consumers adapt to the site repo layout; anything outside `user/plugins` goes in `overlay/`.
