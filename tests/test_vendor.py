from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from yourls_manager import vendor
from yourls_manager.util import YMError
from yourls_manager.vendor import (
    atomic_replace_dir,
    copy_tree,
    glob_match,
    make_exclude,
    prefix_filter,
    tree_sha256,
)

from .conftest import write_files


def test_tree_hash_matches_spec(tmp_path):
    write_files(tmp_path, {"b.txt": "B", "a/x.php": "X", "a/.git/HEAD": "ignored"})
    expected = hashlib.sha256(b"a/x.php\0X\0b.txt\0B\0").hexdigest()
    assert tree_sha256(tmp_path) == expected


def test_tree_hash_sensitive_to_content_and_names(tmp_path):
    write_files(tmp_path, {"a.txt": "1", "d/b.txt": "2"})
    h1 = tree_sha256(tmp_path)
    assert tree_sha256(tmp_path) == h1
    (tmp_path / "d/b.txt").write_text("3")
    h2 = tree_sha256(tmp_path)
    assert h2 != h1
    (tmp_path / "d/b.txt").rename(tmp_path / "d/c.txt")
    assert tree_sha256(tmp_path) not in (h1, h2)


def test_tree_hash_skip_filter(tmp_path):
    write_files(tmp_path, {"core.php": "c", "user/plugins/kqs/plugin.php": "p"})
    skip = prefix_filter(["user/plugins/kqs"])
    only_core = tree_sha256(tmp_path, skip)
    (tmp_path / "user/plugins/kqs/plugin.php").write_text("changed")
    assert tree_sha256(tmp_path, skip) == only_core


def test_symlink_escaping_tree_is_rejected(tmp_path):
    src = tmp_path / "src"
    write_files(src, {"a.txt": "a"})
    outside = tmp_path / "secret"
    outside.write_text("s")
    os.symlink(outside, src / "link")
    with pytest.raises(YMError, match="escapes"):
        tree_sha256(src)


def test_internal_file_symlink_copied_as_file(tmp_path):
    src = tmp_path / "src"
    write_files(src, {"a.txt": "a"})
    os.symlink("a.txt", src / "b.txt")
    dst = tmp_path / "dst"
    copy_tree(src, dst)
    assert not (dst / "b.txt").is_symlink()
    assert (dst / "b.txt").read_text() == "a"


@pytest.mark.parametrize(
    ("pattern", "path", "hit"),
    [
        (".github", ".github/workflows/ci.yml", True),
        (".git*", ".gitattributes", True),
        (".git*", ".github/x", True),
        ("tests", "tests/test.php", True),
        ("tests", "includes/tests/x.php", False),
        ("*.md", "README.md", True),
        ("*.md", "includes/vendor/README.md", False),
        ("**/*.md", "includes/vendor/README.md", True),
        ("includes/vendor/*/tests", "includes/vendor/lib/tests/a.php", True),
        ("admin", "admin2/x", False),
    ],
)
def test_glob_match(pattern, path, hit):
    assert glob_match(pattern, path) is hit


def test_exclude_protects_license_and_readme():
    ex = make_exclude(["LICENSE*", "*.html", "*.md"])
    assert not ex("LICENSE")
    assert not ex("readme.html")
    assert ex("README.md")
    assert ex("other.html")
    explicit = make_exclude(["readme.html"])
    assert explicit("readme.html")


def test_atomic_replace_dir(tmp_path):
    target = tmp_path / "yourls"
    write_files(target, {"old.txt": "old"})
    new = tmp_path / "stage"
    write_files(new, {"new.txt": "new"})
    atomic_replace_dir(target, new)
    assert (target / "new.txt").read_text() == "new"
    assert not (target / "old.txt").exists()
    assert not new.exists()
    assert [p.name for p in tmp_path.iterdir()] == ["yourls"]  # no .old leftovers


def test_atomic_replace_dir_restores_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "yourls"
    write_files(target, {"old.txt": "old"})
    new = tmp_path / "stage"
    write_files(new, {"new.txt": "new"})
    real_replace = os.replace
    calls = []

    def flaky(src, dst):
        calls.append((Path(src).name, Path(dst).name))
        if len(calls) == 2:
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(vendor.os, "replace", flaky)
    with pytest.raises(OSError, match="disk full"):
        atomic_replace_dir(target, new)
    monkeypatch.setattr(vendor.os, "replace", real_replace)
    assert (target / "old.txt").read_text() == "old"
    assert (new / "new.txt").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["stage", "yourls"]


def test_overlay_copy_reports_overrides(tmp_path):
    base = tmp_path / "yourls"
    overlay = tmp_path / "overlay"
    out = tmp_path / "dist"
    write_files(base, {"index.php": "core", "user/plugins/p/plugin.php": "upstream"})
    write_files(overlay, {"user/plugins/p/plugin.php": "patched", ".htaccess": "rw"})
    copy_tree(base, out)
    seen = []
    copy_tree(overlay, out, on_copy=lambda rel, existed: seen.append((rel, existed)))
    assert (out / "user/plugins/p/plugin.php").read_text() == "patched"
    assert (out / "index.php").read_text() == "core"
    assert sorted(seen) == [(".htaccess", False), ("user/plugins/p/plugin.php", True)]
