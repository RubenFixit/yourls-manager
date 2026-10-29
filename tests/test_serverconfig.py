"""`ym target config` (design §9)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from yourls_manager import serverconfig as sc
from yourls_manager.cli import main
from yourls_manager.util import YMError

from .conftest import TEMPLATE_DIR, needs_copier, needs_php, write_files

# Same define()/passwords layout as the template's server/config.example.php.
EXAMPLE = """\
<?php
/** MySQL database username */
define( 'YOURLS_DB_USER', 'CHANGE-ME' );
define( 'YOURLS_DB_PASS', 'CHANGE-ME' );
define( 'YOURLS_DB_NAME', 'CHANGE-ME' );
define( 'YOURLS_DB_HOST', 'localhost' );
define( 'YOURLS_DB_PREFIX', 'yourls_' );
define( 'YOURLS_SITE', 'https://go.example.com' );
define( 'YOURLS_PRIVATE', true );
define( 'YOURLS_COOKIEKEY', 'CHANGE-ME' );
$yourls_user_passwords = [
    'CHANGE-ME-username' => 'phpass:CHANGE-ME',
    // 'username2' => 'phpass:...',
];
define( 'YOURLS_URL_CONVERT', 36 );
"""

SECRET_DB = "db'pa\\ss$word"
SECRET_ADMIN = "Adm1n 'secret' $x"


def render(**kw):
    args = dict(
        db_user="u",
        db_pass="p",
        db_name="n",
        db_host="localhost",
        db_prefix="yourls_",
        site_url="https://s.example",
        cookie_key="k" * 64,
        users={"admin": "phpass:!2y!10!abc"},
    )
    args.update(kw)
    return sc.render_config(EXAMPLE, **args)


def test_render_config_fills_everything():
    text = render(db_pass=SECRET_DB, db_host="mysql.example.com:3307", db_prefix="go_")
    assert "define( 'YOURLS_DB_PASS', 'db\\'pa\\\\ss$word' );" in text
    assert "define( 'YOURLS_DB_HOST', 'mysql.example.com:3307' );" in text
    assert "define( 'YOURLS_DB_PREFIX', 'go_' );" in text
    assert "define( 'YOURLS_SITE', 'https://s.example' );" in text
    assert "define( 'YOURLS_COOKIEKEY', '" + "k" * 64 + "' );" in text
    assert "$yourls_user_passwords = [\n    'admin' => 'phpass:!2y!10!abc',\n];" in text
    assert "CHANGE-ME" not in text.replace("// 'username2'", "")
    assert "define( 'YOURLS_PRIVATE', true );" in text  # untouched
    assert text.endswith("define( 'YOURLS_URL_CONVERT', 36 );\n")


@needs_php
def test_rendered_config_is_valid_php(tmp_path):
    f = tmp_path / "c.php"
    f.write_text(render(db_pass=SECRET_DB))
    code = "require $argv[1]; echo YOURLS_DB_PASS;"
    out = subprocess.run(["php", "-r", code, str(f)], capture_output=True, text=True, check=True)
    assert out.stdout == SECRET_DB


def test_render_config_missing_placeholder_fails_loudly():
    with pytest.raises(YMError, match="YOURLS_COOKIEKEY"):
        sc.render_config(EXAMPLE.replace("YOURLS_COOKIEKEY", "X"), **_kw())
    with pytest.raises(YMError, match="yourls_user_passwords"):
        sc.render_config(EXAMPLE.replace("$yourls_user_passwords", "$users"), **_kw())
    with pytest.raises(YMError, match="placeholder"):
        sc.render_config(EXAMPLE + "define( 'X', 'CHANGE-ME' );\n", **_kw())


def _kw():
    return dict(
        db_user="u", db_pass="p", db_name="n", db_host="h", db_prefix="y_",
        site_url="https://s", cookie_key="k", users={"a": "phpass:x"},
    )  # fmt: skip


def test_array_syntax_and_helpers():
    old = "<?php\n$yourls_user_passwords = array(\n  'bob' => 'plain',\n  'eve' => 'md5:1:2',\n);\n"
    assert sc.plaintext_users(old) == ["bob"]
    new = sc.set_user_passwords(old, {"amy": "phpass:!h"})
    assert "array" not in new and "'amy' => 'phpass:!h'," in new
    with pytest.raises(Exception, match="newlines"):
        sc.php_string("a\nb")
    assert sc.is_shim("getenv('YOURLS_CONFIG_FILE') ... server-local configuration missing")
    assert not sc.is_shim("define('YOURLS_DB_USER', 'x');")


@needs_copier
def test_template_example_matches_parser(tmp_path):
    """The real template's example file has every line server-config replaces."""
    dest = tmp_path / "s"
    subprocess.run(
        ["uvx", "copier", "copy", "--quiet", "--vcs-ref", "HEAD", "--defaults",
         "--data", "site_name=T", "--data", "site_url=https://go.example.com",
         str(TEMPLATE_DIR), str(dest)],
        check=True, capture_output=True,
    )  # fmt: skip
    text = (dest / "server/config.example.php").read_text()
    out = sc.render_config(text, **_kw())
    assert "'CHANGE-ME" not in out and "CHANGE-ME'" not in out
    assert sc.example_value(text, "YOURLS_DB_HOST") == "localhost"
    assert sc.example_value(text, "YOURLS_DB_PREFIX") == "yourls_"


