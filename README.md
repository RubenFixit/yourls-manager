# yourls-manager

`yourls-manager` (alias `ym`) keeps a whole [YOURLS](https://yourls.org) site in its own
Git repository and deploys it with one command.

- **Vendored and locked.** YOURLS core and third-party plugins are copied into `yourls/` at
  exact commits. `yourls.lock.json` records each commit and a tree hash, so every build can be
  reproduced and any hand edit shows up as drift.
- **Site files stay separate.** Your own files (`.htaccess`, health check, config shim,
  branding plugin, patches to vendored plugins) live in `overlay/`. At build time they are
  copied over `yourls/`.
- **One-command deploy.** `ym deploy prod` builds the site, backs up the database, rsyncs the
  files, runs any YOURLS database upgrade, sets the exact active plugin list, records the
  release and checks your redirects.
- **No secrets in Git.** The real YOURLS config (DB credentials, cookie key, users) lives on
  the server, outside the web root.

At runtime the tool needs only the Python 3.11+ standard library. On the server it needs
PHP ≥ 8.1 with the CLI, `mysqldump` and `gzip`.

Site repos are normally generated from the
[`yourls-site-template`](https://github.com/RubenFixit/yourls-site-template) Copier template.
The full design is in [`docs/design.md`](docs/design.md).

## Install

As a global tool, with [uv](https://docs.astral.sh/uv/):

```sh
uv tool install git+https://github.com/RubenFixit/yourls-manager
ym --version
```

As a pinned dependency of a site repo (recommended; this is what the template does):

```toml
# my-site/pyproject.toml
[project]
name = "my-site"
version = "0"
requires-python = ">=3.11"
dependencies = ["yourls-manager"]

[tool.uv.sources]
yourls-manager = { git = "https://github.com/RubenFixit/yourls-manager", tag = "v0.1.0" }
```

Then use `uv run ym ...` inside the site repo. To update the tool, change the tag and run
`uv lock`.

## Site layout

```
my-site/
├── site.toml           # the site definition (you edit this)
├── yourls.lock.json    # written by ym: exact core + plugin commits and tree hashes
├── yourls/             # vendored upstream code, owned by ym; never edit by hand
├── overlay/            # your files, copied over yourls/ at build time (overlay wins)
├── server/config.example.php
├── tests/redirects.toml
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

To patch a vendored plugin, put the changed files in `overlay/user/plugins/<name>/...`. Don't
edit `yourls/`.

## Commands

Global options: `--site-dir PATH` (default: search upward from the current directory for
`site.toml`), `-v/--verbose`, `--version`.

| Command | What it does |
|---|---|
| `ym status` | Show the locked core version, each plugin (locked commit, active flag), and any drift: tree-hash mismatches, plugins in `site.toml` but not in the lock (or the reverse), and uncommitted Git changes under `yourls/`. |
| `ym check [--json]` | Ask upstream (with `git ls-remote`) for the newest stable core tag (`-rc`/`-beta` tags are skipped) and each plugin ref's current commit. Exits **10** if anything can be updated. |
| `ym core [--version V\|latest] [--dry-run] [--force]` | Fetch core at a tag and replace `yourls/`, keeping managed plugin dirs and applying `exclude`. Updates the lock and `[core].version`. |
| `ym sync [--plugin NAME ...] [--update] [--dry-run] [--force]` | Vendor plugins into `yourls/user/plugins/<name>`. By default it uses the locked commit while the plugin's repo/ref/path are unchanged. `--update` re-resolves refs. A managed plugin that was removed from `site.toml` has its directory deleted. |
| `ym build [--out DIR] [--allow-drift]` | Clean `dist/`, then copy `yourls/` and `overlay/` into it. Refuses to build if `yourls/` doesn't match the lock. `-v` lists each overlay override. |
| `ym deploy TARGET [--dry-run] [--no-build] [--skip-backup] [--skip-verify]` | Build → preflight → upload helper → DB backup → rsync → DB upgrade → plugins → record → verify. `--dry-run` shows rsync's itemized changes and stops. |
| `ym verify TARGET` / `ym verify --url URL` | Check `GET /health.php` (expects `{"ok": true}`), then each case in `tests/redirects.toml`. Redirects are not followed. |
| `ym plugins TARGET` | Make the active plugin set on the target exactly the set marked `active` in `site.toml`. |

`ym core` and `ym sync` won't overwrite vendored files that have uncommitted Git changes,
because Git is your backup. Commit first or pass `--force`.

Exit codes: `0` ok, `1` error, `2` usage or configuration error, `10` updates available
(`check` only).

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

For `type = "rsync"`, commands run as `ssh -p PORT user@host -- <cmd>`. If you omit `host`,
the same commands run on the local machine. Each step:

1. **build** (skipped with `--no-build`)
2. **preflight**: ssh works with key auth (`BatchMode`), PHP CLI is ≥ 8.1, rsync is present,
   and `config_file` exists. If the config file is missing, `ym` explains how to create it
   from `server/config.example.php` and exits with code 2.
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

On the very first deploy, YOURLS isn't installed in the database yet. `ym` uploads the files,
then tells you to open `https://your.site/admin/install.php`. After installing, run
`ym plugins TARGET`.

**Rollback** in v0.1 is manual: check out the previous Git tag and run `ym deploy` again. If
the database needs to go back too, restore a dump from `backup_dir`
(`gunzip -c dump.sql.gz | mysql ...`).

When `ym` runs the helper, it sets `YOURLS_CONFIG_FILE` to the target's `config_file`. The
template's `user/config.php` shim reads that variable first. Otherwise the shim falls back to
`<parent of web root>/yourls-config/<slug>.php`. If you use a non-default `config_file`, also
tell the web server about it, for example `SetEnv YOURLS_CONFIG_FILE /home/you/...` in
`overlay/.htaccess`.

## DreamHost notes

- **SSH:** in the panel, set the site's user to *SSH* (shell) access. Then add your public
  key with `ssh-copy-id user@server.dreamhost.com`. `ym` uses `BatchMode=yes`, so password
  prompts aren't possible. Add the host to `~/.ssh/known_hosts` once, by connecting
  interactively or with `ssh-keyscan`.
- **PHP CLI:** plain `php` on DreamHost is often an older version than the site's PHP. Point
  `php` at a versioned binary such as `/usr/local/php83/bin/php` (or `php82`, `php84`, ...).
- **Paths:** web roots live in your home directory as `~/<domain>`, so use
  `path = "go.example.com"`. The defaults then put the server config at
  `~/yourls-config/<slug>.php` and the backups at `~/yourls-backups/<slug>/`, both outside
  the web root.
- **Database host:** DreamHost MySQL hostnames such as `mysql.example.com` work as-is in
  `YOURLS_DB_HOST`. The `host:port` and `host:/path/to/socket` forms work too.
- **Where to run `ym deploy` from:** Linux, macOS, WSL, or GitHub Actions. rsync isn't native
  on Windows. Use WSL there, or let CI deploy.

### Deploying from GitHub Actions

```yaml
- uses: actions/checkout@v4
- uses: astral-sh/setup-uv@v6
- name: SSH key
  run: |
    install -m 700 -d ~/.ssh
    echo "${{ secrets.DEPLOY_SSH_KEY }}" > ~/.ssh/id_ed25519 && chmod 600 ~/.ssh/id_ed25519
    echo "${{ secrets.DEPLOY_KNOWN_HOSTS }}" > ~/.ssh/known_hosts
- run: uv run ym deploy prod
```

## Development

```sh
uv sync
uv run ruff check && uv run ruff format --check
uv run pytest
```

Tests that need `rsync` or `php` are skipped if those tools are missing. To run the helper
against a real, installed YOURLS (`mysql` marker), set `YM_TEST_YOURLS_ROOT` to its web root
and `YM_TEST_YOURLS_CONFIG` to its config file.

## License

MIT © 2026 Ruben Kackstaetter
