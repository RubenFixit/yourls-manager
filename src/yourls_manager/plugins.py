"""`ym sync`: vendor third-party plugins into yourls/user/plugins/<name>."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import gitsrc
from .config import PluginSpec, SiteConfig, UsageError
from .drift import dirty_guard, plugin_dir, plugin_pathspecs
from .lockfile import PluginLock, load_lock, write_lock
from .util import YMError, console, iso_utc
from .vendor import atomic_replace_dir, copy_tree, rmtree, staging_dir, tree_sha256


@dataclass
class _Plan:
    spec: PluginSpec
    commit: str
    action: str  # "keep" | "install" | "update"


def sync_plugins(
    cfg: SiteConfig,
    names: list[str] | None = None,
    *,
    update: bool = False,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, str]:
    """Vendor managed plugins. Returns {name: action}."""
    if not (cfg.vendor_dir / "includes").is_dir():
        raise UsageError("yourls/ has no core yet; run `ym core` first")
    lock = load_lock(cfg.lock_file)
    managed = {p.name: p for p in cfg.managed_plugins}
    if names:
        unknown = [n for n in names if n not in managed]
        if unknown:
            raise UsageError(
                f"not a plugin with a repo in site.toml: {', '.join(unknown)}"
                f" (managed: {', '.join(managed) or 'none'})"
            )
    selected = [managed[n] for n in (names or list(managed))]

    plans: list[_Plan] = []
    for spec in selected:
        assert spec.repo is not None
        entry = lock.plugins.get(spec.name)
        pdir = plugin_dir(cfg, spec.name)
        if entry and not update and entry.matches(spec.repo, spec.ref, spec.path):
            commit = entry.commit
        else:
            commit = gitsrc.resolve_ref(spec.repo, spec.ref)
        if (
            entry
            and entry.commit == commit
            and entry.matches(spec.repo, spec.ref, spec.path)
            and pdir.is_dir()
            and tree_sha256(pdir) == entry.tree_sha256
        ):
            action = "keep"
        else:
            action = "update" if entry else "install"
        old = entry.commit[:10] if entry else "not locked"
        console.info(f"{spec.name}: {old} -> {commit[:10]} ({action})")
        plans.append(_Plan(spec, commit, action))

    removals: list[str] = []
    if not names:
        removals = sorted(set(lock.plugins) - set(managed))
        for name in removals:
            console.info(f"{name}: removed from site.toml (remove)")

    result = {p.spec.name: p.action for p in plans}
    result.update({n: "remove" for n in removals})
    work = [p for p in plans if p.action != "keep"]
    if dry_run:
        console.info("dry run: nothing changed")
        return result
    if not work and not removals:
        console.info("plugins: everything up to date")
        return result
    dirty_guard(cfg, force, plugin_pathspecs(cfg, [p.spec.name for p in work] + removals))

    # Stage everything first so a fetch failure leaves yourls/ untouched.
    checkout_root = gitsrc.temp_checkout_dir()
    new_entries: dict[str, PluginLock] = {}
    try:
        with staging_dir(cfg.root) as stage_root:
            staged: list[tuple[_Plan, Path]] = []
            for i, plan in enumerate(work):
                spec = plan.spec
                assert spec.repo is not None
                co = gitsrc.fetch_commit(spec.repo, plan.commit, checkout_root / f"p{i}")
                src = (co.path / spec.path).resolve()
                if not src.is_relative_to(co.path.resolve()) or not src.is_dir():
                    raise YMError(f"{spec.name}: path {spec.path!r} not found in {spec.repo}")
                if not (src / "plugin.php").is_file():
                    raise YMError(
                        f"{spec.name}: no plugin.php in {spec.repo} at path {spec.path!r}"
                    )
                dest = stage_root / f"p{i}"
                dest.mkdir()
                copy_tree(src, dest)
                new_entries[spec.name] = PluginLock(
                    repo=spec.repo,
                    ref=spec.ref,
                    path=spec.path,
                    commit=plan.commit,
                    tree_sha256=tree_sha256(dest),
                    resolved_at=iso_utc(),
                )
                staged.append((plan, dest))
            for plan, dest in staged:
                atomic_replace_dir(plugin_dir(cfg, plan.spec.name), dest)
    finally:
        rmtree(checkout_root)

    for name in removals:
        pdir = plugin_dir(cfg, name)
        if pdir.exists():
            rmtree(pdir)
        del lock.plugins[name]
    lock.plugins.update(new_entries)
    lock.plugins = dict(sorted(lock.plugins.items()))
    write_lock(cfg.lock_file, lock)
    console.info(f"plugins: {len(new_entries)} vendored, {len(removals)} removed")
    return result
