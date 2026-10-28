# yourls-manager

`yourls-manager` (alias `ym`) keeps a whole [YOURLS](https://yourls.org) site in its own Git
repository and deploys it with one command. YOURLS core and plugins are vendored at locked
commits, your own files live in an overlay, and secrets stay on the server.

## Quick start

### 1. Prerequisites

On the machine you run `ym` from:

- **git**
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** (it installs Python and
  the tool for you)
- **rsync** and **ssh** to deploy to `rsync` targets. Use Linux, macOS, WSL or CI (GitHub
  Actions). rsync isn't native on Windows, so use WSL there. `sftp` targets need neither:
  ym speaks SFTP itself.

On the server, one of:

- **Shell access (`rsync` target, recommended):** **PHP ≥ 8.1** with the CLI, plus
  `mysqldump` and `gzip`; SSH access with **key authentication** (`ym` runs ssh
  non-interactively and never answers password prompts; `ym ssh-setup prod` or the wizard
  creates and installs a key for you).
- **SFTP only (`sftp` target):** for hosts without shell access. See
  [SFTP targets](#sftp-targets) for what changes (no DB backup; install/upgrade and
  plugin activation happen in the browser).

Either way: **MySQL or MariaDB**, with a database and user for YOURLS, and PHP ≥ 8.1 for the
web server.

### 2. Install the tool

```sh
uv tool install git+https://github.com/RubenFixit/yourls-manager
ym --version

uv tool upgrade yourls-manager      # later, to update
```

### 3A. New site

The easy way is the wizard. It asks a few questions and runs the steps below for you:

```sh
ym setup                            # choose "new site"
```

Or run the steps yourself:

```sh
ym new my-site                      # answer the template questions (deploy type: rsync or sftp); vendors core + plugins, git init
cd my-site
ym ssh-setup prod                   # check key login; create + install a key if needed
ym server-config prod               # write the server-local config (DB, cookie key, admin user)
ym deploy prod --dry-run            # build and show what rsync would change
ym deploy prod                      # deploy for real
```

On the first deploy to an empty database, `ym` uploads the files and then asks you to open
`https://<your site>/admin/install.php` once to create the tables. After that:

```sh
ym plugins prod                     # activate the plugins marked active in site.toml
```

Only SFTP on your host? Answer `sftp` to the deploy type question. `ym ssh-setup`,
`ym server-config` and `ym deploy` then work over SFTP; activate the plugins in
`https://<your site>/admin/plugins.php` instead of `ym plugins` (every deploy lists them). See
[SFTP targets](#sftp-targets).

If you leave the deploy host empty when `ym new` asks, the `prod` target is written to
`site.toml` commented out. Uncomment and fill it in before `ym server-config prod`.

### 3B. Adopt and update an existing YOURLS install

`ym adopt` takes a YOURLS install that is already running and puts it under management
without changing what's served. It finds the core version, copies your changed and extra
files into `overlay/` and your plugins into `overlay/user/plugins/`, and checks that a build
matches the live site.

Wizard:

```sh
ym setup                            # choose "adopt existing install"
```

Or by hand:

```sh
ym new my-site --data include_example_plugins=false
cd my-site
# If you didn't give a deploy host to `ym new`, fill in [deploy.targets.prod] in site.toml now.
ym adopt --from-target prod         # snapshot the live site; vendor its core version; fill overlay/
less adopt-review/REPORT.md         # versions, plugins, files to merge by hand, parity summary
ym server-config prod --from-existing   # copy the live config to the server-local location
ym deploy prod --dry-run            # should show (almost) no changes
ym deploy prod                      # parity deploy: same code the site runs today
git add -A && git commit -m "Adopt existing install"
```

The first deploy should change nothing that matters. Only the template's own files differ:
`.htaccess`, `health.php`, the `user/config.php` shim and the `site-branding` plugin. Merge
anything listed under `adopt-review/` before you deploy. Then update:

```sh
ym check                            # what has updates (exit code 10 = updates available)
ym core --version latest            # move YOURLS core to the newest release
ym sync --update                    # move plugins to their newest commits
git diff --stat                     # review
git add -A && git commit -m "Update YOURLS"
ym deploy prod                      # backs up the DB, deploys, runs the DB upgrade
```

`ym adopt --from-dir PATH` works on a local copy of the web root instead of a target.

For an **sftp** target, ym can't read which plugins are active (there's no PHP CLI), so give
them: `ym adopt --from-target prod --active plugin-a,plugin-b` (on a terminal it asks per
plugin otherwise). After the adopt it records the install's files on the server, so the first
`ym deploy prod` only uploads the template's files. You do the DB upgrade after
`ym core --version latest` in the browser (`ym deploy` prints the link). Back up the database
with your host's tools first.

### DreamHost notes

- **SSH user:** in the panel, give the site's user *SSH* (shell) access. Then run
  `ym ssh-setup prod`: it creates a key if you have none, installs it with `ssh-copy-id`
  (asking for the SSH password once and to trust the host key) and checks that key login works.
- **PHP CLI:** plain `php` is often older than the site's PHP. Set the target's `php` to a
  versioned binary such as `/usr/local/php83/bin/php` (or `php82`, `php84`, ...).
- **Web root:** the domain directory in your home, e.g. `path = "go.example.com"`. The server
  config then goes to `~/yourls-config/<slug>.php` and backups to
  `~/yourls-backups/<slug>/`, both outside the web root.
- **Database host:** use the MySQL hostname you created in the panel (e.g.
  `mysql.example.com`), e.g. `ym server-config prod --db-host mysql.example.com`.

### Push to GitHub and deploy from CI

Create an empty repository on GitHub (no README or license), then:

```sh
git remote add origin git@github.com:<you>/my-site.git
git push -u origin main
```

The site repo's `.github/workflows/deploy.yml` deploys `prod` when you push a `v*` tag, or
when you run it by hand. It runs in the `production` environment. Add these secrets to that
environment (Settings → Environments → production):

| Secret | Value |
|---|---|
| `DEPLOY_SSH_KEY` | private key (ed25519) whose public half is in `~/.ssh/authorized_keys` on the host |
| `DEPLOY_KNOWN_HOSTS` | output of `ssh-keyscan -p <port> <host>` (check the fingerprint) |

`.github/workflows/check-updates.yml` runs `ym check` every week and opens a pull request
when there are updates. For that, enable *Allow GitHub Actions to create and approve pull
requests* under Settings → Actions → General.

## Updating an existing install

**Routine updates** (the weekly `check-updates` pull request does steps 1 and 2 for you):

1. `ym check`: see what's new upstream.
2. `ym core --version latest` and/or `ym sync --update`: vendor the new versions.
3. Review `git diff` (especially `yourls/` and `yourls.lock.json`) and commit.
4. `ym deploy prod --dry-run`, then `ym deploy prod`. Or push a `v*` tag and let CI deploy.

Each deploy dumps the database to `backup_dir` before it changes anything.

**Rollback:** check out the last good tag and deploy it again:

```sh
git checkout v<previous> && ym deploy prod
```

If the database has to go back too, restore the dump taken before the bad deploy from
`backup_dir` on the server (default `<parent of web root>/yourls-backups/<slug>/`):
`gunzip -c <dump>.sql.gz | mysql --defaults-extra-file=<credentials file> <db>`.

**Template updates** (workflows, config shim, `.htaccess`, ...): with a clean Git tree, run

```sh
ym template-update                          # pull the latest yourls-site-template changes
ym template-update --manager-version v0.3.0 # also bump the yourls-manager pin in pyproject.toml
```

then review the diff and commit. Files you own (`site.toml`, `tests/redirects.toml`, branding
assets) are never touched.

**Tool updates:** `uv tool upgrade yourls-manager` updates the `ym` on your PATH. The site's
CI uses the version pinned in the site's `pyproject.toml`. Bump that pin with
`ym template-update --manager-version vX.Y.Z`.

## Releases and deploy policy

**Tags.** A release is `v<YOURLS version>-<revision>`: `v1.10.6-1`, `v1.10.6-2`, ... The
revision counts releases of this site and restarts at 1 when YOURLS changes (`v1.11.0-1`).
Release candidates add `-rc.<n>`: `v1.10.6-3-rc.1`. `ym release` works out the next tag from
the locked YOURLS version and the existing tags, so several releases a day are fine.

```sh
ym release --dry-run          # show the next tag and its notes
ym release --rc --push        # v1.10.6-3-rc.1: CI deploys it to staging (if defined)
ym release --promote --push   # v1.10.6-3 on the rc's commit: CI deploys it to prod
ym release --push             # or tag the next final release straight away
ym release --list             # releases, dates, and where each is deployed
```

The annotated tag carries release notes: YOURLS and plugin changes since the previous final
release (from `yourls.lock.json`), your `-m` text and the commit subjects.

**Deploy policy.** Each target has `accept = "any"` (default) or `"release"`:

| | `accept = "any"` | `accept = "release"` |
|---|---|---|
| Untagged or rc commit | deploys | refused (`--force` overrides) |
| Final release tag at HEAD | deploys | deploys |
| Uncommitted changes | refused unless `--allow-dirty` | refused (`--force` overrides) |

The template sets `accept = "release"` on prod. Overrides are recorded in the server's
`release.json` (`forced`, `git_dirty`, `tag`), and ym warns when HEAD isn't pushed yet.

**Deployment markers.** Deployments aren't tagged (they aren't versions). After each
deploy of a clean tree ym moves the local branch `deployed/<target>` to the deployed commit,
so `git log --decorate` shows what's live where. Set `[release] push_markers = true` in
site.toml to push it as well.

