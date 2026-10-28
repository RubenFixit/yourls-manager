"""`ym update`: routine update of core and plugins in one step (design §11.2)."""

from __future__ import annotations

import copy

from .config import LOCK_FILE, SITE_FILE, VENDOR_DIR, SiteConfig, load_site
from .lockfile import load_lock
from .release import lock_diff
from .util import console, run


def update(
    cfg: SiteConfig, *, commit: bool = False, core: bool = True, plugins: bool = True
) -> list[str]:
    """Update core to the latest stable tag and plugins to their newest commits.

    Returns the change summary (empty when everything was up to date)."""
    from . import gitsrc
    from .build import build_site
    from .core import update_core
    from .plugins import sync_plugins

    old = copy.deepcopy(load_lock(cfg.lock_file))
    if core:
        tag, _ = gitsrc.resolve_core_tag(cfg.core.repo, "latest")
        if old.core is None or old.core.version != tag:
            console.step(f"core -> {tag}")
            update_core(cfg, "latest")
        else:
            console.info(f"core: up to date ({tag})")
    if plugins and cfg.managed_plugins:
        console.step("plugins")
        sync_plugins(cfg, None, update=True)
    changes = lock_diff(old, load_lock(cfg.lock_file))
    if not changes:
        console.info("everything is up to date")
        return []
    console.step("build")
    build_site(load_site(cfg.root))  # site.toml [core].version may have changed
    console.info("Changes:")
    for c in changes:
        console.info(f"  - {c}")
    if commit:
        message = "Update " + "; ".join(changes)
        run(["git", "add", "--", SITE_FILE, LOCK_FILE, VENDOR_DIR], cwd=cfg.root)
        run(["git", "commit", "-q", "-m", message], cwd=cfg.root)
        console.info(f"committed: {message}")
    else:
        console.info("review with `git diff --stat`, then commit (or rerun with --commit)")
    return changes
