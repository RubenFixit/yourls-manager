"""`ym release` and the deploy policy (design §11.4, §11.5)."""

from __future__ import annotations

import pytest

from yourls_manager.cli import main
from yourls_manager.config import load_site
from yourls_manager.lockfile import CoreLock, Lock, PluginLock
from yourls_manager.release import (
    check_deploy,
    lock_diff,
    move_marker,
    next_tag,
    parse_tag,
    release_tags,
)
from yourls_manager.util import UsageError

from .conftest import git, write_files


def tags(*names):
    return sorted((parse_tag(n) for n in names), key=lambda t: t.sort_key)


def test_parse_and_order():
    assert parse_tag("v1.10.6-2-rc.3").rc == 3
    assert parse_tag("v1.10.6-2").rc == 0
    for bad in ("v1.10.6", "1.10.6-1", "v1.10.6-0", "v1.10.6-1-rc.0", "v2026.10.28"):
        assert parse_tag(bad) is None
    order = [t.name for t in tags("v1.10.6-2", "v1.10.6-2-rc.1", "v1.10.6-1", "v1.9.2-5")]
    assert order == ["v1.9.2-5", "v1.10.6-1", "v1.10.6-2-rc.1", "v1.10.6-2"]


@pytest.mark.parametrize(
    ("existing", "rc", "expected"),
    [
        ((), False, "v1.10.6-1"),
        ((), True, "v1.10.6-1-rc.1"),
        (("v1.10.6-1",), False, "v1.10.6-2"),
        (("v1.10.6-1",), True, "v1.10.6-2-rc.1"),
        (("v1.10.6-1", "v1.10.6-2-rc.1"), True, "v1.10.6-2-rc.2"),
        (("v1.10.6-1", "v1.10.6-2-rc.1"), False, "v1.10.6-2"),
        (("v1.10.5-4",), False, "v1.10.6-1"),  # revision resets per YOURLS version
    ],
)
def test_next_tag(existing, rc, expected):
    assert next_tag(tags(*existing), "1.10.6", rc=rc) == expected


def test_lock_diff():
    old = Lock(
        core=CoreLock("r", "1.10.5", "a" * 40, "h", "t"),
        plugins={
            "tz": PluginLock("r", "HEAD", ".", "b" * 40, "h", "t"),
            "gone": PluginLock("r", "HEAD", ".", "c" * 40, "h", "t"),
        },
    )
    new = Lock(
        core=CoreLock("r", "1.10.6", "d" * 40, "h", "t"),
        plugins={
            "tz": PluginLock("r", "HEAD", ".", "e" * 40, "h", "t"),
            "new": PluginLock("r", "HEAD", ".", "f" * 40, "h", "t"),
        },
    )
    assert lock_diff(old, new) == [
        "YOURLS 1.10.5 -> 1.10.6",
        "added plugin new (fffffff)",
        "plugin tz bbbbbbb -> eeeeeee",
        "removed plugin gone",
    ]