**CI.** The template's deploy workflow deploys final tags to `prod` and `-rc.<n>` tags to
`staging` (skipped when site.toml has no staging target). The policy is enforced by
`ym deploy` itself, so manual workflow runs can't bypass it.

## Setup wizard

`ym setup` is an interactive front end to the commands above. It needs a terminal (without
one it exits with code 2). It:

1. asks whether you're creating a **new site** or **adopting** an existing install, and
   where to put the site repo;
2. runs `ym new` (the template asks its questions; when adopting it passes
   `--data include_example_plugins=false`);
3. checks SSH key login to the `prod` target and, if it fails, offers `ym ssh-setup prod`;
4. when adopting, asks for the source (the `prod` target if one is configured, or a local
   path) and runs `ym adopt`;
5. offers `ym server-config prod` (with `--from-existing` when adopting and the server-local
   config doesn't exist yet);
6. offers `ym deploy prod --dry-run`;
7. prints a summary of what it did and what's left to do.

Before each step it prints the equivalent command, so you can repeat or script the flow
without the wizard.

**Resuming.** Progress is saved in the site repo (`.git/ym-setup.json`, never committed) and
the last unfinished site is remembered in `~/.local/state/yourls-manager/setup.json`
(`$XDG_STATE_HOME` is honored). If a step fails or you stop early, fix the problem and run
`ym setup` again from anywhere: it offers to resume and skips what's already done (the repo,
a finished adoption; an existing server config is detected on the server). `ym setup DIR`
resumes a specific site repo, including one you created with `ym new` by hand.

