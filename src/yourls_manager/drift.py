"""Compare the vendored tree with the lock; Git dirty-tree guard."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import SiteConfig
from .lockfile import Lock
from .util import UsageError, console, run
from .vendor import PathFilter, prefix_filter, tree_sha256

PLUGINS_REL = "user/plugins"


def managed_names(cfg: SiteConfig, lock: Lock) -> list[str]:
    names = {p.name for p in cfg.managed_plugins} | set(lock.plugins)
    return sorted(names)


def core_filter(cfg: SiteConfig, lock: Lock) -> PathFilter:
    return prefix_filter(f"{PLUGINS_REL}/{n}" for n in managed_names(cfg, lock))


def core_hash(cfg: SiteConfig, lock: Lock, root: Path | None = None) -> str:
    return tree_sha256(root or cfg.vendor_dir, core_filter(cfg, lock))


def plugin_dir(cfg: SiteConfig, name: str) -> Path:
    return cfg.vendor_dir / "user" / "plugins" / name


@dataclass
class Drift:
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.problems


def compute_drift(cfg: SiteConfig, lock: Lock) -> Drift:
    d = Drift()
    if lock.core is None:
        d.problems.append("core: not locked (run `ym core`)")
    else:
        if lock.core.version != cfg.core.version:
            d.problems.append(
                f"core: site.toml version {cfg.core.version} != locked {lock.core.version}"
                " (run `ym core`)"
            )
        if lock.core.repo != cfg.core.repo:
            d.problems.append("core: site.toml repo differs from lock (run `ym core`)")
        if not (cfg.vendor_dir / "includes").is_dir():
            d.problems.append("core: yourls/ is missing (run `ym core`)")
        elif core_hash(cfg, lock) != lock.core.tree_sha256:
            d.problems.append("core: files in yourls/ differ from the locked tree hash")

    toml_managed = {p.name: p for p in cfg.managed_plugins}
    for name, spec in toml_managed.items():
        entry = lock.plugins.get(name)
        if entry is None:
            d.problems.append(f"plugin {name}: in site.toml but not in lock (run `ym sync`)")
            continue
        if not entry.matches(spec.repo or "", spec.ref, spec.path):
            d.problems.append(f"plugin {name}: repo/ref/path changed in site.toml (run `ym sync`)")
        pdir = plugin_dir(cfg, name)
        if not pdir.is_dir():
            d.problems.append(f"plugin {name}: directory missing (run `ym sync`)")
        elif tree_sha256(pdir) != entry.tree_sha256:
            d.problems.append(f"plugin {name}: files differ from the locked tree hash")
    for name in sorted(set(lock.plugins) - set(toml_managed)):
        d.problems.append(f"plugin {name}: in lock but no longer in site.toml (run `ym sync`)")
    return d


def git_changes(root: Path, *pathspecs: str) -> list[str] | None:
    """Porcelain status lines for ``pathspecs`` in the site repo, or None if not a git repo."""
    probe = run(["git", "rev-parse", "--is-inside-work-tree"], cwd=root, check=False)
    if probe.returncode != 0:
        return None
    proc = run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", *pathspecs],
        cwd=root,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return [ln for ln in proc.stdout.decode(errors="replace").splitlines() if ln.strip()]


def dirty_guard(cfg: SiteConfig, force: bool, pathspecs: list[str] | None = None) -> None:
    """Refuse to rewrite vendored files with uncommitted changes (Git is the backup).

    ``pathspecs`` limits the check to what the command is about to replace; it defaults
    to the whole ``yourls/`` tree.
    """
    if not cfg.vendor_dir.exists():
        return
    specs = pathspecs or [cfg.vendor_dir.name]
    changes = git_changes(cfg.root, *specs)
    if changes is None:
        msg = f"{cfg.root} is not a git repository, so yourls/ has no backup"
        if force:
            console.warn(msg + " (continuing because of --force)")
            return
        raise UsageError(msg + "; commit it to git first or pass --force")
    if changes:
        shown = "\n  ".join(changes[:20]) + ("\n  ..." if len(changes) > 20 else "")
        if force:
            console.warn("yourls/ has uncommitted changes (continuing because of --force)")
            return
        raise UsageError(
            f"yourls/ has uncommitted changes; commit or discard them, or pass --force:\n  {shown}"
        )


def core_pathspecs(cfg: SiteConfig, lock: Lock) -> list[str]:
    """yourls/ minus managed plugin dirs (which `core` carries over untouched)."""
    base = cfg.vendor_dir.name
    return [base, *(f":(exclude){base}/{PLUGINS_REL}/{n}" for n in managed_names(cfg, lock))]


def plugin_pathspecs(cfg: SiteConfig, names: list[str]) -> list[str]:
    return [f"{cfg.vendor_dir.name}/{PLUGINS_REL}/{n}" for n in names]
