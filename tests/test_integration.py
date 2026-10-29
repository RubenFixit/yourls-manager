"""core -> sync -> build -> deploy against fake upstreams and a local rsync target.

PHP is replaced by a fake ``php`` executable that emulates ym-helper.php's JSON contract,
so the real command construction, quoting, env passing and rsync are exercised without a
PHP/MySQL stack. See test_helper_php.py for the real helper.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from yourls_manager import __version__
from yourls_manager.cli import main
from yourls_manager.deploy import rsync as rsync_mod

from .conftest import core_files, git, needs_rsync, write_files

FAKE_PHP = r"""
import gzip, json, os, sys
args = sys.argv[1:]
if args[:1] == ["-r"]:
    sys.stdout.write("8.3.0"); sys.exit(0)
helper, cmd, *rest = args
opts = dict(a[2:].split("=", 1) for a in rest)
with open(os.environ["FAKE_PHP_LOG"], "a") as fh:
    fh.write(json.dumps({"helper": helper, "cmd": cmd, "opts": opts,
                         "config_env": os.environ.get("YOURLS_CONFIG_FILE")}) + "\n")
assert os.path.isfile(helper), helper
installed = os.environ.get("FAKE_INSTALLED", "1") == "1"
if cmd == "info":
    out = {"ok": True, "installed": installed, "upgrade_needed": False, "active_plugins": []}
elif cmd == "dump":
    os.makedirs(os.path.dirname(opts["out"]), exist_ok=True)
    with gzip.open(opts["out"], "wb") as fh:
        fh.write(b"-- dump\n")
    out = {"ok": True, "out": opts["out"], "bytes": os.path.getsize(opts["out"])}
elif cmd == "upgrade":
    out = {"ok": True, "upgraded": False}
elif cmd == "plugins":
    active = [a for a in opts["active"].split(",") if a]
    out = {"ok": True, "activated": active, "deactivated": [], "active": active}
else:
    print(json.dumps({"ok": False, "error": "unknown"})); sys.exit(1)
