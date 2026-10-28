"""`ym adopt` (design §9)."""

from __future__ import annotations

import json
import stat
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest

from yourls_manager import adopt as adopt_mod
from yourls_manager.cli import main
from yourls_manager.config import load_site

from .conftest import (
    YOURLS_CORE_URL,
    core_files,
    git,
    git_insteadof,
    needs_copier,
    needs_rsync,
    new_site_args,
    write_files,
)

# --------------------------------------------------------------------------- units


def test_rsync_excludes():
    pats = [".well-known/**", "**/error_log", "**/*.log", ".git/**", "cache/tmp", "**/a/b"]
    assert adopt_mod.rsync_excludes(pats) == [
        "/.well-known",
        "error_log",
        "*.log",
        "/.git",
        "/cache/tmp",
        "**/a/b",
    ]


def test_ignore_matching():
    ign = adopt_mod.Ignore(adopt_mod.DEFAULT_IGNORES)
    assert ign("error_log") and ign("admin/error_log") and ign("x/y.log")
    assert ign(".well-known") and ign(".well-known/acme/x")
    assert ign("a/.DS_Store") and not ign(".htaccess") and not ign("admin/index.php")
    assert ign.under("user/plugins/p")("error_log")


def test_plugin_header_uri(tmp_path):
    p = tmp_path / "plugin.php"
    p.write_text("<?php\n/*\nPlugin Name: X\nPlugin URI: https://example.org/x \n*/\n")
    assert adopt_mod.plugin_header_uri(p) == "https://example.org/x"
    p.write_text("<?php\n/**\n * Plugin URI:   http://y\n */")
    assert adopt_mod.plugin_header_uri(p) == "http://y"
    p.write_text("<?php // nothing")
    assert adopt_mod.plugin_header_uri(p) is None


SITE_TOML = """\
# header comment
[site]
name = "T"
slug = "t"
url  = "https://go.example.com"

[core]
repo    = "{core}"
version = "{version}"   # keep this comment
exclude = [".github", ".git*", "tests", "*.md", "phpunit.xml.dist"]

# Site logo
[[plugins]]
name   = "site-branding"
active = true

# Example (uncomment to use):
#
# [[plugins]]
# name   = "404-if-not-found"

[build]
out = "dist"

# Deploy targets
{targets}
"""


def test_append_plugin_blocks_keeps_comments(tmp_path):
    f = tmp_path / "site.toml"
    f.write_text(SITE_TOML.format(core="x", version="1.0.0", targets=""))
    d = adopt_mod.PluginDecision("custom", "custom", None, "overlay", True, "https://e/x")
    b = adopt_mod.PluginDecision("hyphens-in-urls", "bundled", True, "none", True)
    adopt_mod.append_plugin_blocks(f, [adopt_mod.plugin_block(d), adopt_mod.plugin_block(b)])
    text = f.read_text()
    data = tomllib.loads(text)
    assert [p["name"] for p in data["plugins"]] == ["site-branding", "custom", "hyphens-in-urls"]
    assert data["plugins"][1]["active"] is True and "repo" not in data["plugins"][1]
    assert "# keep this comment" in text and "# header comment" in text
    assert text.index("# Plugin URI: https://e/x") < text.index("[build]")
    assert text.index('name   = "404-if-not-found"') < text.index('name   = "custom"')
    assert "active = true   # TODO verify" in text
    assert text.count("TODO verify") == 1


def test_append_plugin_blocks_no_plugins_table(tmp_path):
    f = tmp_path / "site.toml"
    f.write_text('[site]\nname="x"\n[core]\nversion="1"')
    d = adopt_mod.PluginDecision("p", "custom", False, "overlay", True)
    adopt_mod.append_plugin_blocks(f, [adopt_mod.plugin_block(d)])
    assert tomllib.loads(f.read_text())["plugins"] == [{"name": "p", "active": False}]


# --------------------------------------------------------------------------- fixtures

