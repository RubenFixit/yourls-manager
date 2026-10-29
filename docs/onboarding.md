# Onboarding: the wizard and `ym adopt`

The [README](../README.md#quick-start) shows the short path for a new site and for adopting an existing install. This page has the details.

## Setup wizard

`ym init` is an interactive front end to `ym new`, `ym target …`, `ym adopt` and `ym deploy`. It needs a terminal (without one it exits with code 2). It:

1. asks whether you're creating a **new site** or **adopting** an existing install, and where to put the site repo;
2. runs `ym new` (the template asks its questions; when adopting it passes `--data include_example_plugins=false`);
3. offers `ym target add prod` when site.toml has no `prod` target, then checks the login to it and, if that fails, offers `ym target login prod`;
4. when adopting, asks for the source (the `prod` target if one is configured, or a local path) and runs `ym adopt`;
5. offers `ym target config prod` (with `--from-existing` when adopting and the server-local config doesn't exist yet);
6. offers `ym deploy prod --dry-run`, and lists the manual admin steps for sftp targets;
7. prints a summary of what it did and what's left to do.

Before each step it prints the equivalent command, so you can repeat or script the flow without the wizard.

**Resuming.** Progress is saved in the site repo (`.git/ym-init.json`, never committed) and the last unfinished site is remembered in `~/.local/state/yourls-manager/init.json` (`$XDG_STATE_HOME` is honored). If a step fails or you stop early, fix the problem and run `ym init` again from anywhere: it offers to resume and skips what's already done (the repo, a finished adoption; an existing server config is detected on the server). `ym init DIR` resumes a specific site repo, including one you created with `ym new` by hand.

## `ym adopt`

`ym adopt` brings an install that is already running under management without changing what's served. Run it inside a site repo made by `ym new`.

1. **Snapshot.** `--from-target` rsyncs the target's web root into a temp dir (read-only on the server) and asks the PHP helper for the YOURLS version and the active plugins. For `sftp` targets it downloads the web root over SFTP (ignore patterns are applied while walking, symlinks are skipped); the active plugins come from `--active`, or a per-plugin question on a terminal. `--from-dir` uses a local directory.
2. **Version.** Reads `includes/version.php`, finds the matching upstream tag, writes it to `[core].version` and vendors it.
3. **Diff against pristine core.** Files that match upstream are left alone. Modified or added files are copied into `overlay/` at the same path. If the template already owns that path (`.htaccess`, `health.php`, `user/config.php`, `site-branding`), the file goes to `adopt-review/<path>` for you to merge by hand instead. Upstream files missing from the site are only reported. The config files are skipped, and so are these defaults plus anything you add with `--ignore`: `.well-known/**`, `**/error_log`, `**/*.log`, `**/.DS_Store`, `**/Thumbs.db`, `.git/**`.
4. **Plugins.** An unmodified plugin that ships with core gets a `[[plugins]]` entry without `repo`. Any other plugin is copied as-is into `overlay/user/plugins/<name>/` and gets an entry without `repo`, with its `Plugin URI` in a comment so you can switch to a managed `repo =` later. The `active` flags come from the live site. With `--from-dir` and no local PHP and config, every plugin is written `active = true` with a `# TODO verify` comment. Check those flags, because a deploy makes the active set exactly what `site.toml` says.
5. **Parity check.** Builds `dist/` and compares it to the snapshot. It prints how many files a deploy would add, change or delete (`-v` lists them).
6. **Report.** Writes `adopt-review/REPORT.md`. `adopt-review/` is never deployed and is safe to commit or delete.
7. **Manifest** (`sftp` targets only). Uploads the list of the install's files and their hashes to `<state_dir>/manifest.json`, so the first deploy only sends real differences and may delete files that belonged to the old install (never ignored or `protect`ed ones).

`--dry-run` prints the plan and writes nothing.

For an **sftp** target, ym can't read which plugins are active (there's no PHP CLI), so give them: `ym adopt --from-target prod --active plugin-a,plugin-b`. You do the DB upgrade after `ym core latest` in the browser (`ym deploy` prints the link). Back up the database with your host's tools first.

## DreamHost notes

- **SSH user:** in the panel, give the site's user *SSH* (shell) access. Then run `ym target login prod`: it creates a key if you have none, installs it with `ssh-copy-id` (asking for the SSH password once and to trust the host key) and checks that key login works.
- **PHP CLI:** plain `php` is often older than the site's PHP. Set the target's `php` to a versioned binary such as `/usr/local/php83/bin/php` (or `php82`, `php84`, ...).
- **Web root:** the domain directory in your home, e.g. `path = "go.example.com"`. The server config then goes to `~/yourls-config/<slug>.php` and backups to `~/yourls-backups/<slug>/`, both outside the web root.
- **Database host:** use the MySQL hostname you created in the panel (e.g. `mysql.example.com`), e.g. `ym target config prod --db-host mysql.example.com`.
