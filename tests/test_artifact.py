"""`ym artifact build` and the artifact-only site shape (design §15)."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import time
import zipfile
from pathlib import Path

import pytest

from yourls_manager import __version__
from yourls_manager.cli import build_parser, main
from yourls_manager.doctor import doctor

from .conftest import git, write_files


@pytest.fixture
def site(tmp_path, fake_core, upstream):
    plugin = upstream("tz")
    plugin.commit({"plugin.php": "<?php /* Plugin Name: TZ */\n"})
    site = tmp_path / "site"
    write_files(
        site,
        {
            "site.toml": f"""
                [site]
                name = "T"
                slug = "t"
                url = "https://go.example.com"
                [core]
                repo = "{fake_core.url}"
                version = "1.0.0"
                [[plugins]]
                name = "tz"
                repo = "{plugin.url}"
                [artifact]
                enabled = true
                formats = ["tar.gz", "zip"]
                """,
            "overlay/user/config.php": "<?php // shim\n",
            "overlay/health.php": "<?php echo '{\"ok\": true}';\n",
            ".gitignore": "dist/\ndist-artifacts/\n",
        },
    )
    git(site, "init", "-q", "-b", "main")
    assert main(["--site-dir", str(site), "core"]) == 0
    assert main(["--site-dir", str(site), "plugin", "sync"]) == 0
    git(site, "add", "-A")
    git(site, "commit", "-q", "-m", "init")
    return site


def ym(site: Path, *args: str) -> int:
    return main(["--site-dir", str(site), *args])


def build(site: Path, out: Path, *args: str) -> int:
    return ym(site, "artifact", "build", "--out", str(out), *args)


def test_parser():
    args = build_parser().parse_args(
        ["artifact", "build", "--ref", "v1.0.0-1", "--format", "zip", "--allow-untagged"]
    )
    assert (args.command, args.artifact_cmd, args.ref, args.formats) == (
        "artifact", "build", "v1.0.0-1", ["zip"]
    )  # fmt: skip
    assert args.allow_untagged and args.out is None
    assert build_parser().parse_args(["artifact", "build"]).ref == "HEAD"


def test_refuses_untagged_and_dirty(site, tmp_path, capsys):
    out = tmp_path / "out"
    assert build(site, out) == 2
    assert "has no release tag" in capsys.readouterr().err
    assert build(site, out, "--ref", "nope") == 2
    assert "no such commit" in capsys.readouterr().err

    assert ym(site, "release") == 0
    (site / "overlay" / "health.php").write_text("<?php // edited\n")
    assert build(site, out) == 2
    assert "uncommitted changes" in capsys.readouterr().err
    git(site, "checkout", "--", "overlay/health.php")
    (site / "notes.txt").write_text("untracked counts as dirty too\n")
    assert build(site, out) == 2
    assert not out.exists()


def test_byte_identical_rebuilds(site, tmp_path):
    assert ym(site, "release") == 0
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    assert build(site, a) == 0
    # file times in the working tree don't leak into the archives
    for p in (site / "yourls").rglob("*"):
        os.utime(p, (1_000_000_000, 1_000_000_000))
    assert build(site, b) == 0
    names = sorted(p.name for p in a.iterdir())
    assert (
        names
        == sorted(p.name for p in b.iterdir())
        == [
            "t-1.0.0-1.json",
            "t-1.0.0-1.tar.gz",
            "t-1.0.0-1.tar.gz.sha256",
            "t-1.0.0-1.zip",
            "t-1.0.0-1.zip.sha256",
        ]
    )
    for name in names:
        assert (a / name).read_bytes() == (b / name).read_bytes(), name

    # the tag rebuilt later from another HEAD (temporary worktree) is identical too
    (site / "overlay" / "health.php").write_text("<?php // newer\n")
    git(site, "commit", "-q", "-am", "newer")
    assert build(site, c, "--ref", "v1.0.0-1") == 0
    for name in names:
        assert (a / name).read_bytes() == (c / name).read_bytes(), name
    assert git(site, "worktree", "list").count("\n") == 0  # temporary worktree removed

    # existing files of the same name are replaced, others left alone
    (a / "t-1.0.0-1.zip").write_bytes(b"stale")
    (a / "other.txt").write_text("keep")
    assert build(site, a, "--format", "zip", "--ref", "v1.0.0-1") == 0
    assert (a / "t-1.0.0-1.zip").read_bytes() == (b / "t-1.0.0-1.zip").read_bytes()
    assert (a / "other.txt").read_text() == "keep"


def test_archive_layout(site, tmp_path):
    assert ym(site, "release") == 0
    out = tmp_path / "out"
    assert build(site, out) == 0
    ctime = int(git(site, "show", "-s", "--format=%ct", "HEAD"))

    raw = (out / "t-1.0.0-1.tar.gz").read_bytes()
    assert raw[:3] == b"\x1f\x8b\x08"
    assert raw[3] & 0x08 == 0  # no FNAME in the gzip header
    assert raw[4:8] == b"\0\0\0\0"  # gzip mtime 0
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        members = tar.getmembers()
        names = [m.name for m in members]
        assert names == sorted(names)
        assert "yourls-loader.php" in names and "health.php" in names  # no top-level dir
        assert "user/plugins/tz/plugin.php" in names
        assert "user/config.php" in names
        for m in members:
            assert (m.uid, m.gid, m.uname, m.gname, m.mtime) == (0, 0, "", "", ctime)
            assert m.mode == (0o755 if m.isdir() else 0o644), m.name
            assert m.isdir() or m.isfile()
        assert tar.extractfile("user/config.php").read() == b"<?php // shim\n"
    # a directory entry precedes the files in it
    assert names.index("user") < names.index("user/config.php")

    with zipfile.ZipFile(out / "t-1.0.0-1.zip") as zf:
        infos = zf.infolist()
        znames = [i.filename for i in infos]
        assert [n.rstrip("/") for n in znames] == names
        stamp = time.gmtime(ctime - ctime % 2)[:6]  # zip stores even seconds
        for i in infos:
            assert i.date_time == stamp
            assert i.create_system == 3
            assert (i.external_attr >> 16) & 0o777 == (0o755 if i.is_dir() else 0o644)
        assert zf.read("user/config.php") == b"<?php // shim\n"


def test_sidecars(site, tmp_path):
    assert ym(site, "release", "--rc") == 0
    out = tmp_path / "out"
    assert build(site, out) == 0
    files = {}
    for fmt in ("tar.gz", "zip"):
        archive = out / f"t-1.0.0-1-rc.1.{fmt}"
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        assert (out / f"{archive.name}.sha256").read_bytes() == (
            f"{digest}  {archive.name}\n".encode()
        )
        files[archive.name] = digest
    text = (out / "t-1.0.0-1-rc.1.json").read_text()
    assert text.endswith("}\n")
    meta = json.loads(text)
    lock = json.loads((site / "yourls.lock.json").read_text())
    commit = git(site, "rev-parse", "HEAD")
    committed = git(site, "show", "-s", "--format=%cd", "--date=format-local:%Y-%m-%dT%H:%M:%SZ")
    assert meta == {
        "schema": 1,
        "site": "t",
        "version": "1.0.0-1-rc.1",
        "tag": "v1.0.0-1-rc.1",
        "rc": True,
        "commit": commit,
        "committed": committed,
        "core": {"version": "1.0.0", "commit": lock["core"]["commit"]},
        "plugins": {
            "tz": {"repo": lock["plugins"]["tz"]["repo"], "commit": lock["plugins"]["tz"]["commit"]}
        },
        "manager": __version__,
        "files": files,
    }


def test_newest_tag_wins_unless_ref_names_one(site, tmp_path):
    assert ym(site, "release", "--rc") == 0
    assert ym(site, "release", "promote") == 0  # v1.0.0-1 on the same commit
    out = tmp_path / "out"
    assert build(site, out, "--format", "zip") == 0
    meta = json.loads((out / "t-1.0.0-1.json").read_text())
    assert (meta["tag"], meta["rc"]) == ("v1.0.0-1", False)
    assert build(site, out, "--format", "zip", "--ref", "v1.0.0-1-rc.1") == 0
    meta = json.loads((out / "t-1.0.0-1-rc.1.json").read_text())
    assert (meta["tag"], meta["rc"]) == ("v1.0.0-1-rc.1", True)


def test_allow_untagged_and_default_out(site, capsys):
    assert ym(site, "artifact", "build", "--allow-untagged", "--format", "tar.gz") == 0
    short = git(site, "rev-parse", "--short=7", "HEAD")
    out = site / "dist-artifacts"
    meta = json.loads((out / f"t-0.0.0-dev-{short}.json").read_text())
    assert meta["version"] == f"0.0.0-dev+{short}"
    assert (meta["tag"], meta["rc"], meta["untagged"]) == (None, False, True)
    assert list(meta["files"]) == [f"t-0.0.0-dev-{short}.tar.gz"]
    assert "untagged build" in capsys.readouterr().err
    assert git(site, "status", "--porcelain") == ""  # gitignored output, no dist/ written


def test_custom_name_from_site_toml(site, tmp_path):
    toml = site / "site.toml"
    toml.write_text(toml.read_text().replace('formats = ["tar.gz", "zip"]', 'name = "app-{tag}"'))
    git(site, "commit", "-q", "-am", "name")
    assert ym(site, "release") == 0
    out = tmp_path / "out"
    assert build(site, out) == 0
    assert sorted(p.name for p in out.iterdir()) == [
        "app-v1.0.0-1.json",
        "app-v1.0.0-1.tar.gz",
        "app-v1.0.0-1.tar.gz.sha256",
    ]


def test_drift_refused(site, tmp_path, capsys):
    (site / "yourls" / "admin" / "index.php").write_text("<?php // hacked\n")
    git(site, "commit", "-q", "-am", "edit vendored file")
    assert ym(site, "release", "--dry-run") == 2  # release refuses drift too
    git(site, "tag", "v1.0.0-1")  # tagged by hand
    assert build(site, tmp_path / "out") == 1
    assert "does not match the lock" in capsys.readouterr().err


# --------------------------------------------------------------------------- doctor (§15.3)


def _doctor(site: Path, capsys, *, local: bool = False) -> str:
    capsys.readouterr()
    doctor(site, local=local)
    return capsys.readouterr().out


def test_doctor_artifact_only_site(site, capsys):
    out = _doctor(site, capsys)
    assert "ok    targets: none (artifact-only site)" in out
    assert "warn  artifact: enabled, but there is no .github/workflows/*.y*ml (github)" in out

    write_files(site, {".github/workflows/deploy.yml": "on: push\n"})
    out = _doctor(site, capsys, local=True)
    assert "no CI job runs `ym artifact build` (.github/workflows/deploy.yml)" in out

    write_files(
        site, {".github/workflows/publish.yml": "steps:\n  - run: uv run ym artifact build\n"}
    )
    out = _doctor(site, capsys, local=True)
    assert "ok    artifact: tar.gz, zip, published by .github/workflows/publish.yml" in out

    write_files(site, {".copier-answers.yml": "_commit: v0.7.0\nci_provider: gitlab\n"})
    out = _doctor(site, capsys, local=True)
    assert "there is no .gitlab-ci.yml (gitlab)" in out
    write_files(site, {".gitlab-ci.yml": "publish:\n  script: uv run ym artifact build\n"})
    assert "published by .gitlab-ci.yml" in _doctor(site, capsys, local=True)

    write_files(site, {".copier-answers.yml": "ci_provider: 'none'\n"})
    assert "ci_provider = 'none': nothing publishes it" in _doctor(site, capsys, local=True)


def test_doctor_without_artifact(site, capsys):
    toml = site / "site.toml"
    toml.write_text(toml.read_text().replace("enabled = true", "enabled = false"))
    out = _doctor(site, capsys)
    assert "warn  targets: none defined in site.toml" in out
    assert "artifact:" not in out
