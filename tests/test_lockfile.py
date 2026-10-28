from __future__ import annotations

import json

import pytest

from yourls_manager.lockfile import (
    CoreLock,
    Lock,
    PluginLock,
    load_lock,
    lock_sha256,
    parse_lock,
    write_lock,
)
from yourls_manager.util import UsageError


def sample() -> Lock:
    return Lock(
        core=CoreLock(
            repo="https://github.com/YOURLS/YOURLS.git",
            version="1.10.6",
            commit="a" * 40,
            tree_sha256="b" * 64,
            resolved_at="2026-10-27T00:00:00Z",
        ),
        plugins={
            "zeta": PluginLock(
                "https://x/z", "HEAD", ".", "c" * 40, "d" * 64, "2026-10-27T00:00:00Z"
            ),
            "alpha": PluginLock(
                "https://x/a", "main", "sub", "e" * 40, "f" * 64, "2026-10-27T00:00:00Z"
            ),
        },
    )


def test_round_trip(tmp_path):
    path = tmp_path / "yourls.lock.json"
    write_lock(path, sample())
    loaded = load_lock(path)
    assert loaded == sample()
    write_lock(path, loaded)
    assert path.read_text() == sample().dumps()


def test_stable_output_format():
    text = sample().dumps()
    assert text.endswith("}\n") and not text.endswith("\n\n")
    assert '\n  "core": {\n    "commit"' in text  # 2-space indent, sorted keys
    keys = list(json.loads(text))
    assert keys == sorted(keys) == ["core", "lock_version", "plugins"]
    assert list(json.loads(text)["plugins"]) == ["alpha", "zeta"]
    # insertion order does not matter
    other = sample()
    other.plugins = dict(reversed(list(other.plugins.items())))
    assert other.dumps() == text


def test_missing_lock_is_empty(tmp_path):
    lock = load_lock(tmp_path / "nope.json")
    assert lock.core is None and lock.plugins == {}
    assert lock_sha256(tmp_path / "nope.json") is None


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"lock_version": 2},
        {"lock_version": 1, "core": {"repo": "x"}},
        {"lock_version": 1, "core": None, "plugins": {"a": {"repo": "x"}}},
    ],
)
def test_invalid_locks(data):
    with pytest.raises(UsageError):
        parse_lock(data)


def test_plugin_matches():
    p = sample().plugins["alpha"]
    assert p.matches("https://x/a", "main", "sub")
    assert not p.matches("https://x/a", "HEAD", "sub")