# --------------------------------------------------------------------------- CLI


def make_site(tmp_path: Path, php: str = "php") -> tuple[Path, Path]:
    site = tmp_path / "site"
    web = tmp_path / "www"
    write_files(
        site,
        {
            "site.toml": textwrap.dedent(
                f"""\
                [site]
                name = "T"
                slug = "t"
                url = "https://go.example.com"
                [core]
                version = "1.10.6"
                [deploy.targets.local]
                path = "{web}/site"
                php = "{php}"
                url = "http://127.0.0.1:8099"
                """
            ),
            "server/config.example.php": EXAMPLE,
        },
    )
    return site, web


def yourls_password_ok(config: Path, user: str, password: str) -> bool:
    """Check ``password`` against the stored hash the way YOURLS does.

    Uses YOURLS' own includes/functions-auth.php when YM_TEST_YOURLS_SRC points at a YOURLS
    checkout; otherwise a line-for-line replica of yourls_check_password_hash()'s phpass path.
    """
    src = os.environ.get("YM_TEST_YOURLS_SRC")
    if src:
        code = (
            "require $argv[1]; require $argv[3] . '/includes/functions-auth.php';"
            " echo yourls_check_password_hash($argv[2], stream_get_contents(STDIN)) ? 'OK' : 'NO';"
        )
        argv = ["php", "-r", code, str(config), user, src]
    else:
        code = (
            "require $argv[1]; $s = $yourls_user_passwords[$argv[2]];"
            " if (substr($s, 0, 7) !== 'phpass:') { echo 'NO'; exit; }"
            " list(, $h) = explode(':', $s); $h = str_replace('!', '$', $h);"
            " echo password_verify(stream_get_contents(STDIN), $h) ? 'OK' : 'NO';"
        )
        argv = ["php", "-r", code, str(config), user]
    out = subprocess.run(argv, input=password, capture_output=True, text=True)
    return out.stdout.strip() == "OK"


