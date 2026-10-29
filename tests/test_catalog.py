"""`ym plugin search`, `ym plugin add NAME` (design §11.1) and `ym doctor` (§11.3)."""

from __future__ import annotations

import json
import os
import time

import pytest

from yourls_manager import catalog
from yourls_manager.cli import main
from yourls_manager.plugincmd import read_header
from yourls_manager.util import UsageError

from .conftest import git, write_files

CATALOG = """
categories:
  - slug: redirection
    label: Redirection
  - slug: admin
    label: Admin interface
plugins:
  - name: 302-Instead
    url: https://github.com/EpicPilgrim/302-instead
    description: Send a 302 redirect instead of 301.
    category: redirection
    tags: ["302", temporary, status code]
  - name: Always-302
    url: https://github.com/tinjaw/Always-302
    description: A newer alternative to 302-Instead.
    category: redirection
  - name: Timezones
    url: https://github.com/YOURLS/timezones
    description: Tell YOURLS your time zone.
    category: admin
    tags: [date, time]
    official: true
  - name: Blog Plugin
    url: https://yourls.org/blog/2011/04/some-plugin
    description: Only a blog post.
    category: admin
  - name: broken entry without url
"""


def test_parse_search_find():
    cat = catalog.parse_catalog(CATALOG.encode())
    assert [e.name for e in cat.entries] == ["302-Instead", "Always-302", "Timezones",
                                             "Blog Plugin"]  # fmt: skip
    assert cat.categories["admin"] == "Admin interface"
    tz = catalog.find(cat, "Timezones")
    assert tz.official and tz.tags == ("date", "time")
    # name beats description
    assert [e.name for e in catalog.search(cat, "302")][:2] == ["302-Instead", "Always-302"]
    assert [e.name for e in catalog.search(cat, "temporary")] == ["302-Instead"]
    assert [e.name for e in catalog.search(cat, "time zone")] == ["Timezones"]
    assert catalog.search(cat, "302 nonexistent") == []
    assert [e.name for e in catalog.search(cat, "", "admin")] == ["Blog Plugin", "Timezones"]
    with pytest.raises(UsageError, match="unknown category"):
        catalog.search(cat, "x", "nope")
    with pytest.raises(UsageError, match="QUERY"):
        catalog.search(cat, "")
    assert catalog.find(cat, "timezones").name == "Timezones"
    assert catalog.find(cat, "time").name == "Timezones"  # unique prefix
    with pytest.raises(UsageError, match="several"):
        catalog.find(cat, "")
    with pytest.raises(UsageError, match="no plugin named"):
        catalog.find(cat, "zzz")


def test_repo_key():
    assert catalog.repo_key("https://github.com/YOURLS/Timezones") == (
        "https://github.com/yourls/timezones", ".")  # fmt: skip
    assert catalog.repo_key("https://github.com/o/r/tree/master/sub") == (
        "https://github.com/o/r", "sub")  # fmt: skip
    assert catalog.repo_key("git@github.com:o/r.git") == ("https://github.com/o/r", ".")


def test_read_header(tmp_path):
    php = tmp_path / "plugin.php"
    php.write_text(
        "<?php\n/*\nPlugin Name: Time Zones\nPlugin URI: https://example.com/x\n"
        "Description: Tell YOURLS: your zone\nVersion: 2.1\nRequires YOURLS: 1.9\n*/\n"
    )
    h = read_header(php)
    assert h["Plugin Name"] == "Time Zones"
    assert h["Plugin URI"] == "https://example.com/x"
    assert h["Description"] == "Tell YOURLS: your zone"
    assert h["Requires YOURLS"] == "1.9"


@pytest.fixture
def catalog_env(tmp_path, monkeypatch):
    """Serve a catalog from a file:// URL and isolate the cache."""
    src = tmp_path / "catalog.yml"
    src.write_text(CATALOG)
    monkeypatch.setenv("YM_CATALOG_URL", src.as_uri())
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return src


