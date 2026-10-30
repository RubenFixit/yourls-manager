# Deploying

A site deploys to the targets in `[deploy.targets.*]` of [`site.toml`](site-repo.md#sitetoml): `type = "rsync"` for servers with a shell, `type = "sftp"` for hosts that only give you SFTP. `ym target add` writes a target, `ym target login` makes sure ym can log in without prompts, and `ym target config` writes the server-local config. Which commits a target accepts is in [Releases](releases.md#deploy-policy).

## rsync targets

Commands run as `ssh -p PORT user@host -- <cmd>` in BatchMode, so the target needs key login (`ym target login` creates and installs a key). If you omit `host`, the same commands run on the local machine. A deploy:

1. **build** (skipped with `--no-build`).
2. **preflight**: ssh works, rsync is present locally and on the server, PHP CLI is ≥ 8.1, and `config_file` exists. If the config file is missing, ym points you to `ym target config` and exits with code 2. `--dry-run` then shows rsync's itemized changes and stops.
3. **upload helper**: `ym-helper.php` goes to `~/.yourls-manager/ym-helper-<version>.php`, outside the web root.
4. **backup**: `mysqldump --single-transaction | gzip` into `backup_dir`, keeping the newest `backup_keep` dumps. Skipped when the database has no tables yet (first deploy), or with `--skip-backup`.
5. **rsync**: `rsync -az --delete --delay-updates` of `dist/` into the web root. Paths listed in `protect` are never deleted.
6. **install check**: on an empty database ym asks you to open `<url>/admin/install.php` once, then run `ym plugin apply TARGET`, and skips the next two steps.
7. **db upgrade**: runs YOURLS' own upgrade routine; nothing happens when no upgrade is needed.
8. **plugins**: activates and deactivates plugins until the active set is exactly the `site.toml` set.
9. **record**: writes `~/.yourls-manager/<slug>/release.json` (git commit, dirty flag, tag, forced, tool version, lock sha256, core version, time, backup, active plugins), outside the web root.
10. **verify** (skipped with `--skip-verify`).

Afterwards ym moves the local branch `deployed/<target>` to the deployed commit, even when verify failed.

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

`php`, `backup_dir` and `backup_keep` don't apply (ym warns if you set them). `ym new` writes this block when you answer `sftp` to the deploy type question.

**Requirements.** SFTP access to the web root and to its parent directory (for the server-local config and `state_dir`, both outside the web root). Nothing else on the server: ym uses [paramiko](https://www.paramiko.org/) and needs no shell, so SFTP-only and chrooted accounts work.

**Login.** `~/.ssh/config` is honored for the host (`HostName`, `Port`, `User`, `IdentityFile`) unless the target sets them. ym tries ssh-agent, then your key files, then a password: from `YM_<TARGET>_PASSWORD` (target name upper-cased, other characters as `_`, e.g. `YM_PROD_PASSWORD`) or, on a terminal, one prompt per command. ym never stores passwords. `ym target login prod` installs a key over SFTP (after one password login; `.ssh` 700, `authorized_keys` 600) so you don't need the password afterwards.

**Host keys** are checked against `~/.ssh/known_hosts`. On a terminal, ym shows an unknown host's SHA256 fingerprint and asks before adding it; without a terminal (CI) it fails and tells you to add it (`ssh-keyscan -p PORT HOST >> ~/.ssh/known_hosts`, after checking the fingerprint). A changed host key always fails.

**What a deploy does.**

1. **build** (skipped with `--no-build`), connect, **preflight**: the web root is reachable (created if missing) and `config_file` exists (else: run `ym target config prod`).
2. **plan**: ym compares `dist/` with `<state_dir>/manifest.json`, the list of files (with sha256 and size) it deployed last time. New and changed files are uploaded; files in the manifest that are no longer in `dist/` are deleted, unless `protect`ed.
3. `--dry-run` prints the plan (counts; file lists with `-v`) and stops.
4. **upload**: each file is written as `.<name>.ym-tmp` and renamed into place, so visitors never see half a file. `.htaccess`, `user/config.php` and `yourls-loader.php` go last. Then the deletes, then directories that became empty. `manifest.json` and `release.json` (0600) are written to `state_dir`.
5. **YOURLS state over HTTP**: ym requests `<url>/` without following redirects (it steps through same-site redirects one by one). A redirect to `admin/install.php` or `admin/upgrade.php` means you have to open that page. ym prints the link.
6. **plugins**: ym prints the plugins `site.toml` marks active and the link to `admin/plugins.php`. It can't check or change activation without PHP on the server, so `ym plugin apply` prints the same steps.
7. **verify** (skipped with `--skip-verify`). While an install or upgrade is pending it is skipped too, and the deploy still succeeds: finish the step in the browser, then run `ym verify TARGET`.

**How deletes differ from rsync.** rsync `--delete` makes the web root an exact copy of `dist/` and removes anything else that isn't `protect`ed. An sftp deploy only deletes files that ym itself deployed (or that `ym adopt` recorded). Files you uploaded by hand, logs and caches stay. The first deploy without a manifest uploads everything and deletes nothing (ym says so). The manifest can't see edits made on the server: `ym deploy prod --verify-remote` checks the size of every deployed file and uploads the ones someone changed again. A file that was changed on the server and then removed from the build is left in place with a warning.

**Manual admin steps** (ym prints them after every deploy, `ym init` lists them too):

- new site: open `<url>/admin/install.php` once to create the tables;
- after a core update: open `<url>/admin/upgrade.php` and run the upgrade (back up the database first);
- plugins: in `<url>/admin/plugins.php`, make exactly the `active = true` plugins from `site.toml` active.

**No database backup.** Without a shell there's no `mysqldump`, so sftp deploys don't back up the database (`--skip-backup` is accepted and ignored). Use your host's backup tool, especially before a core update.

**`ym target config`** writes the config over SFTP (mode 0600; `--force` keeps a 0600 `.bak-<UTC>` copy). The admin password is hashed locally with bcrypt (cost 10) in the format YOURLS expects (`phpass:` + `$2y$` hash with `$` written as `!`). `--from-existing` downloads the live `user/config.php` and uploads it to `config_file`.

## Deploying from CI

Deploys run from your machine by default; CI is opt-in per target with `ci = true`. The template's `ci_provider` answer picks the CI files: `github` (default), `gitlab` or `none`. Choose it in `ym new` / `ym init`, or switch later with `ym template update --data ci_provider=gitlab`; the update removes the old provider's files.

### GitHub Actions

The site repo's `.github/workflows/deploy.yml` deploys targets that have `ci = true`: final release tags go to `prod`, `-rc.<n>` tags to `staging` (skipped when site.toml has no staging target), or run it by hand for any target. Targets without `ci = true` are skipped, so a site you deploy by hand never gets failed workflow runs. `prod` runs in the `production` environment, every other target in `staging`; each environment needs two secrets. The deploy policy is enforced by `ym deploy` itself, so manual workflow runs can't bypass it. Let ym set it up:

```sh
ym ci setup                         # prod and staging; offers `gh auth login` and `ym repo create`
git commit -m "Deploy from GitHub Actions" site.toml && git push
ym ci status                        # environments and secrets of the ci = true targets
```

`ym ci setup` creates the environment if needed, generates a dedicated CI key (ed25519, no passphrase), installs its public half on the server over your working login (ssh, or SFTP for `sftp` targets), checks that it logs in, and stores the private half only in `DEPLOY_SSH_KEY`. The host keys come from your `~/.ssh/known_hosts` (else `ssh-keyscan`, with the fingerprints to confirm). Last, it sets `ci = true` on the target; commit that, since the workflow reads it from the commit it deploys. Run it again any time: it keeps the key and refreshes the host keys; `--rotate` replaces the key and offers to remove the environment's previous CI keys (`ym-ci-<slug>-<environment>-*`) from the server's `authorized_keys`; `--dry-run` shows what it would do.

Or by hand: set `ci = true` on the target, and under Settings → Environments → production:

| Secret | Value |
|---|---|
| `DEPLOY_SSH_KEY` | private key (ed25519) whose public half is in `~/.ssh/authorized_keys` on the host |
| `DEPLOY_KNOWN_HOSTS` | output of `ssh-keyscan -p <port> <host>` (check the fingerprint) |

For sftp targets the key must be authorized for the SFTP account.

`.github/workflows/check-updates.yml` runs `ym update --check` every week and opens a pull request when there are updates. For that, enable *Allow GitHub Actions to create and approve pull requests* under Settings → Actions → General.

### GitLab CI

`.gitlab-ci.yml` has the same jobs: deploy on release tags for `ci = true` targets (`production` for prod, `staging` for others), a scheduled `check-updates` job that opens a merge request, the artifact publish job, and a `check` job (`ym doctor --local`, `ym build`) on merge requests and branch pushes. With the [GitLab CLI](https://gitlab.com/gitlab-org/cli) (`glab`) logged in, ym sets the project up the same way as on GitHub:

```sh
ym repo create                      # the project from origin / repo_url, or --host --owner --name
ym ci setup                         # prod and staging, plus check-updates; offers `glab auth login`
git commit -m "Deploy from GitLab CI" site.toml && git push
ym ci status                        # environments, variables, protected tags, check-updates
```

On GitLab, `ym ci setup` stores `DEPLOY_SSH_KEY` and `DEPLOY_KNOWN_HOSTS` as File-type, protected CI/CD variables scoped to the environment, and protects the `v*` tags (protected variables only reach pipelines of protected refs, and only maintainers can then create release tags). It also sets up `check-updates`, with or without deploy targets (so artifact-only sites get it too): the pipeline schedule `ym check-updates` (Mondays 05:23 UTC; change it under Build → Pipeline schedules) and the masked variable `YM_UPDATE_TOKEN` the job pushes with. ym creates a project access token for it (Developer, `write_repository`, valid for a year: run `ym ci setup --rotate` before it expires); where GitLab refuses (project access tokens may need a paid tier on gitlab.com), it asks you to paste a personal or group access token with `write_repository`. `--skip-updates` leaves check-updates out. ym never asks for or stores GitLab credentials: glab owns the login (ym offers `glab auth login` and reads glab's token, from `GITLAB_TOKEN`, glab's config or its keyring), and ym makes the API calls with [python-gitlab](https://python-gitlab.readthedocs.io/) using that token. Self-managed GitLab works: the host comes from `origin` (or `repo_url`, or `--host`), and glab's `api_host` / `api_protocol` settings for it apply.

By hand, the site README's "GitLab CI" section lists the same settings.

## Rollback

Check out the last good tag and deploy it again:

```sh
git checkout v<previous> && ym deploy prod
```

If the database has to go back too, restore the dump taken before the bad deploy from `backup_dir` on the server (default `<parent of web root>/yourls-backups/<slug>/`): `gunzip -c <dump>.sql.gz | mysql --defaults-extra-file=<credentials file> <db>`. sftp targets have no dumps; use your host's backups.