print(json.dumps(out))
"""


def make_fake_php(path: Path) -> Path:
    path.write_text(f"#!{sys.executable}\n{FAKE_PHP}")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def site_toml(core_url: str, kqs_url: str, mono_url: str, web: Path, php: Path, url: str) -> str:
    return textwrap.dedent(
        f"""\
        [site]
        name = "Test Links"
        slug = "test-links"
        url  = "{url}"

        [core]
        repo    = "{core_url}"
        version = "1.0.0"
        exclude = [".github", ".git*", "tests", "*.md"]

        [[plugins]]
        name = "kqs"
        repo = "{kqs_url}"

        [[plugins]]
        name   = "multi"
        repo   = "{mono_url}"
        ref    = "main"
        path   = "plug-b"
        active = false

        [[plugins]]
        name = "hyphens-in-urls"

        [[plugins]]
        name = "site-branding"

        [deploy.targets.local]
        type        = "rsync"
        path        = "{web}/site"
        php         = "{php}"
        protect     = [".well-known/"]
        backup_keep = 2
        """
    )


def load_lock(site: Path) -> dict:
    return json.loads((site / "yourls.lock.json").read_text())


def helper_calls(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


@needs_rsync
def test_full_flow(tmp_path, upstream, server, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    log = tmp_path / "php.log"
    monkeypatch.setenv("FAKE_PHP_LOG", str(log))
    php = make_fake_php(tmp_path / "fake-php")

    core = upstream("core")
    core.commit(core_files("1.0.0", "506"), tag="1.0.0")
    kqs = upstream("kqs")
    kqs_c1 = kqs.commit({"plugin.php": "<?php // kqs v1\n", "README.md": "kqs"})
    mono = upstream("mono")
    mono.commit({"plug-b/plugin.php": "<?php // b\n", "other/x.txt": "x"})

    web = tmp_path / "www"
    (web / "site/.well-known").mkdir(parents=True)
    (web / "site/.well-known/acme.txt").write_text("keep me")
    (web / "site/stale.php").write_text("delete me")
    (web / "yourls-config").mkdir()
    (web / "yourls-config/test-links.php").write_text("<?php // secrets\n")

    site = tmp_path / "site"
    write_files(
        site,
        {
            "site.toml": site_toml(core.url, kqs.url, mono.url, web, php, server),
            "overlay/.htaccess": "RewriteEngine On\n",
            "overlay/health.php": "<?php echo '{\"ok\":true}';\n",
            "overlay/user/config.php": "<?php // shim\n",
            "overlay/user/plugins/kqs/plugin.php": "<?php // kqs patched by overlay\n",
            "overlay/user/plugins/site-branding/plugin.php": "<?php // branding\n",
            "tests/redirects.toml": '[[case]]\npath = "/g"\nstatus = 301\n'
            'location = "https://git.example.org/*"\n',
            ".gitignore": "dist/\n",
        },
    )
    git(site, "init", "-q", "-b", "main")
    git(site, "add", "-A")
    git(site, "commit", "-q", "-m", "init")
    S = ["--site-dir", str(site)]

    # ---- core
    assert main([*S, "core"]) == 0
    y = site / "yourls"
    assert "'1.0.0'" in (y / "includes/version.php").read_text()
    assert (y / "LICENSE").exists() and (y / "readme.html").exists()
    assert not (y / "README.md").exists() and not (y / "CHANGELOG.md").exists()
    assert (y / "includes/vendor/lib/README.md").exists()  # excludes are anchored
    assert not (y / ".github").exists() and not (y / "tests").exists()
    assert not (y / ".gitattributes").exists()
    lock = load_lock(site)
    assert lock["core"]["version"] == "1.0.0" and lock["plugins"] == {}

    # ---- sync
    assert main([*S, "plugin", "sync"]) == 0
    lock = load_lock(site)
    assert lock["plugins"]["kqs"]["commit"] == kqs_c1
    assert lock["plugins"]["multi"]["path"] == "plug-b"
    assert (y / "user/plugins/kqs/README.md").exists()
    assert (y / "user/plugins/multi/plugin.php").exists()
    assert not (y / "user/plugins/multi/other").exists()
    git(site, "add", "-A")
    git(site, "commit", "-q", "-m", "vendor")
    assert main([*S, "plugin", "sync"]) == 0  # idempotent
    assert "everything up to date" in capsys.readouterr().out

    # ---- status
    assert main([*S, "doctor", "--local"]) == 0
    assert "no drift" in capsys.readouterr().out

    # ---- build + drift detection
    assert main([*S, "build", "-v"]) == 0
    out = capsys.readouterr().out
    assert "overlay overrides user/plugins/kqs/plugin.php" in out
    dist = site / "dist"
    assert (dist / "user/plugins/kqs/plugin.php").read_text() == "<?php // kqs patched by overlay\n"
    assert (dist / ".htaccess").exists()
    (y / "admin/index.php").write_text("<?php // hacked\n")
    assert main([*S, "build"]) == 1
    assert "differ from the locked tree hash" in capsys.readouterr().err
    assert main([*S, "build", "--allow-drift"]) == 0
    assert main([*S, "core"]) == 2  # dirty-tree guard
    assert "uncommitted changes" in capsys.readouterr().err
    git(site, "checkout", "--", "yourls")

    # ---- deploy --dry-run
    assert main([*S, "deploy", "local", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "dry run" in out and re.search(r"deleting\s+stale\.php", out)
    assert (web / "site/stale.php").exists()
    assert helper_calls(log) == []

    # ---- deploy (x3 to exercise backup pruning)
    counter = iter(range(100))
    monkeypatch.setattr(rsync_mod, "compact_utc", lambda: f"20261027T0000{next(counter):02d}Z")
    assert main([*S, "deploy", "local"]) == 0
    out = capsys.readouterr().out
    assert "PASS  /g" in out
    root = web / "site"
    assert (root / "yourls-loader.php").exists()
    assert (root / "user/plugins/kqs/plugin.php").read_text().endswith("overlay\n")
    assert not (root / "stale.php").exists()
    assert (root / ".well-known/acme.txt").read_text() == "keep me"
    helper = home / ".yourls-manager" / f"ym-helper-{__version__}.php"
    assert helper.read_text().startswith("<?php")
    assert oct(helper.stat().st_mode & 0o777) == "0o600"
    calls = helper_calls(log)
    assert [c["cmd"] for c in calls] == ["dump", "info", "upgrade", "plugins"]
    config_abs = str(web / "yourls-config/test-links.php")
    assert all(c["config_env"] == config_abs for c in calls)
    assert calls[0]["opts"]["config"] == config_abs
    assert calls[0]["opts"]["out"].startswith(str(web / "yourls-backups/test-links/test-links-"))
    assert calls[2]["opts"]["root"] == str(root)
    assert calls[3]["opts"]["active"] == "kqs,hyphens-in-urls,site-branding"
    release = json.loads((home / ".yourls-manager/test-links/release.json").read_text())
    assert release["git_commit"] == git(site, "rev-parse", "HEAD")
    assert release["git_dirty"] is False
    assert release["tool_version"] == __version__
    assert release["core_version"] == "1.0.0"
    assert len(release["lock_sha256"]) == 64
    assert not (root / "release.json").exists()

    for _ in range(2):
        assert main([*S, "deploy", "local", "--no-build", "--skip-verify"]) == 0
    dumps = sorted(os.listdir(web / "yourls-backups/test-links"))
    assert dumps == ["test-links-20261027T000001Z.sql.gz", "test-links-20261027T000002Z.sql.gz"]
    with gzip.open(web / "yourls-backups/test-links" / dumps[0]) as fh:
        assert fh.read() == b"-- dump\n"

    # ---- --skip-backup, not-installed DB
    log.unlink()
    monkeypatch.setenv("FAKE_INSTALLED", "0")
    assert main([*S, "deploy", "local", "--skip-backup", "--skip-verify"]) == 0
    assert [c["cmd"] for c in helper_calls(log)] == ["info"]
    assert "not installed" in capsys.readouterr().err
    assert main([*S, "plugin", "apply", "local"]) == 1
    monkeypatch.setenv("FAKE_INSTALLED", "1")
    assert main([*S, "plugin", "apply", "local"]) == 0

    # ---- updates: check, core latest, sync --update
    core.commit(core_files("1.1.0"), tag="1.1.0")
    core.commit(core_files("1.2.0-rc1"), tag="1.2.0-rc1")
    kqs_c2 = kqs.commit({"plugin.php": "<?php // kqs v2\n"})
    capsys.readouterr()
    assert main([*S, "update", "--check", "--json"]) == 10
    report = json.loads(capsys.readouterr().out)
    assert report["core"] == {"current": "1.0.0", "latest": "1.1.0", "update": True}
    assert report["plugins"]["kqs"]["update"] is True
    assert report["plugins"]["multi"]["update"] is False

    assert main([*S, "core", "latest"]) == 0
    assert 'version = "1.1.0"' in (site / "site.toml").read_text()
    assert "'1.1.0'" in (y / "includes/version.php").read_text()
    assert (y / "user/plugins/kqs/plugin.php").read_text() == "<?php // kqs v1\n"  # preserved
    assert (y / "user/plugins/multi/plugin.php").exists()
    assert main([*S, "plugin", "sync", "--update"]) == 0
    assert load_lock(site)["plugins"]["kqs"]["commit"] == kqs_c2
    assert main([*S, "doctor", "--local"]) == 0
    assert "no drift" in capsys.readouterr().out
    git(site, "add", "-A")
    git(site, "commit", "-q", "-m", "update")
    assert main([*S, "update", "--check"]) == 0

    # ---- removing a plugin from site.toml removes its managed dir
    toml = (site / "site.toml").read_text()
    start = toml.index('[[plugins]]\nname   = "multi"')
    end = toml.index('[[plugins]]\nname = "hyphens-in-urls"')
    (site / "site.toml").write_text(toml[:start] + toml[end:])
    assert main([*S, "plugin", "sync", "--dry-run"]) == 0
    assert (y / "user/plugins/multi").exists()
    assert main([*S, "plugin", "sync"]) == 0
    assert not (y / "user/plugins/multi").exists()
    assert "multi" not in load_lock(site)["plugins"]
    assert main([*S, "build"]) == 0


def test_build_requires_active_plugins(tmp_path, upstream, capsys):
    core = upstream("core")
    core.commit(core_files("1.0.0"), tag="1.0.0")
    site = tmp_path / "site"
    write_files(
        site,
        {
            "site.toml": textwrap.dedent(
                f"""\
                [site]
                name = "T"
                slug = "t"
                url = "https://t.example"
                [core]
                repo = "{core.url}"
                version = "1.0.0"
                [[plugins]]
                name = "does-not-exist"
                """
            ),
        },
    )
    S = ["--site-dir", str(site)]
    assert main([*S, "core"]) == 0
    assert main([*S, "build"]) == 1
    assert "does-not-exist" in capsys.readouterr().err


def test_deploy_missing_config_exits_2(tmp_path, upstream, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    php = make_fake_php(tmp_path / "fake-php")
    core = upstream("core")
    core.commit(core_files("1.0.0"), tag="1.0.0")
    site = tmp_path / "site"
    write_files(
        site,
        {
            "site.toml": textwrap.dedent(
                f"""\
                [site]
                name = "T"
                slug = "t"
                url = "https://t.example"
                [core]
                repo = "{core.url}"
                version = "1.0.0"
                [deploy.targets.here]
                path = "www/t"
                php = "{php}"
                """
            ),
            "overlay/user/config.php": "<?php\n",
        },
    )
    S = ["--site-dir", str(site)]
    assert main([*S, "core"]) == 0
    assert main([*S, "deploy", "here", "--skip-verify"]) == 2
    err = capsys.readouterr().err
    assert f"{tmp_path}/www/yourls-config/t.php" in err
    assert "ym target config here" in err
    assert main([*S, "deploy", "nope"]) == 2


@pytest.mark.parametrize("argv", [["--version"]])
def test_version(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_site_toml_is_usage_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["build"]) == 2
