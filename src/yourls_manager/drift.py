"""Compare the vendored tree with the lock; Git dirty-tree guard."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import SiteConfig
from .lockfile import Lock
from .util import UsageError, YMError, console, run
from .vendor import PathFilter, iter_files, prefix_filter, tree_sha256

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
            d.problems.append(f"plugin {name}: in site.toml but not in lock (run `ym plugin sync`)")
            continue
        if not entry.matches(spec.repo or "", spec.ref, spec.path):
            d.problems.append(
                f"plugin {name}: repo/ref/path changed in site.toml (run `ym plugin sync`)"
            )
        pdir = plugin_dir(cfg, name)
        if not pdir.is_dir():
            d.problems.append(f"plugin {name}: directory missing (run `ym plugin sync`)")
        elif tree_sha256(pdir) != entry.tree_sha256:
            d.problems.append(f"plugin {name}: files differ from the locked tree hash")
    for name in sorted(set(lock.plugins) - set(toml_managed)):
        d.problems.append(
            f"plugin {name}: in lock but no longer in site.toml (run `ym plugin sync`)"
        )
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


@dataclass
class Ignored:
    """A vendored file that Git ignores and doesn't track; ``rule`` is ``source:line``."""

    path: str
    rule: str
    pattern: str


def ignored_files(cfg: SiteConfig, dirs: list[Path], skip: PathFilter | None = None
                  ) -> list[Ignored]:  # fmt: skip
    """Files under ``dirs`` (``skip`` applies to paths relative to each dir) that Git
    would leave out of a commit. Empty outside a git repository or without git."""
    paths: list[str] = []
    for d in dirs:
        if d.is_dir():
            base = d.relative_to(cfg.root).as_posix()
            files = iter_files(d, skip) if skip else iter_files(d)
            paths += [f"{base}/{rel}" for rel, _ in files]
    if not paths:
        return []
    try:
        probe = run(["git", "rev-parse", "--is-inside-work-tree"], cwd=cfg.root, check=False)
        if probe.returncode != 0:
            return []
        # Tracked files are never reported: Git keeps them whatever the ignore rules say.
        proc = run(["git", "check-ignore", "--stdin", "-z", "-v"], cwd=cfg.root,
                   input="\0".join(paths).encode() + b"\0", check=False)  # fmt: skip
    except YMError:
        return []
    if proc.returncode not in (0, 1):
        return []
    fields = proc.stdout.decode(errors="replace").split("\0")
    return [
        Ignored(path=fields[i + 3], rule=f"{fields[i]}:{fields[i + 1]}", pattern=fields[i + 2])
        for i in range(0, len(fields) - 3, 4)
    ]


IGNORED_HINT = "it won't be committed, and clean checkouts (CI) will fail the drift check"


def describe_ignored(ignored: list[Ignored], limit: int = 5) -> list[str]:
    """One line per file, grouped by rule and capped at ``limit`` per rule."""
    by_rule: dict[tuple[str, str], list[str]] = {}
    for item in ignored:
        by_rule.setdefault((item.rule, item.pattern), []).append(item.path)
    lines: list[str] = []
    for (rule, pattern), paths in by_rule.items():
        lines += [f"{p} (ignored by {rule}: {pattern})" for p in paths[:limit]]
        if len(paths) > limit:
            lines.append(f"... and {len(paths) - limit} more ignored by {rule}")
    return lines


def warn_ignored(cfg: SiteConfig, dirs: list[Path], skip: PathFilter | None = None) -> None:
    ignored = ignored_files(cfg, dirs, skip)
    if not ignored:
        return
    console.warn(f"{len(ignored)} vendored file(s) are ignored by Git; {IGNORED_HINT}:\n  "
                 + "\n  ".join(describe_ignored(ignored))
                 + "\n  add a negation such as `!/yourls/**` to .gitignore")  # fmt: skip


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
