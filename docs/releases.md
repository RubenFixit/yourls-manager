# Updates and releases

## Updating

**Routine updates** (the weekly `check-updates` job does steps 1 and 2 for you and opens a pull or merge request):

1. `ym update --check`: see what's new upstream (exit code 10 = updates available).
2. `ym update`: vendor the new versions and build (`--core-only` / `--plugins-only`; the lower-level `ym core latest` and `ym plugin sync --update` do one part each).
3. Review `git diff` (especially `yourls/` and `yourls.lock.json`) and commit, or use `ym update --commit`.
4. Release and deploy: `ym release`, then `ym deploy prod --dry-run` and `ym deploy prod`, or push the tag and let CI deploy.

rsync deploys dump the database to `backup_dir` before they change anything. See [Rollback](deploying.md#rollback).

**Template updates** (workflows, config shim, `.htaccess`, ...): with a clean Git tree, run

```sh
ym template update                           # pull the latest yourls-site-template changes
ym template update --manager-version v0.9.1  # also bump the yourls-manager pin in pyproject.toml
ym template update --data ci_provider=gitlab # change an earlier template answer
```

then review the diff and commit. Files you own (`site.toml`, `tests/redirects.toml`, branding assets) are never touched.

**Tool updates:** `uv tool upgrade yourls-manager` updates the `ym` on your PATH. The site's CI uses the version pinned in the site's `pyproject.toml`; bump that pin with `ym template update --manager-version vX.Y.Z`.

## Releases

A release is `v<YOURLS version>-<revision>`: `v1.10.6-1`, `v1.10.6-2`, ... The revision counts releases of this site and restarts at 1 when YOURLS changes (`v1.11.0-1`). Release candidates add `-rc.<n>`: `v1.10.6-3-rc.1`. `ym release` works out the next tag from the locked YOURLS version and the existing tags, so several releases a day are fine. Other tags are ignored.

```sh
ym release --dry-run                # show the next tag and its notes
ym release --rc --push              # v1.10.6-3-rc.1: CI deploys it to staging (if defined)
ym release promote --push           # v1.10.6-3 on the rc's commit: CI deploys it to prod
ym release --push                   # or tag the next final release straight away
ym release list                     # releases, dates, and where each is deployed
```

`ym release` needs a clean tree without lock drift and builds before tagging. The annotated tag carries release notes: YOURLS and plugin changes since the previous final release (from `yourls.lock.json`), your `-m` text and the commit subjects. `--push` pushes the tag only, and refuses before tagging when there is no `origin`.

## Deploy policy

Each target has `accept = "any"` (default) or `"release"`:

| | `accept = "any"` | `accept = "release"` |
|---|---|---|
| Untagged or rc commit | deploys | refused (`--force` overrides) |
| Final release tag at HEAD | deploys | deploys |
| Uncommitted changes | refused unless `--allow-dirty` | refused (`--force` overrides) |

The template and `ym target add` set `accept = "release"` on prod. Overrides are recorded in the server's `release.json` (`forced`, `git_dirty`, `tag`), and ym warns when HEAD isn't pushed yet (only when the repo has a remote).

**Deployment markers.** Deployments aren't tagged (they aren't versions). After each deploy of a clean tree ym moves the local branch `deployed/<target>` to the deployed commit, so `git log --decorate` shows what's live where, and `ym release list` uses it. Set `[release] push_markers = true` in site.toml to push it as well.

## Release artifacts

Some sites aren't deployed by ym: YOURLS is one part of a larger service with its own packaging, and that service pins a built copy of the site instead. For those, keep the site in its own repo as usual (it needs no deploy targets) and publish each release as an archive:

```toml
[artifact]
enabled = true                 # tells doctor (and the template's CI) that releases are published
formats = ["tar.gz", "zip"]    # default ["tar.gz"]
name    = "{slug}-{version}"   # {slug}, {version} (the tag without "v"), {tag}
```

```sh
ym release                         # tag v1.10.6-2 as usual
ym artifact build                  # HEAD must carry a release tag; the tree must be clean
ym artifact build --ref v1.10.6-1  # rebuild an older release (same bytes as before)
```

This writes to `dist-artifacts/` (or `--out DIR`):

| File | Contents |
|---|---|
| `rfixme-1.10.6-2.tar.gz`, `.zip` | The built web root (`yourls/` + `overlay/`), without a top-level directory. Entries are sorted and carry fixed times (the commit's), owners (0) and modes (0644 files, 0755 directories), so rebuilding a tag gives byte-identical archives (with the same ym and zlib). |
| `rfixme-1.10.6-2.tar.gz.sha256` | `sha256sum` format: `<hex>  <file name>`. |
| `rfixme-1.10.6-2.json` | Site, version, tag, rc, commit and commit time, locked core and plugin commits, the ym version, and each archive's sha256. |

ym doesn't upload anything; the site's CI publishes the files. Sites from the template get this with the `release_artifact` answer (`ym new ... --data release_artifact=true`, or `ym template update --data release_artifact=true` plus the `[artifact]` table by hand): on every release tag, a publish job attaches the files to a GitHub release (rc tags as pre-releases), or with `ci_provider = gitlab` uploads them to GitLab's Generic Package Registry and creates a release. The site README explains how a consumer pins, verifies and extracts them. `ym doctor` treats a site with `enabled = true` and no deploy targets as artifact-only and warns when no CI file runs `ym artifact build`. `--allow-untagged` builds an untagged commit as version `0.0.0-dev+<sha>` for local tests.
