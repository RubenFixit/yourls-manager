"""`ym core`: vendor YOURLS core at a tag."""

from __future__ import annotations

import re
from pathlib import Path

from . import gitsrc
from .config import SiteConfig, set_core_version
from .drift import (
    PLUGINS_REL,
    core_hash,
    core_pathspecs,
    dirty_guard,
    managed_names,
    plugin_dir,
)
from .lockfile import CoreLock, load_lock, write_lock
from .util import YMError, console, iso_utc
from .vendor import atomic_replace_dir, copy_tree, make_exclude, rmtree, staging_dir

_VERSION_RE = re.compile(r"""define\s*\(\s*['"]YOURLS_VERSION['"]\s*,\s*['"]([^'"]+)['"]""")


def read_yourls_version(root: Path) -> str | None:
    vf = root / "includes" / "version.php"
    if not vf.is_file():
        return None
    m = _VERSION_RE.search(vf.read_text(encoding="utf-8", errors="replace"))
    return m[1] if m else None


def update_core(
    cfg: SiteConfig,
    version: str | None = None,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> CoreLock | None:
    lock = load_lock(cfg.lock_file)
    wanted = version or cfg.core.version
    tag, commit = gitsrc.resolve_core_tag(cfg.core.repo, wanted)
    current = lock.core
    console.info(
        f"core: {current.version + ' @ ' + current.commit[:10] if current else 'not locked'}"
        f" -> {tag} @ {commit[:10]}"
    )
    if dry_run:
        console.info("dry run: nothing changed")
        return None
    dirty_guard(cfg, force, core_pathspecs(cfg, lock))

    managed = managed_names(cfg, lock)
    checkout_dir = gitsrc.temp_checkout_dir()
    try:
        src = gitsrc.fetch_commit(cfg.core.repo, commit, checkout_dir / "src")
        upstream_version = read_yourls_version(src.path)
        if upstream_version is None:
            raise YMError(f"{tag}: includes/version.php with YOURLS_VERSION not found")
        if upstream_version != tag.removeprefix("v"):
            console.warn(f"tag {tag} declares YOURLS_VERSION {upstream_version}")
        with staging_dir(cfg.root) as stage_parent:
            stage = stage_parent / "yourls"
            stage.mkdir()
            n = copy_tree(src.path, stage, make_exclude(cfg.core.exclude))
            console.detail(f"copied {n} core files")
            # Carry managed plugin dirs over untouched (they are owned by `sync`).
            for name in managed:
                existing = plugin_dir(cfg, name)
                dest = stage / PLUGINS_REL / name
                if dest.exists():
                    rmtree(dest)
                if existing.is_dir():
                    copy_tree(existing, dest)
            tree = core_hash(cfg, lock, stage)
            atomic_replace_dir(cfg.vendor_dir, stage)
    finally:
        rmtree(checkout_dir)

    lock.core = CoreLock(
        repo=cfg.core.repo,
        version=tag,
        commit=commit,
        tree_sha256=tree,
        resolved_at=iso_utc(),
    )
    write_lock(cfg.lock_file, lock)
    if set_core_version(cfg.site_file, tag):
        console.info(f"site.toml: [core].version = {tag!r}")
    console.info(f"core: vendored YOURLS {upstream_version} ({tag} @ {commit[:10]})")
    return lock.core
