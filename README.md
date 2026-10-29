# yourls-manager

`yourls-manager` (alias `ym`) keeps a whole [YOURLS](https://yourls.org) site in its own Git repository and deploys it with one command. YOURLS core and plugins are vendored at locked commits, your own files live in an overlay, and secrets stay on the server.

## Quick start

### 1. Prerequisites

On the machine you run `ym` from:

- **git**
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** (it installs Python and the tool for you)
- **rsync** and **ssh** to deploy to `rsync` targets. Use Linux, macOS, WSL or CI (GitHub Actions). rsync isn't native on Windows, so use WSL there. `sftp` targets need neither: ym speaks SFTP itself.

On the server, one of:

- **Shell access (`rsync` target, recommended):** **PHP ≥ 8.1** with the CLI, plus `mysqldump` and `gzip`; SSH access with **key authentication** (`ym` runs ssh non-interactively and never answers password prompts; `ym target login prod` or the wizard creates and installs a key for you).
- **SFTP only (`sftp` target):** for hosts without shell access. See [SFTP targets](docs/deploying.md#sftp-targets) for what changes (no DB backup; install/upgrade and plugin activation happen in the browser).

Either way: **MySQL or MariaDB**, with a database and user for YOURLS, and PHP ≥ 8.1 for the web server.

### 2. Install the tool

```sh
uv tool install git+https://github.com/RubenFixit/yourls-manager
ym --version

uv tool upgrade yourls-manager      # later, to update
```

### 3A. New site

The easy way is the wizard. It asks a few questions and runs the steps below for you:

```sh
ym init                             # choose "new site"
```

Or run the steps yourself:

```sh
ym new my-site                      # answer the template questions (deploy type: rsync or sftp); vendors core + plugins, git init
cd my-site
ym target login prod                # check key login; create + install a key if needed
ym target config prod               # write the server-local config (DB, cookie key, admin user)
ym deploy prod --dry-run            # build and show what rsync would change
ym deploy prod                      # deploy for real
```

On the first deploy to an empty database, `ym` uploads the files and then asks you to open `https://<your site>/admin/install.php` once to create the tables. After that:

```sh
ym plugin apply prod                # activate the plugins marked active in site.toml
```

Only SFTP on your host? Answer `sftp` to the deploy type question. `ym target login`, `ym target config` and `ym deploy` then work over SFTP; activate the plugins in `https://<your site>/admin/plugins.php` instead of `ym plugin apply` (every deploy lists them). See [SFTP targets](docs/deploying.md#sftp-targets).

If you leave the deploy host empty when `ym new` asks, the `prod` target is written to `site.toml` commented out. Add it with `ym target add prod` (it asks for host, user and web root) before `ym target config prod`. `ym target list` shows the targets.

### 3B. Adopt and update an existing YOURLS install

`ym adopt` takes a YOURLS install that is already running and puts it under management without changing what's served. It finds the core version, copies your changed and extra files into `overlay/` and your plugins into `overlay/user/plugins/`, and checks that a build matches the live site.

Wizard:

```sh
ym init                             # choose "adopt existing install"
```

Or by hand:

```sh
ym new my-site --data include_example_plugins=false
cd my-site
ym target add prod                  # only if you didn't give a deploy host to `ym new`
ym adopt --from-target prod         # snapshot the live site; vendor its core version; fill overlay/
less adopt-review/REPORT.md         # versions, plugins, files to merge by hand, parity summary
ym target config prod --from-existing       # copy the live config to the server-local location
ym deploy prod --dry-run            # should show (almost) no changes
ym deploy prod                      # parity deploy: same code the site runs today
git add -A && git commit -m "Adopt existing install"
```

The first deploy should change nothing that matters. Only the template's own files differ: `.htaccess`, `health.php`, the `user/config.php` shim and the `site-branding` plugin. Merge anything listed under `adopt-review/` before you deploy. Then update:

```sh
ym update --check                   # what has updates (exit code 10 = updates available)
ym update --commit                  # core to the newest release, plugins to newest commits
ym deploy prod                      # backs up the DB, deploys, runs the DB upgrade
```

`ym adopt --from-dir PATH` works on a local copy of the web root instead of a target. For an sftp target, pass the active plugins with `--active a,b,...`; [Onboarding](docs/onboarding.md#ym-adopt) has the details.

### Store the site on GitHub

Git is the record of the site, so a remote is optional: deploys run from your machine either way. To keep it on GitHub, with the [GitHub CLI](https://cli.github.com) (`gh`) installed:

```sh
ym repo create                      # private, named after the site directory; --name, --owner, --public
```

It creates the repository, adds it as `origin` (or creates the one `origin` already names) and pushes the current branch. Tags are not pushed; `git push --tags` when you want them. Nothing about deploying changes. By hand: create an empty repository on GitHub (no README or license), then `git remote add origin git@github.com:<you>/my-site.git && git push -u origin main`.

To deploy from GitHub Actions or GitLab CI instead of your machine, see [Deploying from CI](docs/deploying.md#deploying-from-ci).

## Documentation

- [The site repo](docs/site-repo.md): layout, who owns what, the full `site.toml` reference, redirect tests, the server-local config
- [Onboarding](docs/onboarding.md): the `ym init` wizard, `ym adopt` step by step, DreamHost notes
- [Deploying](docs/deploying.md): rsync and SFTP targets, deploying from GitHub Actions or GitLab CI, rollback
- [Updates and releases](docs/releases.md): routine updates, release tags, the deploy policy, release artifacts
- [Command reference](docs/cli.md): every command and option (also `ym COMMAND --help`)
- [Planned work](docs/planned.md) and the [changelog](CHANGELOG.md)

## Development

```sh
uv sync
uv run ruff check && uv run ruff format --check
uv run pytest
```

Tests that need `rsync` or `php` are skipped if those tools are missing. To run the helper against a real, installed YOURLS (`mysql` marker), set `YM_TEST_YOURLS_ROOT` to its web root and `YM_TEST_YOURLS_CONFIG` to its config file.

The SFTP tests (`sftp` marker) start a real OpenSSH server on 127.0.0.1 with an ordinary and a chrooted SFTP-only test account. They need root and `/usr/sbin/sshd` (they create the `ymsftp` / `ymsftponly` users) and are skipped otherwise. Set `YM_TEST_YOURLS_SRC` to a YOURLS checkout to check password hashes with YOURLS' own `yourls_check_password_hash()`.

The site template lives in [`yourls-site-template`](https://github.com/RubenFixit/yourls-site-template).

## License

MIT © 2026 Ruben Kackstaetter