REAL_CONFIG = """\
<?php
define( 'YOURLS_DB_USER', 'olduser' );
define( 'YOURLS_DB_PASS', 'secret' );
define( 'YOURLS_DB_NAME', 'yourls' );
define( 'YOURLS_DB_HOST', 'localhost' );
define( 'YOURLS_SITE', 'https://go.example.com' );
$yourls_user_passwords = [ 'bob' => 'phpass:!2y!10!x' ];
"""

CUSTOM_PLUGIN = """\
<?php
/*
Plugin Name: Custom Plug
Plugin URI: https://github.com/someone/custom-plug
*/
"""


def make_install(root: Path, version: str = "1.0.0") -> None:
    """An unmanaged install as uploaded from a release zip, then hand-edited."""
    files: dict[str, str | bytes] = {
        rel: content
        for rel, content in core_files(version).items()
        if not rel.startswith((".github/", "tests/", ".gitattributes"))
    }
    del files["includes/vendor/lib/README.md"]  # upstream file missing from the install
    files.update(
        {
            "admin/index.php": "<?php // admin, hand-patched\n",
            "custom.php": "<?php echo 'custom page';\n",
            ".htaccess": "# BEGIN YOURLS\nRewriteRule ^custom$ custom.php [L]\n# END YOURLS\n",
            "user/config.php": REAL_CONFIG,
            "user/plugins/custom-plug/plugin.php": CUSTOM_PLUGIN,
            "user/plugins/custom-plug/inc/helper.php": "<?php // helper\n",
            "user/plugins/custom-plug/error_log": "PHP Notice: noise\n",
            "error_log": "PHP Warning: noise\n",
            "admin/error_log": "PHP Warning: noise\n",
            ".well-known/acme-challenge/token": "abc",
            "stats.log": "noise",
        }
    )
    write_files(root, files)


TEMPLATE_OVERLAY = {
    "overlay/.htaccess": "# template htaccess\nRewriteRule ^.*$ /yourls-loader.php [L]\n",
    "overlay/health.php": "<?php echo '{\"ok\":true}';\n",
    "overlay/user/config.php": (
        "<?php // shim: getenv('YOURLS_CONFIG_FILE'); server-local configuration missing\n"
    ),
    "overlay/user/plugins/site-branding/plugin.php": "<?php /* Plugin Name: Site Branding */\n",
    "overlay/user/plugins/site-branding/assets/brand.css": "body{}\n",
    ".gitignore": "/dist/\n",
}


def make_site(tmp_path: Path, core_url: str, targets: str = "", version: str = "1.1.0") -> Path:
    site = tmp_path / "site"
    write_files(
        site,
        {"site.toml": SITE_TOML.format(core=core_url, version=version, targets=targets)},
    )
    write_files(site, TEMPLATE_OVERLAY)
    assert main(["--site-dir", str(site), "core"]) == 0
    git(site, "init", "-q", "-b", "main")
    git(site, "add", "-A")
    git(site, "commit", "-q", "-m", "new site")
    return site