def test_catalog_cache(catalog_env, tmp_path, capsys):
    cache = tmp_path / "cache/yourls-manager/plugins.yml"
    catalog.load_catalog()
    assert cache.is_file()
    catalog_env.write_text(CATALOG.replace("Timezones", "Zones"))
    assert "Timezones" in [e.name for e in catalog.load_catalog().entries]  # fresh cache
    assert "Zones" in [e.name for e in catalog.load_catalog(refresh=True).entries]
    catalog_env.unlink()
    old = time.time() - 2 * catalog.MAX_AGE
    os.utime(cache, (old, old))
    assert "Zones" in [e.name for e in catalog.load_catalog().entries]  # stale beats nothing
    assert "using the cached copy" in capsys.readouterr().err


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
                [deploy.targets.local]
                path = "{tmp_path}/www"
                """,
            ".gitignore": "dist/\n",
        },
    )
    git(site, "init", "-q", "-b", "main")
    assert main(["--site-dir", str(site), "core"]) == 0
    git(site, "add", "-A")
    git(site, "commit", "-q", "-m", "init")
    return site


def test_plugin_search_cli(site, catalog_env, capsys):
    S = ["--site-dir", str(site)]
    toml = site / "site.toml"
    toml.write_text(
        toml.read_text()
        + '\n[[plugins]]\nname = "tz"\nrepo = "https://github.com/YOURLS/timezones.git"\n'
    )
    assert main([*S, "plugin", "search", "time"]) == 0
    out = capsys.readouterr().out
    assert "Timezones  [admin]  (official)  * in site.toml as tz" in out
    assert main([*S, "plugin", "search", "--category", "redirection", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["name"] for r in rows] == ["302-Instead", "Always-302"]
    assert rows[0]["in_site"] is None
    assert main([*S, "plugin", "search"]) == 2


def test_plugin_add_by_name(site, upstream, catalog_env, capsys):
    S = ["--site-dir", str(site)]
    plug = upstream("tz")
    plug.commit({"timezones/plugin.php": "<?php\n/*\nPlugin Name: Timezones\nVersion: 2.0\n*/\n",
                 "README.md": "x"})  # fmt: skip
    catalog_env.write_text(CATALOG.replace("https://github.com/YOURLS/timezones", plug.url))
    assert main([*S, "plugin", "add", "timezones", "--no-sync"]) == 0
    out = capsys.readouterr().out
    assert "tz: added Timezones 2.0" not in out and "timezones: added Timezones 2.0" in out
    assert "ym plugin sync timezones" in out
    text = (site / "site.toml").read_text()
    assert f'name   = "timezones"\nrepo   = "{plug.url}"\npath   = "timezones"\n' in text
    assert not (site / "yourls/user/plugins/timezones").exists()
    assert main([*S, "plugin", "add", "Timezones"]) == 2  # same repo + path
    assert "already in site.toml" in capsys.readouterr().err
    assert main([*S, "plugin", "sync"]) == 0
    assert (site / "yourls/user/plugins/timezones/plugin.php").is_file()
    # a catalog link that isn't a git repo
    assert main([*S, "plugin", "add", "Blog Plugin"]) == 2
    assert "isn't a git repository" in capsys.readouterr().err
    # directory names that would clobber core's bundled plugins are refused
    other = upstream("other")
    other.commit({"plugin.php": "<?php // x\n"})
    assert main([*S, "plugin", "add", other.url, "--name", "sample-plugin"]) == 2
    assert "bundled with core" in capsys.readouterr().err


# --------------------------------------------------------------------------- doctor


def test_doctor(site, tmp_path, monkeypatch, capsys):
    import yourls_manager.deploy.sftp as sftp_mod

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    states = iter([("install", "x"), ("ok", None)])
    monkeypatch.setattr(sftp_mod, "yourls_state", lambda url: next(states))
    php_ok = {"v": False}

    def php_version(self):
        if not php_ok["v"]:
            from yourls_manager.util import YMError

            raise YMError("command not found: php")
        return (8, 3, 1)

    monkeypatch.setattr("yourls_manager.deploy.rsync.RsyncTarget.php_version", php_version)
    S = ["--site-dir", str(site)]
    assert main([*S, "doctor"]) == 1
    out = capsys.readouterr().out
    assert "ok    lock: YOURLS 1.0.0 @ " in out and "0 vendored plugin(s), no drift" in out
    assert "ok    working tree: clean" in out
    assert "ok    remote: none (local only)" in out
    assert "ok    login: local target (no host)" in out
    assert "FAIL  php: command not found: php" in out
    assert "FAIL  server config" in out and "ym target config local" in out
    assert "warn  http: install pending" in out
    assert "warn  deployed: nothing deployed yet" in out

    php_ok["v"] = True
    home = tmp_path / "home"
    (tmp_path / "yourls-config").mkdir()  # default: sibling of the web root
    (tmp_path / "yourls-config/t.php").write_text("<?php\n")
    state = home / ".yourls-manager/t"
    state.mkdir(parents=True)
    head = git(site, "rev-parse", "HEAD")
    release = {"git_commit": head, "git_dirty": False, "deployed_at": "2026-10-28T00:00:00Z"}
    (state / "release.json").write_text(json.dumps(release))
    (site / "new.txt").write_text("x")
    assert main([*S, "doctor", "local"]) == 0
    out = capsys.readouterr().out
    assert "ok    php: PHP 8.3.1" in out
    assert "warn  working tree: 1 uncommitted change(s)" in out
    assert f"ok    deployed: {head[:7]} at 2026-10-28T00:00:00Z = HEAD" in out
    assert "doctor: " in out and "0 failed" in out
    assert main([*S, "doctor", "nope"]) == 2


def test_doctor_outside_site(tmp_path, capsys):
    assert main(["--site-dir", str(tmp_path), "doctor"]) == 1
    assert "FAIL  site.toml" in capsys.readouterr().out


def test_plugin_list_json_check(site, upstream, capsys):
    S = ["--site-dir", str(site)]
    plug = upstream("p")
    plug.commit({"plugin.php": "<?php\n/*\nPlugin Name: P\nVersion: 1.0\n*/\n"})
    toml = site / "site.toml"
    toml.write_text(
        toml.read_text() + f'\n[[plugins]]\nname = "p"\nrepo = "{plug.url}"\n\n'
        '[[plugins]]\nname = "sample-plugin"\nactive = false\n'
    )
    assert main([*S, "plugin", "sync"]) == 0
    capsys.readouterr()
    assert main([*S, "plugin", "list", "--check"]) == 0
    out = capsys.readouterr().out
    assert "version" in out and "1.0" in out and "up to date" in out
    assert "all plugins up to date" in out
    new = plug.commit({"plugin.php": "<?php\n/*\nPlugin Name: P\nVersion: 1.1\n*/\n"})
    assert main([*S, "plugin", "list", "--check", "--json"]) == 10
    rows = {r["name"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["p"]["version"] == "1.0" and rows["p"]["latest_commit"] == new
    assert rows["p"]["update_available"] is True
    assert rows["sample-plugin"]["source"] == "core"
    assert rows["sample-plugin"]["update_available"] is None
    assert main([*S, "plugin", "list"]) == 0  # offline by default
    assert "-> " not in capsys.readouterr().out
