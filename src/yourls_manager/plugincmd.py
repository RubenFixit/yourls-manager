"""`ym plugin list | enable | disable` (design §11.1). site.toml edits are textual."""

from __future__ import annotations

import re

from .config import SiteConfig, load_site
from .lockfile import load_lock
from .util import UsageError, console

_TABLE = re.compile(r"^\s*\[")
_PLUGINS = re.compile(r"^\s*\[\[\s*plugins\s*\]\]")
_ACTIVE = re.compile(r"^(\s*active\s*=\s*)(true|false)(.*)$")


def _name_re(name: str) -> re.Pattern[str]:
    return re.compile(rf"""^\s*name\s*=\s*(["']){re.escape(name)}\1""")


def set_active(cfg: SiteConfig, name: str, active: bool) -> bool:
    """Set ``active`` for plugin ``name`` in site.toml. Returns False if unchanged."""
    spec = cfg.plugin(name)
    if spec is None:
        raise UsageError(f"no plugin named {name!r} in site.toml (see `ym plugin list`)")
    if spec.active == active:
        return False
    path = cfg.site_file
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    start = end = None
    for i, line in enumerate(lines):
        if start is None and _PLUGINS.match(line):
            j = i + 1
            while j < len(lines) and not _TABLE.match(lines[j]):
                j += 1
            if any(_name_re(name).match(x) for x in lines[i + 1 : j]):
                start, end = i, j
    if start is None:
        raise UsageError(f"could not find the [[plugins]] block for {name!r} in site.toml")
    value = "true" if active else "false"
    for k in range(start + 1, end):
        m = _ACTIVE.match(lines[k].rstrip("\n"))
        if m:
            lines[k] = f"{m[1]}{value}{m[3]}\n"
            break
    else:
        k = next(k for k in range(start + 1, end) if _name_re(name).match(lines[k]))
        indent = re.match(r"\s*", lines[k])[0]
        lines.insert(k + 1, f"{indent}active = {value}\n")
    path.write_text("".join(lines), encoding="utf-8")
    try:
        if load_site(cfg.root).plugin(name).active != active:
            raise UsageError("edit did not take effect")
    except Exception:
        path.write_text(original, encoding="utf-8")
        raise
    return True


def list_plugins(cfg: SiteConfig) -> None:
    lock = load_lock(cfg.lock_file)
    rows = [("name", "active", "source", "ref", "commit")]
    for p in cfg.plugins:
        locked = lock.plugins.get(p.name)
        if p.repo:
            source = p.repo + ("" if p.path in ("", ".") else f" ({p.path})")
        elif (cfg.overlay_dir / "user/plugins" / p.name).is_dir():
            source = "overlay"
        else:
            source = "core"
        rows.append((p.name, "yes" if p.active else "no", source, p.ref if p.repo else "-",
                     locked.commit[:10] if locked else "-"))  # fmt: skip
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    for r in rows:
        console.info("  ".join(c.ljust(w) for c, w in zip(r, widths, strict=False)) + "  " + r[4])