def check_adopted(site: Path, res, *, todo: bool) -> None:
    ov = site / "overlay"
    # core-file differences -> overlay
    assert (ov / "admin/index.php").read_text() == "<?php // admin, hand-patched\n"
    assert (ov / "custom.php").read_text() == "<?php echo 'custom page';\n"
    # collisions with template-owned overlay files -> adopt-review
    assert (site / "adopt-review/.htaccess").read_text().startswith("# BEGIN YOURLS")
    assert (ov / ".htaccess").read_text().startswith("# template htaccess")
    # custom plugin -> overlay (without ignored noise); bundled plugins are not copied
    assert (ov / "user/plugins/custom-plug/plugin.php").read_text() == CUSTOM_PLUGIN
    assert (ov / "user/plugins/custom-plug/inc/helper.php").exists()
    assert not (ov / "user/plugins/custom-plug/error_log").exists()
    assert not (ov / "user/plugins/hyphens-in-urls").exists()
    # ignored files are never adopted; config never copied into the repo
    for rel in ("error_log", "admin/error_log", "stats.log", ".well-known"):
        assert not (ov / rel).exists() and not (site / "adopt-review" / rel).exists()
    assert "secret" not in (ov / "user/config.php").read_text()
    assert not (site / "adopt-review/user/config.php").exists()

    # site.toml: version + plugin entries, comments preserved
    text = (site / "site.toml").read_text()
    data = tomllib.loads(text)
    assert data["core"]["version"] == "1.0.0"
    assert "# keep this comment" in text
    names = {p["name"]: p for p in data["plugins"]}
    assert "repo" not in names["custom-plug"]
    assert names["custom-plug"]["active"] is True
    assert "# Plugin URI: https://github.com/someone/custom-plug" in text
    assert ("TODO verify" in text) is todo
    lock = json.loads((site / "yourls.lock.json").read_text())
    assert lock["core"]["version"] == "1.0.0"
    assert "'1.0.0'" in (site / "yourls/includes/version.php").read_text()

    # parity: only template-owned / excluded / restored differences
    par = res.parity
    assert par.unexpected_count == 0, par.unexpected()
    assert par.change == {".htaccess": "template"}
    assert par.delete == {"CHANGELOG.md": "excluded", "README.md": "excluded"}
    assert par.add["includes/vendor/lib/README.md"] == "restored"
    assert par.add["health.php"] == "template"

    # report
    report = (site / "adopt-review/REPORT.md").read_text()
    assert "YOURLS version: **1.0.0**" in report
    assert "`custom.php`" in report and "`admin/index.php`" in report
    assert "`.htaccess`" in report
    assert "| `custom-plug` | custom |" in report
    assert "https://github.com/someone/custom-plug" in report
    assert "unexpected (*other*): **0**" in report
    assert "`includes/vendor/lib/README.md`" in report  # missing upstream file


# --------------------------------------------------------------------------- integration