## Commands

Global options: `--site-dir PATH` (default: search upward from the current directory for
`site.toml`), `-v/--verbose`, `--version`.

Exit codes: `0` ok, `1` error, `2` usage or configuration error, `10` updates available
(`check` only).

### Onboarding

| Command | What it does |
|---|---|
| `ym setup [DIR]` | Interactive wizard for a new site or an adoption; resumable (see [Setup wizard](#setup-wizard)). |
| `ym ssh-setup TARGET [--key PATH]` | Check non-interactive SSH key login to the target. If it fails: create a key with `ssh-keygen` (default `~/.ssh/id_ed25519`) when there is none, install it with `ssh-copy-id` (asks for the SSH password once and to trust the host key; falls back to plain `ssh` where `ssh-copy-id` is missing), then check again. Explains the fix when a passphrase-protected key isn't in an agent (`ssh-add`) or a non-default key needs an `~/.ssh/config` entry. For `sftp` targets the check is an SFTP login (SFTP-only accounts have no shell), unknown host keys are shown with their SHA256 fingerprint for you to trust, and the key is appended to `.ssh/authorized_keys` over SFTP after one password login (`.ssh` 700, file 600). |
| `ym new [DEST] [--template SRC] [--vcs-ref REF] [--data K=V ...] [--defaults] [--no-vendor] [--no-git]` | Create a site repo from the template. The default template is `gh:RubenFixit/yourls-site-template`; override it with `--template` or the `YM_TEMPLATE` environment variable. Copier asks its questions unless you pass `--defaults`. Then `ym new` runs `ym core` + `ym sync` (skip with `--no-vendor`), runs `git init -b main` and makes the first commit (skip with `--no-git`; skipped with a warning if no Git identity is set), and tries `uv lock`. Refuses a non-empty `DEST`. |
| `ym template-update [--vcs-ref REF] [--manager-version vX.Y.Z] [--defaults]` | Run `copier update` in the site repo. Refuses if the Git tree is dirty. `--manager-version` also bumps the yourls-manager pin. Then it tries `uv lock` and prints `git diff --stat` for you to review and commit. |
| `ym server-config TARGET [--force] [--from-existing] [--db-host H] [--db-name N] [--db-user U] [--db-prefix P] [--admin-user U]` | Create the server-local config at the target's `config_file` from `server/config.example.php`. It fills in the DB settings, `YOURLS_SITE`, a fresh `YOURLS_COOKIEKEY` and one admin user. Passwords come from `YM_DB_PASSWORD` / `YM_ADMIN_PASSWORD` or a prompt, never from the command line. The admin password is hashed by the target's PHP (for `sftp` targets: locally with bcrypt, cost 10, in the same `phpass:` format). The file is written with mode 0600. It won't overwrite an existing file unless you pass `--force`, which first keeps a copy as `<file>.bak-<UTC timestamp>`. `--from-existing` copies the live install's `user/config.php` (or `includes/config.php`) to `config_file` instead; use it when adopting. |
| `ym adopt (--from-target TARGET \| --from-dir PATH) [--ignore GLOB ...] [--active A,B,...] [--dry-run]` | Bring an existing YOURLS install under management, inside a repo made by `ym new`. `--active` names the plugins that are active on the install (needed for `sftp` targets; also skips reading them with the PHP helper elsewhere). See [below](#ym-adopt). |

### Day-to-day

| Command | What it does |
|---|---|
| `ym status` | Show the locked core version, each plugin (locked commit, active flag), and any drift: tree-hash mismatches, plugins in `site.toml` but not in the lock (or the reverse), and uncommitted Git changes under `yourls/`. |
| `ym check [--json]` | Ask upstream (with `git ls-remote`) for the newest stable core tag (`-rc`/`-beta` tags are skipped) and each plugin ref's current commit. Exits **10** if anything can be updated. |
| `ym core [--version V\|latest] [--dry-run] [--force]` | Fetch core at a tag and replace `yourls/`, keeping managed plugin dirs and applying `exclude`. Updates the lock and `[core].version`. |
| `ym sync [--plugin NAME ...] [--update] [--dry-run] [--force]` | Vendor plugins into `yourls/user/plugins/<name>`. By default it uses the locked commit while the plugin's repo/ref/path are unchanged. `--update` re-resolves refs. A managed plugin that was removed from `site.toml` has its directory deleted. |
| `ym build [--out DIR] [--allow-drift]` | Clean `dist/`, then copy `yourls/` and `overlay/` into it. Refuses to build if `yourls/` doesn't match the lock. `-v` lists each overlay override. |
| `ym deploy TARGET [--dry-run] [--no-build] [--skip-backup] [--skip-verify] [--verify-remote] [--allow-dirty] [--force]` | `rsync` targets: build → preflight → upload helper → DB backup → rsync → DB upgrade → plugins → record → verify. `--dry-run` shows rsync's itemized changes and stops. `sftp` targets: see [SFTP targets](#sftp-targets); `--verify-remote` (sftp only) also checks every deployed file on the server. Refuses uncommitted changes unless `--allow-dirty`; targets with `accept = "release"` only take a final release tag (`--force` overrides, recorded). Afterwards moves the `deployed/<target>` branch. See [Releases and deploy policy](#releases-and-deploy-policy). |
| `ym verify TARGET` / `ym verify --url URL` | Check `GET /health.php` (expects `{"ok": true}`), then each case in `tests/redirects.toml`. Redirects are not followed. |
| `ym plugins TARGET` | Make the active plugin set on the target exactly the set marked `active` in `site.toml`. Not available for `sftp` targets (it prints what to activate in the admin UI). |
| `ym release [--rc \| --promote \| --list] [--push] [--dry-run] [-m TEXT]` | Tag the next release `v<yourls>-<rev>` (`--rc`: `-rc.<n>`; `--promote`: the newest rc's final) on a clean, drift-free tree, with release notes from the lock diff. `--list` shows releases and which targets have them deployed. |
| `ym update [--commit] [--core-only \| --plugins-only]` | Routine update in one step: core to the latest stable release, plugins to their newest commits, build, then a summary of the changes (`--commit` commits them with that summary as the message). |
| `ym plugin list` / `ym plugin enable NAME` / `ym plugin disable NAME` | List plugins with source, ref and locked commit; switch a plugin's `active` flag in `site.toml` (comments kept). The next deploy applies it. |

`ym core` and `ym sync` won't overwrite vendored files that have uncommitted Git changes,
because Git is your backup. Commit first or pass `--force`.

`ym new`, `ym setup` and `ym template-update` run Copier as `uvx copier`, or as `copier` if
uv isn't on your PATH.

### `ym adopt`

1. **Snapshot.** `--from-target` rsyncs the target's web root into a temp dir (read-only on
   the server) and asks the PHP helper for the YOURLS version and the active plugins. For
   `sftp` targets it downloads the web root over SFTP (ignore patterns are applied while
   walking, symlinks are skipped); the active plugins come from `--active`, or a per-plugin
   question on a terminal. `--from-dir` uses a local directory.
2. **Version.** Reads `includes/version.php`, finds the matching upstream tag, writes it to
   `[core].version` and vendors it.
3. **Diff against pristine core.** Files that match upstream are left alone. Modified or
   added files are copied into `overlay/` at the same path. If the template already owns
   that path (`.htaccess`, `health.php`, `user/config.php`, `site-branding`), the file goes to
   `adopt-review/<path>` for you to merge by hand instead. Upstream files missing from the
   site are only reported. The config files are skipped, and so are these defaults plus
   anything you add with `--ignore`: `.well-known/**`, `**/error_log`, `**/*.log`,
   `**/.DS_Store`, `**/Thumbs.db`, `.git/**`.
4. **Plugins.** An unmodified plugin that ships with core gets a `[[plugins]]` entry without
   `repo`. Any other plugin is copied as-is into `overlay/user/plugins/<name>/` and gets an
   entry without `repo`, with its `Plugin URI` in a comment so you can switch to a managed
   `repo =` later. The `active` flags come from the live site. With `--from-dir` and no local
   PHP and config, every plugin is written `active = true` with a `# TODO verify` comment.
   Check those flags, because a deploy makes the active set exactly what `site.toml` says.
5. **Parity check.** Builds `dist/` and compares it to the snapshot. It prints how many
   files a deploy would add, change or delete (`-v` lists them).
6. **Report.** Writes `adopt-review/REPORT.md`. `adopt-review/` is never deployed and is
   safe to commit or delete.
7. **Manifest** (`sftp` targets only). Uploads the list of the install's files and their
   hashes to `<state_dir>/manifest.json`, so the first deploy only sends real differences and
   may delete files that belonged to the old install (never ignored or `protect`ed ones).

`--dry-run` prints the plan and writes nothing.

### Redirect tests

```toml
# tests/redirects.toml
[[case]]
path = "/gh"
status = 301
location = "https://github.com/RubenFixit"

[[case]]
path = "/docs/anything"
status = [301, 302]
location = "https://docs.example.com/*"   # trailing * = prefix match
```

### What a deploy does

For `type = "rsync"` (for `type = "sftp"` see [SFTP targets](#sftp-targets)), commands run as `ssh -p PORT user@host -- <cmd>`. If you omit `host`,
the same commands run on the local machine. Each step:

1. **build** (skipped with `--no-build`)
2. **preflight**: ssh works with key auth (`BatchMode`), PHP CLI is ≥ 8.1, rsync is present,
   and `config_file` exists. If the config file is missing, `ym` points you to
   `ym server-config` and exits with code 2.
3. **upload helper**: `ym-helper.php` goes to `~/.yourls-manager/ym-helper-<version>.php`,
   outside the web root.
4. **backup**: `mysqldump --single-transaction | gzip` into `backup_dir`, keeping the newest
   `backup_keep` dumps. This is skipped automatically when the database has no tables yet.
5. **rsync**: `rsync -az --delete --delay-updates` of `dist/` into the web root. Paths listed in
   `protect` are never deleted.
6. **db upgrade**: runs YOURLS' own upgrade routine, and does nothing if no upgrade is needed.
7. **plugins**: activates and deactivates plugins until the active set is exactly the
   `site.toml` set.
8. **record**: writes `~/.yourls-manager/<slug>/release.json` (git commit, dirty flag, tool
   version, lock sha256, core version, time), outside the web root.
9. **verify** (skipped with `--skip-verify`).

## SFTP targets

For hosts that give you SFTP but no shell (no rsync, no PHP CLI), use `type = "sftp"`:

```toml
[deploy.targets.prod]
type   = "sftp"
host   = "ssh.example.com"
user   = "ruben"
port   = 22
path   = "go.example.com"                 # web root, relative to the SFTP login dir (or absolute)
# config_file = "yourls-config/<slug>.php"  # default: <parent of path>/yourls-config/<slug>.php
# state_dir   = ".yourls-manager/<slug>"    # default: <parent of path>/.yourls-manager/<slug>
protect = [".well-known/"]
url     = "https://go.example.com"
```

`php`, `backup_dir` and `backup_keep` don't apply (ym warns if you set them). `ym new` writes
this block when you answer `sftp` to the deploy type question.

**Requirements.** SFTP access to the web root and to its parent directory (for the
server-local config and `state_dir`, both outside the web root). Nothing else on the server:
ym uses [paramiko](https://www.paramiko.org/) and needs no shell, so SFTP-only and chrooted
accounts work.

**Login.** `~/.ssh/config` is honored for the host (`HostName`, `Port`, `User`,
`IdentityFile`) unless the target sets them. ym tries ssh-agent, then your key files, then a
password: from `YM_<TARGET>_PASSWORD` (target name upper-cased, other characters as `_`, e.g.
`YM_PROD_PASSWORD`) or, on a terminal, one prompt per command. ym never stores passwords.
`ym ssh-setup prod` installs a key over SFTP so you don't need the password afterwards.

**Host keys** are checked against `~/.ssh/known_hosts`. On a terminal, ym shows an unknown
host's SHA256 fingerprint and asks before adding it; without a terminal (CI) it fails and
tells you to add it (`ssh-keyscan -p PORT HOST >> ~/.ssh/known_hosts`, after checking the
fingerprint). A changed host key always fails.

**What a deploy does.**

1. **build** (skipped with `--no-build`), connect, **preflight**: the web root is reachable
   (created if missing) and `config_file` exists (else: run `ym server-config prod`).
2. **plan**: ym compares `dist/` with `<state_dir>/manifest.json`, the list of files (with
   sha256 and size) it deployed last time. New and changed files are uploaded; files in the
   manifest that are no longer in `dist/` are deleted, unless `protect`ed.
3. `--dry-run` prints the plan (counts; file lists with `-v`) and stops.
4. **upload**: each file is written as `.<name>.ym-tmp` and renamed into place, so visitors
   never see half a file. `.htaccess`, `user/config.php` and `yourls-loader.php` go last.
   Then the deletes, then directories that became empty. `manifest.json` and `release.json`
   (0600) are written to `state_dir`.
5. **YOURLS state over HTTP**: ym requests `<url>/` without following redirects (it steps
   through same-site redirects one by one). A redirect to `admin/install.php` or
   `admin/upgrade.php` means you have to open that page. ym prints the link.
6. **plugins**: ym prints the plugins `site.toml` marks active and the link to
   `admin/plugins.php`. It can't check or change activation without PHP on the server.
7. **verify** (skipped with `--skip-verify`). While an install or upgrade is pending it is
   skipped too, and the deploy still succeeds: finish the step in the browser, then run
   `ym verify TARGET`.

**How deletes differ from rsync.** rsync `--delete` makes the web root an exact copy of
`dist/` and removes anything else that isn't `protect`ed. An sftp deploy only deletes files
that ym itself deployed (or that `ym adopt` recorded). Files you uploaded by hand, logs and
caches stay. The first deploy without a manifest uploads everything and deletes nothing (ym
says so). The manifest can't see edits made on the server: `ym deploy prod --verify-remote`
checks the size of every deployed file and uploads the ones someone changed again. A file
that was changed on the server and then removed from the build is left in place with a
warning.

**Manual admin steps** (ym prints them after every deploy, `ym setup` lists them too):

- new site: open `<url>/admin/install.php` once to create the tables;
- after a core update: open `<url>/admin/upgrade.php` and run the upgrade (back up the
  database first);
- plugins: in `<url>/admin/plugins.php`, make exactly the `active = true` plugins from
  `site.toml` active.

**No database backup.** Without a shell there's no `mysqldump`, so sftp deploys don't back
up the database (`--skip-backup` is accepted and ignored). Use your host's backup tool,
especially before a core update.

**server-config** writes the config over SFTP (mode 0600; `--force` keeps a 0600
`.bak-<UTC>` copy). The admin password is hashed locally with bcrypt (cost 10) in the format
YOURLS expects (`phpass:` + `$2y$` hash with `$` written as `!`). `--from-existing` downloads
the live `user/config.php` and uploads it to `config_file`.

**CI.** The site's deploy workflow works for sftp targets too: `DEPLOY_SSH_KEY` must be
authorized for the SFTP account, and `DEPLOY_KNOWN_HOSTS` must contain the host key.

## Concepts

### Site repo layout

```
my-site/
├── site.toml           # the site definition (you edit this)
├── yourls.lock.json    # written by ym: exact core + plugin commits and tree hashes
├── yourls/             # vendored upstream code, owned by ym; never edit by hand
├── overlay/            # your files, copied over yourls/ at build time (overlay wins)
├── server/config.example.php   # starting point for the server-local config
├── tests/redirects.toml
├── adopt-review/       # only after `ym adopt`; never deployed
├── pyproject.toml      # pins the yourls-manager version CI uses
└── dist/               # build output (gitignored)
```

A minimal `site.toml` (the full schema is in [design §2](docs/design.md#2-sitetoml-schema)):

```toml
[site]
name = "Example Go Links"
slug = "go-example-com"
url  = "https://go.example.com"

[core]
version = "1.10.6"
exclude = [".github", ".git*", "tests", "*.md"]

[[plugins]]
name = "keep-query-string"
repo = "https://github.com/rinogo/yourls-keep-query-string"
ref  = "HEAD"

[[plugins]]
name = "hyphens-in-urls"      # no repo: ships with core (or comes from overlay/)

[deploy.targets.prod]
type = "rsync"
host = "example.dreamhost.com"
user = "ruben"
path = "go.example.com"       # relative paths are relative to the remote $HOME
php  = "/usr/local/php83/bin/php"
protect = [".well-known/"]
```

`core.exclude` patterns are anchored at the repo root: `*` never matches across `/`,
`tests` excludes only the top-level `tests/` directory, and `*.md` matches only root-level
Markdown files (use `**/*.md` to match at every level). `LICENSE` and `readme.html` are kept
unless you list them by exact name.

### Who owns what

| Where | Owner | Changed by |
|---|---|---|
| `yourls/`, `yourls.lock.json` | `ym` | `ym core`, `ym sync`, `ym adopt` only. Never edit by hand; `ym status` shows drift. |
| `overlay/` | you (some files come from the template) | You, `ym adopt`, and `ym template-update` for the template's files. To patch a vendored plugin, put the changed files in `overlay/user/plugins/<name>/`. |
| `site.toml`, `tests/redirects.toml` | you | You (`ym core` and `ym adopt` update `[core].version` and append plugins, keeping your comments). |
| Server-local config | the server | `ym server-config`. It's never in Git. |

The server-local config holds the DB credentials, cookie key and users. By default it lives
at `<parent of web root>/yourls-config/<slug>.php`. The `overlay/user/config.php` shim
loads it. When `ym` runs the helper, it sets `YOURLS_CONFIG_FILE` to the target's
`config_file`, and the shim reads that variable first. If you use a non-default
`config_file`, tell the web server too, for example with
`SetEnv YOURLS_CONFIG_FILE /home/you/...` in `overlay/.htaccess`.

### Passwords are stored pre-hashed

YOURLS normally rewrites plaintext passwords in its config file into hashes. Here,
YOURLS's config file is the shim, and the shim must never be modified on the server. So the
shim sets `YOURLS_NO_HASH_PASSWORD`, and a plaintext password would stay plaintext. Instead,
`ym server-config` writes the YOURLS hash format directly (`phpass:` + `password_hash()` with
`$` written as `!`), using the target's own PHP. To add users by hand, see the comment in
`server/config.example.php`.

### `ym` vs `uv run ym`

The globally installed `ym` runs every command, including `ym new` and `ym setup`, which
have to work before a site repo exists. Each site repo also pins a yourls-manager version in
its `pyproject.toml`. `uv run ym ...` inside the repo uses that pinned version, and so does
CI.

## Development

```sh
uv sync
uv run ruff check && uv run ruff format --check
uv run pytest
```

Tests that need `rsync` or `php` are skipped if those tools are missing. To run the helper
against a real, installed YOURLS (`mysql` marker), set `YM_TEST_YOURLS_ROOT` to its web root
and `YM_TEST_YOURLS_CONFIG` to its config file.

The SFTP tests (`sftp` marker) start a real OpenSSH server on 127.0.0.1 with an ordinary and
a chrooted SFTP-only test account. They need root and `/usr/sbin/sshd` (they create the
`ymsftp` / `ymsftponly` users) and are skipped otherwise. Set `YM_TEST_YOURLS_SRC` to a
YOURLS checkout to check password hashes with YOURLS' own `yourls_check_password_hash()`.

The full design is in [`docs/design.md`](docs/design.md). The site template lives in
[`yourls-site-template`](https://github.com/RubenFixit/yourls-site-template).

## License

MIT © 2026 Ruben Kackstaetter
