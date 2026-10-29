# The site repo

A site repo holds one YOURLS site: its definition, the vendored upstream code, your own files, and tests. `ym new` creates it from [yourls-site-template](https://github.com/RubenFixit/yourls-site-template).

## Layout

```
my-site/
├── site.toml           # the site definition (you edit this)
├── yourls.lock.json    # written by ym: exact core + plugin commits and tree hashes
├── yourls/             # vendored upstream code, owned by ym; never edit by hand
├── overlay/            # your files, copied over yourls/ at build time (overlay wins)
│   ├── .htaccess, health.php
│   └── user/config.php, user/plugins/site-branding/
├── server/config.example.php   # starting point for the server-local config
├── tests/redirects.toml        # redirect expectations checked by `ym verify`
├── adopt-review/       # only after `ym adopt`; never deployed
├── pyproject.toml      # pins the yourls-manager version CI uses
├── dist/               # build output (gitignored)
└── dist-artifacts/     # `ym artifact build` output (gitignored)
```

Plus the CI files of the template's `ci_provider` answer: `.github/workflows/` (`deploy.yml`, `check-updates.yml`, and `publish.yml` with release artifacts) and `dependabot.yml`, or `.gitlab-ci.yml`, or none.

## Who owns what

| Where | Owner | Changed by |
|---|---|---|
| `yourls/`, `yourls.lock.json` | `ym` | `ym core`, `ym plugin sync` and the commands built on them (`update`, `plugin add/remove`, `new`, `adopt`). Never edit by hand; `ym doctor --local` shows drift. |
| `overlay/` | you (some files come from the template) | You, `ym adopt`, and `ym template update` for the template's files. To patch a vendored plugin, put the changed files in `overlay/user/plugins/<name>/`. |
| `site.toml`, `tests/redirects.toml` | you | You. `ym core`, `ym adopt`, `ym plugin …`, `ym target add` and `ym ci setup` edit `site.toml` as text, keeping your comments, and restore it if the result doesn't validate. |
| Server-local config | the server | `ym target config`. It's never in Git. |

`ym core` and `ym plugin sync` won't overwrite vendored files that have uncommitted Git changes, because Git is your backup. Commit first or pass `--force`.

## `site.toml`

Unknown keys warn; invalid values fail with an error that names the key.

```toml
[site]
name = "Example Go Links"       # display name
slug = "go-example-com"         # [a-z0-9-]+; names the server config, state and backups
url  = "https://go.example.com" # public base URL, no trailing slash

[core]
repo    = "https://github.com/YOURLS/YOURLS.git"   # default
version = "1.10.6"                                 # exact tag; `ym core latest` resolves the newest
exclude = [".github", ".git*", "tests", "*.md"]    # optional; globs relative to the repo root

[[plugins]]
name   = "keep-query-string"                       # directory under user/plugins
repo   = "https://github.com/rinogo/yourls-keep-query-string"
ref    = "HEAD"                                    # branch, tag, HEAD or a 40-character sha; default HEAD
path   = "."                                       # subdirectory holding plugin.php; default "."
active = true                                      # default true

[[plugins]]
name = "hyphens-in-urls"                           # no repo: ships with core, or comes from overlay/

[build]
out = "dist"                                       # default

[deploy.targets.prod]
type        = "rsync"                              # "rsync" (default) or "sftp"
host        = "example.dreamhost.com"              # omit for a target on this machine (rsync only)
user        = "ruben"                              # default: ~/.ssh/config, else your local user
port        = 22                                   # default 22
path        = "go.example.com"                     # web root (required)
php         = "/usr/local/php83/bin/php"           # rsync only; default "php"
config_file = "yourls-config/go-example-com.php"   # default <parent of path>/yourls-config/<slug>.php
backup_dir  = "yourls-backups/go-example-com"      # rsync only; default <parent of path>/yourls-backups/<slug>
backup_keep = 10                                   # rsync only; database dumps kept; default 10
state_dir   = ".yourls-manager/go-example-com"     # sftp only; default <parent of path>/.yourls-manager/<slug>
protect     = [".well-known/"]                     # paths deploys never delete
url         = "https://go.example.com"             # default [site].url; used by verify and doctor
accept      = "release"                            # "any" (default) or "release"; see releases.md
ci          = true                                 # deployed by the site's CI; needs a host; default false

[release]
push_markers = false                               # also push the deployed/<target> branches

[artifact]
enabled = true                                     # default false; see releases.md
formats = ["tar.gz"]                               # "tar.gz" and/or "zip"; default ["tar.gz"]
name    = "{slug}-{version}"                       # placeholders {slug}, {version} (tag without "v"), {tag}
```

Details:

- **`core.exclude`** patterns are anchored at the repo root: `*` never matches across `/`, `tests` excludes only the top-level `tests/` directory, and `*.md` matches only root-level Markdown files (use `**/*.md` for every level). `LICENSE` and `readme.html` are kept unless you list them by exact name.
- **Target names** match `[A-Za-z0-9][A-Za-z0-9._-]*`. `ym target add` writes a target table for you; `ym target list` shows them.
- **Paths** on a target are limited to `[A-Za-z0-9._/@+~-]`. Relative paths are relative to the remote home directory (rsync) or the SFTP login directory (sftp). `php`, `backup_dir` and `backup_keep` on an sftp target warn and are ignored.
- **`protect`** works like rsync's protect filter: a pattern with a `/` at the start or inside is anchored at the web root, otherwise it matches at any depth; a directory covers everything below it.
- **`[artifact].name`** must render to `[A-Za-z0-9._-]+` and not start with `.` or `-`.

## `tests/redirects.toml`

`ym verify` (and every deploy) checks `GET /health.php` for `{"ok": true}`, then each case. Redirects are not followed.

```toml
[[case]]
path = "/gh"                                 # requested path, relative to the target url
status = 301                                 # or a list: [301, 302]
location = "https://github.com/RubenFixit"   # optional; exact match

[[case]]
path = "/docs/anything"
status = [301, 302]
location = "https://docs.example.com/*"      # trailing * = prefix match
```

## The server-local config

The server-local config holds the DB credentials, cookie key and users, so secrets never enter Git. By default it lives at `<parent of web root>/yourls-config/<slug>.php`, outside the web root; `ym target config` writes it. The `overlay/user/config.php` shim loads it:

1. the file named by the `YOURLS_CONFIG_FILE` environment variable, if set and readable (ym sets it to the target's `config_file` when it runs the PHP helper);
2. else `<parent of web root>/yourls-config/<slug>.php`;
3. else HTTP 503 "server-local configuration missing".

If you use a non-default `config_file`, tell the web server too, for example with `SetEnv YOURLS_CONFIG_FILE /home/you/...` in `overlay/.htaccess`.

`overlay/health.php` answers `{"ok": true}` when the database answers `SELECT 1`, else 503 `{"ok": false}`. It discloses no versions.

### Passwords are stored pre-hashed

YOURLS normally rewrites plaintext passwords in its config file into hashes. Here, YOURLS's config file is the shim, and the shim must never be modified on the server. So the shim sets `YOURLS_NO_HASH_PASSWORD`, and a plaintext password would stay plaintext. Instead, `ym target config` writes the YOURLS hash format directly (`phpass:` + `password_hash()` with `$` written as `!`), using the target's own PHP (sftp targets: locally with bcrypt, cost 10). To add users by hand, see the comment in `server/config.example.php`.

## `ym` vs `uv run ym`

The globally installed `ym` runs every command, including `ym new` and `ym init`, which have to work before a site repo exists. Each site repo also pins a yourls-manager version in its `pyproject.toml`. `uv run ym ...` inside the repo uses that pinned version, and so does CI. `ym new`, `ym init` and `ym template update` run Copier as `uvx copier`, or as `copier` if uv isn't on your PATH.
