"""`ym update --check` (formerly `ym check`): ask upstreams for updates."""

from __future__ import annotations

import json
from typing import Any

from . import gitsrc
from .config import SiteConfig
from .lockfile import load_lock
from .util import YMError, console


def _table(rows: list[list[str]], header: list[str]) -> str:
    widths = [max(len(str(r[i])) for r in [header, *rows]) for i in range(len(header))]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*header), fmt.format(*("-" * w for w in widths))]
    lines += [fmt.format(*map(str, r)) for r in rows]
    return "\n".join(line.rstrip() for line in lines)


def check_updates(cfg: SiteConfig) -> dict[str, Any]:
    """Query upstreams. Returns a JSON-serializable report."""
    lock = load_lock(cfg.lock_file)
    report: dict[str, Any] = {"core": None, "plugins": {}, "updates_available": False}

    tags = gitsrc.remote_tags(cfg.core.repo)
    latest = gitsrc.latest_stable_tag(tags)
    current = lock.core.version if lock.core else cfg.core.version
    cur_ver = gitsrc.parse_stable_semver(current)
    core_update = False
    if latest:
        lat_ver = gitsrc.parse_stable_semver(latest[0])
        core_update = cur_ver is None or (lat_ver is not None and lat_ver > cur_ver)
    report["core"] = {
        "current": current,
        "latest": latest[0] if latest else None,
        "update": core_update,
    }

    for p in cfg.managed_plugins:
        assert p.repo is not None
        entry = lock.plugins.get(p.name)
        try:
            remote = gitsrc.resolve_ref(p.repo, p.ref)
            error = None
        except YMError as exc:
            remote, error = None, str(exc)
        locked = entry.commit if entry else None
        changed = entry is not None and not entry.matches(p.repo, p.ref, p.path)
        update = remote is not None and (locked != remote or changed)
        report["plugins"][p.name] = {
            "ref": p.ref,
            "current": locked,
            "available": remote,
            "update": update,
            **({"error": error} if error else {}),
        }
    report["updates_available"] = core_update or any(
        v["update"] for v in report["plugins"].values()
    )
    return report


def print_check(report: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return
    rows = []
    c = report["core"]
    rows.append(["core", "tags", c["current"], c["latest"] or "?", "UPDATE" if c["update"] else ""])
    for name, p in report["plugins"].items():
        short = lambda s: s[:10] if s else "-"  # noqa: E731
        flag = "UPDATE" if p["update"] else ("ERROR" if p.get("error") else "")
        rows.append([name, p["ref"], short(p["current"]), short(p["available"]), flag])
    console.info(_table(rows, ["component", "ref", "current", "available", ""]))
    for name, p in report["plugins"].items():
        if p.get("error"):
            console.warn(f"{name}: {p['error']}")
    console.info("")
    console.info("updates available" if report["updates_available"] else "everything up to date")