def test_adopt_from_dir(tmp_path, fake_core, capsys):
    site = make_site(tmp_path, fake_core.url)
    install = tmp_path / "install"
    make_install(install)
    before = {p: p.read_bytes() for p in install.rglob("*") if p.is_file()}

    # dry run writes nothing
    S = ["--site-dir", str(site)]
    assert main([*S, "adopt", "--from-dir", str(install), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "custom.php" in out and "dry run: nothing written" in out
    assert git(site, "status", "--porcelain") == ""

    res = adopt_mod.adopt(load_site(site), from_dir=install)
    check_adopted(site, res, todo=True)
    out, err = capsys.readouterr()
    assert "TODO verify" in err and "could not read the active plugin set" in err
    assert "ym server-config TARGET --from-existing" in out
    # bundled plugins are not added without a known active state
    names = [p["name"] for p in tomllib.loads((site / "site.toml").read_text())["plugins"]]
    assert names == ["site-branding", "custom-plug"]
    # the install was not touched
    assert {p: p.read_bytes() for p in install.rglob("*") if p.is_file()} == before
    # adopt result builds cleanly and is idempotent-ish: a second run adds nothing new
    git(site, "add", "-A")
    git(site, "commit", "-q", "-m", "adopt")
    res2 = adopt_mod.adopt(load_site(site), from_dir=install)
    assert res2.toml_added == [] and res2.plan.overlay_files == []
    assert res2.parity.unexpected_count == 0


def test_adopt_unknown_version(tmp_path, fake_core, capsys):
    site = make_site(tmp_path, fake_core.url)
    install = tmp_path / "install"
    make_install(install, version="1.0.9")
    assert main(["--site-dir", str(site), "adopt", "--from-dir", str(install)]) == 1
    err = capsys.readouterr().err
    assert "'1.0.9' has no matching tag" in err and "nearby tags: 1.0.0, 1.1.0" in err


def test_adopt_args(tmp_path, capsys):
    with pytest.raises(SystemExit):
        main(["adopt"])
    with pytest.raises(SystemExit):
        main(["adopt", "--from-dir", "x", "--from-target", "y"])
    assert main(["adopt", "--from-dir", str(tmp_path)]) == 2  # no site.toml


FAKE_PHP = r"""
import json, os, sys
args = sys.argv[1:]
if args[:1] == ["-r"]:
    sys.stdout.write("8.3.0"); sys.exit(0)
helper, cmd, *rest = args
assert os.path.isfile(helper), helper
with open(os.environ["FAKE_PHP_LOG"], "a") as fh:
    fh.write(json.dumps({"cmd": cmd, "args": rest,
                         "config_env": os.environ.get("YOURLS_CONFIG_FILE")}) + "\n")
print(json.dumps({"ok": True, "installed": True, "yourls_version": "1.0.0",
                  "active_plugins": ["custom-plug", "hyphens-in-urls", "gone-plugin"]}))
"""


@needs_rsync
def test_adopt_from_target(tmp_path, fake_core, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    log = tmp_path / "php.log"
    monkeypatch.setenv("FAKE_PHP_LOG", str(log))
    php = tmp_path / "fake-php"
    php.write_text(f"#!{sys.executable}\n{FAKE_PHP}")
    php.chmod(php.stat().st_mode | stat.S_IXUSR)
    web = tmp_path / "www" / "go.example.com"
    make_install(web)
    (web / "private").mkdir()
    (web / "private/big.bin").write_bytes(b"x" * 10)
    targets = textwrap.dedent(
        f"""\
        [deploy.targets.prod]
        path    = "{web}"
        php     = "{php}"
        protect = [".well-known/", "private/"]
        """
    )
    site = make_site(tmp_path, fake_core.url, targets)
    S = ["--site-dir", str(site)]

    assert main([*S, "adopt", "--from-target", "prod", "--dry-run"]) == 0
    assert not log.exists()  # dry run: helper not uploaded or run
    assert git(site, "status", "--porcelain") == ""

    res = adopt_mod.adopt(load_site(site), from_target="prod")
    check_adopted(site, res, todo=False)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert [c["cmd"] for c in calls] == ["info"]
    assert calls[0]["args"] == [f"--root={web}"]
    assert calls[0]["config_env"] == f"{web}/user/config.php"  # config_file missing
    data = tomllib.loads((site / "site.toml").read_text())
    names = {p["name"]: p["active"] for p in data["plugins"]}
    assert names == {"site-branding": True, "custom-plug": True, "hyphens-in-urls": True}
    assert not (site / "overlay/private").exists()  # protected paths are ignored
    err = capsys.readouterr().err
    assert "missing on disk: gone-plugin" in err
    # the server's web root is untouched
    assert (web / "error_log").exists() and (web / "user/config.php").read_text() == REAL_CONFIG


@needs_copier
def test_adopt_into_template_site(tmp_path, fake_core, no_uv_lock, capfd):
    """`ym new` from the real template, then adopt: only template files differ."""
    git_insteadof(YOURLS_CORE_URL, fake_core.url)
    site = tmp_path / "go-example-com"
    args = new_site_args(site, "yourls_version=1.1.0", "include_example_plugins=false")
    assert main(args) == 0
    install = tmp_path / "install"
    make_install(install)
    res = adopt_mod.adopt(load_site(site), from_dir=install)
    par = res.parity
    assert par.unexpected_count == 0, par.unexpected()
    assert par.change == {".htaccess": "template"}
    assert (site / "adopt-review/.htaccess").exists()
    assert (site / "overlay/custom.php").exists()
    data = tomllib.loads((site / "site.toml").read_text())
    assert data["core"]["version"] == "1.0.0"
    assert [p["name"] for p in data["plugins"]] == ["site-branding", "custom-plug"]
    # template-owned overlay files show up as expected adds
    assert par.add["user/plugins/site-branding/plugin.php"] == "template"
    assert main(["--site-dir", str(site), "build"]) == 0
