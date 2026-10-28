from __future__ import annotations

import textwrap
import tomllib
from pathlib import Path

import pytest

from yourls_manager.config import (
    ConfigError,
    find_site_dir,
    load_site,
    normalize_remote_path,
    parse_site,
    set_core_version,
)
from yourls_manager.util import UsageError

BASE = """
[site]
name = "Example Go Links"
slug = "go-example-com"
url  = "https://go.example.com"

[core]
version = "1.10.6"
exclude = [".github", ".git*", "tests", "*.md"]

[[plugins]]
name = "keep-query-string"
repo = "https://github.com/rinogo/yourls-keep-query-string"

[[plugins]]
name = "hyphens-in-urls"
active = true

[deploy.targets.prod]
type = "rsync"
host = "example.dreamhost.com"
user = "ruben"
path = "go.example.com"
php  = "/usr/local/php83/bin/php"
protect = [".well-known/"]
"""


def parse(text: str, root: Path = Path("/site")):
    return parse_site(tomllib.loads(textwrap.dedent(text)), root)


def test_defaults_and_derived_values():
    cfg = parse(BASE)
    assert cfg.site.slug == "go-example-com"
    assert cfg.core.repo == "https://github.com/YOURLS/YOURLS.git"
    kqs = cfg.plugin("keep-query-string")
    assert kqs and kqs.managed and kqs.ref == "HEAD" and kqs.path == "." and kqs.active
    hyph = cfg.plugin("hyphens-in-urls")
    assert hyph and not hyph.managed
    assert [p.name for p in cfg.managed_plugins] == ["keep-query-string"]
    assert cfg.active_plugins == ["keep-query-string", "hyphens-in-urls"]
    assert cfg.build.out == "dist"
    t = cfg.target("prod")
    assert t.port == 22 and t.backup_keep == 10
    assert t.config_file == "yourls-config/go-example-com.php"
    assert t.backup_dir == "yourls-backups/go-example-com"
    assert t.url == "https://go.example.com"
    assert t.protect == (".well-known/",)
    assert not t.is_local
    assert cfg.warnings == []


def test_absolute_path_defaults_and_local_target():
    cfg = parse(
        BASE.replace('host = "example.dreamhost.com"\n', "").replace(
            'path = "go.example.com"', 'path = "/var/www/go/"'
        )
    )
    t = cfg.target("prod")
    assert t.is_local
    assert t.path == "/var/www/go"
    assert t.config_file == "/var/www/yourls-config/go-example-com.php"
    assert t.backup_dir == "/var/www/yourls-backups/go-example-com"


def test_tilde_paths_become_home_relative():
    assert normalize_remote_path("~/sites/go/") == "sites/go"
    assert normalize_remote_path("~") == "."
    assert normalize_remote_path("/abs/x/") == "/abs/x"


@pytest.mark.parametrize(
    ("old", "new", "key"),
    [
        ('slug = "go-example-com"', 'slug = "Go_Example"', "site.slug"),
        ('url  = "https://go.example.com"', 'url = "https://go.example.com/"', "site.url"),
        ('url  = "https://go.example.com"', 'url = "go.example.com"', "site.url"),
        ('version = "1.10.6"', 'version = "latest"', "core.version"),
        ('version = "1.10.6"', "version = 1.10", "core.version"),
        ('type = "rsync"', 'type = "ftp"', "deploy.targets.prod.type"),
        ('user = "ruben"', 'user = "ruben"\nport = 70000', "deploy.targets.prod.port"),
        ('user = "ruben"', 'user = "ruben"\nport = "22"', "deploy.targets.prod.port"),
        ('path = "go.example.com"', 'path = "my site"', "deploy.targets.prod.path"),
        ('path = "go.example.com"', 'path = "~"', "deploy.targets.prod.path"),
        ('name = "hyphens-in-urls"', 'name = "Keep-Query-String"', "plugins[1].name"),
        ('name = "hyphens-in-urls"', 'name = "../evil"', "plugins[1].name"),
        ("active = true", 'active = "yes"', "plugins[1].active"),
    ],
)
def test_validation_errors_name_the_key(old, new, key):
    assert old in BASE
    with pytest.raises(ConfigError) as exc:
        parse(BASE.replace(old, new, 1))
    assert exc.value.key == key
    assert key in str(exc.value)
    assert exc.value.exit_code == 2


def test_missing_required_key():
    with pytest.raises(ConfigError, match=r"site\.name: is required"):
        parse(BASE.replace('name = "Example Go Links"\n', ""))


def test_plugin_path_must_stay_inside_repo():
    text = BASE.replace(
        'repo = "https://github.com/rinogo/yourls-keep-query-string"',
        'repo = "https://example.invalid/x.git"\npath = "../outside"',
    )
    with pytest.raises(ConfigError, match=r"plugins\[0\]\.path"):
        parse(text)


def test_unknown_keys_warn():
    cfg = parse(BASE + "\n[build]\nout = 'dist'\nbogus = 1\n")
    assert any("build.bogus" in w for w in cfg.warnings)
    cfg2 = parse(BASE.replace("active = true", 'active = true\nref = "main"'))
    assert any("no repo" in w for w in cfg2.warnings)


def test_build_out_cannot_be_vendor_dir():
    with pytest.raises(ConfigError, match=r"build\.out"):
        parse(BASE + '\n[build]\nout = "yourls"\n')


def test_load_site_and_find_upward(tmp_path, capsys):
    (tmp_path / "site.toml").write_text("surprise = 1\n" + BASE)
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert find_site_dir(nested) == tmp_path.resolve()
    cfg = load_site(tmp_path)
    assert cfg.root == tmp_path.resolve()
    assert "unknown key 'surprise'" in capsys.readouterr().err


def test_find_site_dir_missing(tmp_path):
    with pytest.raises(UsageError):
        find_site_dir(tmp_path)


def test_invalid_toml(tmp_path):
    (tmp_path / "site.toml").write_text("[site\n")
    with pytest.raises(UsageError, match="invalid TOML"):
        load_site(tmp_path)


def test_set_core_version_preserves_formatting(tmp_path):
    f = tmp_path / "site.toml"
    f.write_text(BASE.replace('version = "1.10.6"', 'version   = "1.10.6"   # pinned'))
    assert set_core_version(f, "1.10.7") is True
    text = f.read_text()
    assert 'version   = "1.10.7"   # pinned' in text
    assert text.count("1.10.7") == 1
    assert set_core_version(f, "1.10.7") is False
    # other tables' "version" keys are untouched
    f.write_text("[site]\nversion = \"x\"\n[core]\nversion = '1.0.0'\n")
    set_core_version(f, "1.1.0")
    assert f.read_text() == "[site]\nversion = \"x\"\n[core]\nversion = '1.1.0'\n"