@needs_php
def test_server_config_fresh(tmp_path, monkeypatch, capfd):
    site, web = make_site(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("YM_DB_PASSWORD", SECRET_DB)
    monkeypatch.setenv("YM_ADMIN_PASSWORD", SECRET_ADMIN)
    S = ["--site-dir", str(site), "-v"]
    argv = [*S, "target", "config", "local", "--db-name", "yourls", "--db-user", "yu"]
    argv += ["--admin-user", "ruben", "--db-host", "127.0.0.1:3306"]
    assert main(argv) == 0
    out, err = capfd.readouterr()
    assert SECRET_DB not in out + err and SECRET_ADMIN not in out + err
    cfg = web / "yourls-config/t.php"
    assert oct(cfg.stat().st_mode & 0o777) == "0o600"
    text = cfg.read_text()
    assert "define( 'YOURLS_DB_NAME', 'yourls' );" in text
    assert "define( 'YOURLS_DB_HOST', '127.0.0.1:3306' );" in text
    assert "define( 'YOURLS_SITE', 'http://127.0.0.1:8099' );" in text
    key = re.search(r"YOURLS_COOKIEKEY', '([0-9a-f]+)'", text)
    assert key and len(key[1]) == 64
    assert re.search(r"'ruben' => 'phpass:!2y!\d\d![A-Za-z0-9./]{53}',", text)
    assert yourls_password_ok(cfg, "ruben", SECRET_ADMIN)
    assert not yourls_password_ok(cfg, "ruben", "wrong")

    # refuses to overwrite; --force keeps a backup
    assert main(argv) == 2
    assert "--force" in capfd.readouterr().err
    assert main([*argv, "--force"]) == 0
    backups = list((web / "yourls-config").glob("t.php.bak-*"))
    assert len(backups) == 1 and backups[0].read_text() == text
    assert oct(backups[0].stat().st_mode & 0o777) == "0o600"
    assert re.search(r"COOKIEKEY', '([0-9a-f]+)'", cfg.read_text())[1] != key[1]


def test_server_config_requires_values_non_interactive(tmp_path, monkeypatch, capsys):
    site, _ = make_site(tmp_path)
    monkeypatch.delenv("YM_DB_PASSWORD", raising=False)
    assert main(["--site-dir", str(site), "target", "config", "local"]) == 2
    assert "--db-name" in capsys.readouterr().err
    argv = [
        "--site-dir",
        str(site),
        "target",
        "config",
        "local",
        "--db-name",
        "n",
        "--db-user",
        "u",
    ]
    assert main([*argv, "--admin-user", "a"]) == 2
    assert "YM_DB_PASSWORD" in capsys.readouterr().err


def test_server_config_prompts(tmp_path, monkeypatch):
    """Interactive path: values via ask, secrets via the secret prompt (confirmed twice)."""
    site, _ = make_site(tmp_path, php=shutil.which("php") or "php")
    from yourls_manager.config import load_site
    from yourls_manager.remote import LocalRunner

    answers = iter(["", "dbname", "dbuser", "", "admin"])
    secrets_ = iter(["dbpw", "a1", "a2", "a1", "a1"])
    hashed = []

    def fake_hash(runner, php, password):
        hashed.append(password)
        return "phpass:!2y!10!fakehash"

    monkeypatch.setattr(sc, "hash_password", fake_hash)
    path = sc.server_config(
        load_site(site),
        "local",
        sc.ServerConfigOptions(),
        runner=LocalRunner(),
        env={},
        ask=lambda q: next(answers),
        secret_prompt=lambda q: next(secrets_),
    )
    text = Path(path).read_text()
    assert hashed == ["a1"]  # mismatch was retried
    assert "'YOURLS_DB_HOST', 'localhost'" in text and "'YOURLS_DB_PREFIX', 'yourls_'" in text
    assert "'YOURLS_DB_PASS', 'dbpw'" in text and "'admin' => 'phpass:!2y!10!fakehash'" in text


def test_server_config_hash_failure(tmp_path, monkeypatch, capsys):
    site, _ = make_site(tmp_path, php="/nonexistent/php")
    monkeypatch.setenv("YM_DB_PASSWORD", "x")
    monkeypatch.setenv("YM_ADMIN_PASSWORD", "y")
    argv = ["--site-dir", str(site), "target", "config", "local", "--db-name", "n"]
    assert main([*argv, "--db-user", "u", "--admin-user", "a"]) == 1
    assert "could not hash" in capsys.readouterr().err


REAL_CONFIG = """\
<?php
define( 'YOURLS_DB_USER', 'olduser' );
define( 'YOURLS_DB_PASS', 'oldpass' );
$yourls_user_passwords = [ 'bob' => 'plain', 'amy' => 'phpass:!2y!10!x' ];
"""


def test_server_config_from_existing(tmp_path, capsys):
    site, web = make_site(tmp_path)
    S = ["--site-dir", str(site), "target", "config", "local", "--from-existing"]
    assert main(S) == 2
    assert "no existing YOURLS config" in capsys.readouterr().err
    write_files(web / "site", {"user/config.php": REAL_CONFIG})
    assert main([*S, "--db-name", "x"]) == 2
    assert main(S) == 0
    err = capsys.readouterr().err
    assert "clear text for: bob" in err
    cfg = web / "yourls-config/t.php"
    assert cfg.read_text() == REAL_CONFIG
    assert oct(cfg.stat().st_mode & 0o777) == "0o600"
    assert main(S) == 2  # exists
    assert main([*S, "--force"]) == 0
    assert list((web / "yourls-config").glob("t.php.bak-*"))

    # includes/config.php fallback, and the shim is refused
    (web / "site/user/config.php").write_text(
        "<?php getenv('YOURLS_CONFIG_FILE'); // server-local configuration missing\n"
    )
    assert main([*S, "--force"]) == 2
    assert "shim" in capsys.readouterr().err
    (web / "site/user/config.php").unlink()
    write_files(web / "site", {"includes/config.php": REAL_CONFIG.replace("old", "inc")})
    assert main([*S, "--force"]) == 0
    assert "incuser" in cfg.read_text()
