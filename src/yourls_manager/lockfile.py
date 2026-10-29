"""Read and write ``yourls.lock.json``: the exact core and plugin commits a site vendors.

Written only by ``ym core`` and ``ym plugin sync`` (and the commands built on them)::

    {
      "lock_version": 1,
      "core": {"repo": "...", "version": "1.10.6", "commit": "<sha>",
               "tree_sha256": "<hex>", "resolved_at": "<ISO 8601 UTC>"},
      "plugins": {
        "<name>": {"repo": "...", "ref": "HEAD", "path": ".", "commit": "<sha>",
                   "tree_sha256": "<hex>", "resolved_at": "..."}
      }
    }

``tree_sha256`` is :func:`vendor.tree_sha256` of the vendored files; for core it skips the
managed plugin directories. Plugins without a ``repo`` (bundled with core, or in
``overlay/``) are not in the lock. Keys are sorted, indented by 2, with a trailing newline,
so diffs stay small.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .util import UsageError

LOCK_VERSION = 1


@dataclass
class CoreLock:
    repo: str
    version: str
    commit: str
    tree_sha256: str
    resolved_at: str


@dataclass
class PluginLock:
    repo: str
    ref: str
    path: str
    commit: str
    tree_sha256: str
    resolved_at: str

    def matches(self, repo: str, ref: str, path: str) -> bool:
        return (self.repo, self.ref, self.path) == (repo, ref, path)


@dataclass
class Lock:
    core: CoreLock | None = None
    plugins: dict[str, PluginLock] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"lock_version": LOCK_VERSION}
        data["core"] = asdict(self.core) if self.core else None
        data["plugins"] = {name: asdict(p) for name, p in self.plugins.items()}
        return data

    def dumps(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _require(d: dict[str, Any], keys: tuple[str, ...], where: str) -> None:
    missing = [k for k in keys if not isinstance(d.get(k), str)]
    if missing:
        raise UsageError(f"yourls.lock.json: {where}: missing/invalid {', '.join(missing)}")


def parse_lock(data: Any) -> Lock:
    if not isinstance(data, dict):
        raise UsageError("yourls.lock.json: top level must be an object")
    if data.get("lock_version") != LOCK_VERSION:
        raise UsageError(
            f"yourls.lock.json: unsupported lock_version {data.get('lock_version')!r} "
            f"(this tool understands {LOCK_VERSION})"
        )
    lock = Lock()
    core = data.get("core")
    if core is not None:
        if not isinstance(core, dict):
            raise UsageError("yourls.lock.json: core must be an object")
        keys = ("repo", "version", "commit", "tree_sha256", "resolved_at")
        _require(core, keys, "core")
        lock.core = CoreLock(**{k: core[k] for k in keys})
    plugins = data.get("plugins") or {}
    if not isinstance(plugins, dict):
        raise UsageError("yourls.lock.json: plugins must be an object")
    for name, p in plugins.items():
        if not isinstance(p, dict):
            raise UsageError(f"yourls.lock.json: plugins.{name} must be an object")
        keys = ("repo", "ref", "path", "commit", "tree_sha256", "resolved_at")
        _require(p, keys, f"plugins.{name}")
        lock.plugins[name] = PluginLock(**{k: p[k] for k in keys})
    return lock


def load_lock(path: Path) -> Lock:
    if not path.exists():
        return Lock()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise UsageError(f"{path}: invalid JSON: {exc}") from None
    return parse_lock(data)


def write_lock(path: Path, lock: Lock) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(lock.dumps(), encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def lock_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()