@pytest.fixture
def site(tmp_path, fake_core):
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
                [deploy.targets.staging]
                path = "{tmp_path}/www-staging"
                [deploy.targets.prod]
                path = "{tmp_path}/www"
                accept = "release"
                """,
            ".gitignore": "dist/\n",
        },
    )
    git(site, "init", "-q", "-b", "main")
    git(site, "add", "-A")
    git(site, "commit", "-q", "-m", "init")
    assert main(["--site-dir", str(site), "core"]) == 0
    git(site, "add", "-A")
    git(site, "commit", "-q", "-m", "core")
    return site


def test_release_flow(site, capsys):
    S = ["--site-dir", str(site)]
    assert main([*S, "release", "--dry-run"]) == 0
    assert "release v1.0.0-1" in capsys.readouterr().out
    assert git(site, "tag") == ""

    assert main([*S, "release", "--rc", "-m", "first candidate"]) == 0
    assert git(site, "tag") == "v1.0.0-1-rc.1"
    msg = git(site, "tag", "-l", "--format=%(contents)", "v1.0.0-1-rc.1")
    assert "First release." in msg and "YOURLS 1.0.0" in msg and "first candidate" in msg

    assert main([*S, "release", "--rc"]) == 2  # HEAD already carries an rc
    (site / "note.txt").write_text("x")
    assert main([*S, "release"]) == 2  # dirty tree
    git(site, "add", "-A")
    git(site, "commit", "-q", "-m", "fix")
    assert main([*S, "release", "--rc"]) == 0
    assert main([*S, "release", "--promote"]) == 0
    assert git(site, "rev-parse", "v1.0.0-1^{}") == git(site, "rev-parse", "v1.0.0-1-rc.2^{}")
    assert git(site, "cat-file", "-p", "v1.0.0-1").splitlines()[1] == "type commit"
    assert main([*S, "release", "--promote"]) == 2  # already promoted
    assert main([*S, "release"]) == 2  # HEAD already released as v1.0.0-1

    # core update -> notes show the change, revision resets
    assert main([*S, "core", "--version", "1.1.0"]) == 0
    git(site, "add", "-A")
    git(site, "commit", "-q", "-m", "YOURLS 1.1.0")
    capsys.readouterr()
    assert main([*S, "release"]) == 0
    assert "Changes since v1.0.0-1:" in capsys.readouterr().out
    assert [t.name for t in release_tags(site)][-1] == "v1.1.0-1"

    git(site, "branch", "-f", "deployed/prod", "v1.0.0-1")
    assert main([*S, "release", "--list"]) == 0
    out = capsys.readouterr().out
    assert "v1.0.0-1 " in out and "deployed: prod" in out


def test_deploy_policy(site, capsys):
    cfg = load_site(site)
    staging, prod = cfg.target("staging"), cfg.target("prod")
    assert cfg.target("staging").accept == "any" and prod.accept == "release"

    # untagged HEAD: staging ok, prod refused unless forced
    assert check_deploy(cfg, staging, allow_dirty=False, force=False) == {
        "tag": None,
        "forced": False,
    }
    with pytest.raises(UsageError, match="only accepts releases"):
        check_deploy(cfg, prod, allow_dirty=False, force=False)
    assert check_deploy(cfg, prod, allow_dirty=False, force=True)["forced"] is True
    assert main(["--site-dir", str(site), "deploy", "prod"]) == 2  # refused before any work

    # rc tags are not releases for prod
    git(site, "tag", "-a", "v1.0.0-1-rc.1", "-m", "rc")
    with pytest.raises(UsageError):
        check_deploy(cfg, prod, allow_dirty=False, force=False)
    assert check_deploy(cfg, staging, allow_dirty=False, force=False)["tag"] == "v1.0.0-1-rc.1"
    git(site, "tag", "-a", "v1.0.0-1", "-m", "final")
    assert check_deploy(cfg, prod, allow_dirty=False, force=False) == {
        "tag": "v1.0.0-1",
        "forced": False,
    }
    assert "not on any remote branch" not in capsys.readouterr().err  # no remote: local only
    git(site, "remote", "add", "origin", str(site / "nowhere.git"))
    check_deploy(cfg, prod, allow_dirty=False, force=False)
    assert "not on any remote branch" in capsys.readouterr().err
    git(site, "remote", "remove", "origin")

    # dirty tree: refused everywhere unless --allow-dirty; prod still needs --force
    (site / "wip.txt").write_text("x")
    with pytest.raises(UsageError, match="--allow-dirty"):
        check_deploy(cfg, staging, allow_dirty=False, force=False)
    check_deploy(cfg, staging, allow_dirty=True, force=False)
    with pytest.raises(UsageError, match="uncommitted"):
        check_deploy(cfg, prod, allow_dirty=True, force=False)
    move_marker(cfg, staging)  # dirty: marker not moved
    assert git(site, "branch", "--list", "deployed/*") == ""
    (site / "wip.txt").unlink()
    move_marker(cfg, prod)
    assert git(site, "rev-parse", "deployed/prod") == git(site, "rev-parse", "HEAD")


def test_accept_validated(site):
    toml = (site / "site.toml").read_text().replace('accept = "release"', 'accept = "tags"')
    (site / "site.toml").write_text(toml)
    with pytest.raises(UsageError, match="accept"):
        load_site(site)


def test_update(site, capsys):
    S = ["--site-dir", str(site)]
    assert main([*S, "update", "--plugins-only"]) == 0
    assert "everything is up to date" in capsys.readouterr().out
    assert main([*S, "update", "--commit"]) == 0  # fake core has 1.1.0
    out = capsys.readouterr().out
    assert "YOURLS 1.0.0 -> 1.1.0" in out
    assert git(site, "log", "-1", "--format=%s") == "Update YOURLS 1.0.0 -> 1.1.0"
    assert git(site, "status", "--porcelain") == ""
    assert main([*S, "update"]) == 0
    assert "everything is up to date" in capsys.readouterr().out


def test_plugin_enable_disable_list(site, capsys):
    S = ["--site-dir", str(site)]
    toml = site / "site.toml"
    toml.write_text(
        toml.read_text()
        + '\n[[plugins]]\nname = "hyphens"   # bundled\n\n[[plugins]]\nname = "tz"\n'
        "active = true  # keep\n"
    )
    assert main([*S, "plugin", "disable", "tz"]) == 0
    assert "active = false  # keep" in toml.read_text()
    assert main([*S, "plugin", "disable", "hyphens"]) == 0
    assert 'name = "hyphens"   # bundled\nactive = false\n' in toml.read_text()
    assert main([*S, "plugin", "enable", "hyphens"]) == 0
    assert main([*S, "plugin", "enable", "hyphens"]) == 0
    assert "already active" in capsys.readouterr().out
    assert main([*S, "plugin", "enable", "nope"]) == 2
    assert main([*S, "plugin", "list"]) == 0
    out = capsys.readouterr().out
    assert "hyphens" in out and "tz " in out and "core" in out


def test_plugin_remove(site, upstream, capsys):
    S = ["--site-dir", str(site)]
    plug = upstream("plug")
    plug.commit({"plugin.php": "<?php // p\n"})
    toml = site / "site.toml"
    toml.write_text(
        toml.read_text() + f'\n[[plugins]]\nname = "p"\nrepo = "{plug.url}"\n\n'
        '# the next one\n[[plugins]]\nname = "hyphens"\n'
    )
    assert main([*S, "sync"]) == 0
    assert (site / "yourls/user/plugins/p/plugin.php").is_file()
    git(site, "add", "-A")
    git(site, "commit", "-q", "-m", "add p")
    assert main([*S, "plugin", "remove", "p"]) == 0
    text = toml.read_text()
    assert 'name = "p"' not in text and '# the next one\n[[plugins]]\nname = "hyphens"' in text
    assert not (site / "yourls/user/plugins/p").exists()
    assert "plugin data" in capsys.readouterr().out
    assert main([*S, "plugin", "remove", "p"]) == 2


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://github.com/YOURLS/timezones",
            ("https://github.com/YOURLS/timezones.git", None, None),
        ),
        ("https://github.com/o/r.git", ("https://github.com/o/r.git", None, None)),
        (
            "https://github.com/joshp23/YOURLS-U-SRV/tree/master/usrv",
            ("https://github.com/joshp23/YOURLS-U-SRV.git", "master", "usrv"),
        ),
        (
            "https://github.com/o/r/blob/main/sub/plugin.php",
            ("https://github.com/o/r.git", "main", "sub"),
        ),
        ("https://gitlab.com/g/r/-/tree/v2/x", ("https://gitlab.com/g/r.git", "v2", "x")),
        (
            "https://bitbucket.org/o/r/src/master/",
            ("https://bitbucket.org/o/r.git", "master", None),
        ),
        ("git@github.com:o/r.git", ("git@github.com:o/r.git", None, None)),
    ],
)
def test_parse_plugin_url(url, expected):
    from yourls_manager.plugincmd import parse_plugin_url

    assert parse_plugin_url(url) == expected


def test_plugin_add(site, upstream, capsys):
    S = ["--site-dir", str(site)]
    mono = upstream("mono")
    mono.commit({"plug-b/plugin.php": "<?php // b\n", "plug-c/plugin.php": "<?php // c\n"})
    assert main([*S, "plugin", "add", mono.url]) == 2  # several plugins, no terminal
    assert "--path: plug-b, plug-c" in capsys.readouterr().err
    assert "[[plugins]]" not in (site / "site.toml").read_text()
    assert main([*S, "plugin", "add", mono.url, "--path", "nope"]) == 2
    assert "[[plugins]]" not in (site / "site.toml").read_text()
    assert main([*S, "plugin", "add", mono.url, "--path", "plug-b", "--inactive"]) == 0
    assert (site / "yourls/user/plugins/plug-b/plugin.php").is_file()
    assert 'path   = "plug-b"\nactive = false' in (site / "site.toml").read_text()
    assert main([*S, "plugin", "add", mono.url, "--path", "plug-b"]) == 2  # duplicate


def test_release_push_needs_origin(site, capsys):
    S = ["--site-dir", str(site)]
    assert main([*S, "release", "--push"]) == 2
    err = capsys.readouterr().err
    assert "no git remote `origin`" in err and "ym repo create" in err
    assert release_tags(site) == []  # refused before tagging
    bare = site.parent / "origin.git"
    git(site.parent, "init", "-q", "--bare", str(bare))
    git(site, "remote", "add", "origin", str(bare))
    assert main([*S, "release", "--push"]) == 0
    assert "not on any remote branch" in capsys.readouterr().err  # branch never pushed
    assert git(bare, "tag") == "v1.0.0-1"
