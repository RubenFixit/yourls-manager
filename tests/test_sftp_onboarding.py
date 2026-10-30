"""target config, target login and the wizard for sftp targets."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

from yourls_manager import serverconfig as sc
from yourls_manager.cli import main
from yourls_manager.config import load_site
from yourls_manager.login import check_login, install_key_sftp, login
from yourls_manager.transport.sftp import SftpTransport
from yourls_manager.wizard import run_wizard

from .conftest import (
    YOURLS_CORE_URL,
    git_insteadof,
    needs_copier,
    needs_php,
    needs_sshd,
    new_site_args,
    write_files,
)
from .sftp_server import host_public_key
from .test_serverconfig import EXAMPLE, SECRET_ADMIN, SECRET_DB, yourls_password_ok
from .test_wizard import Script

# --------------------------------------------------------------------------- bcrypt


def _yourls_src() -> Path | None:
    env = os.environ.get("YM_TEST_YOURLS_SRC")
    return Path(env) if env and (Path(env) / "includes/functions-auth.php").is_file() else None


def test_bcrypt_phpass_format():
    h = sc.bcrypt_phpass("pw")
    assert re.fullmatch(r"phpass:!2y!10![A-Za-z0-9./]{53}", h), h
    assert sc.PHPASS_RE.match(h)
    assert sc.bcrypt_phpass("pw") != h  # salted
    with pytest.raises(sc.UsageError, match="72 bytes"):
        sc.bcrypt_phpass("x" * 73)
    with pytest.raises(sc.UsageError, match="empty"):
        sc.bcrypt_phpass("")


@needs_php
@pytest.mark.parametrize("password", ["pw", SECRET_ADMIN, "ünïcødé ✓", "x" * 72])
def test_bcrypt_hash_verifies_with_php_password_verify(password):
    h = sc.bcrypt_phpass(password)
    code = (
        "$h = str_replace('!', '$', substr($argv[1], 7));"
        " $p = stream_get_contents(STDIN);"
        " echo password_verify($p, $h) ? 'OK' : 'NO', ' ', password_get_info($h)['algoName'];"
    )
    wrong = password[:-1] + ("y" if password[-1] != "y" else "z")
    for attempt, expect in ((password, "OK"), (wrong, "NO")):
        out = subprocess.run(
            ["php", "-r", code, h], input=attempt.encode(), capture_output=True, check=True
        )
        assert out.stdout.decode() == f"{expect} bcrypt"


@needs_php
@pytest.mark.skipif(_yourls_src() is None, reason="set YM_TEST_YOURLS_SRC to a YOURLS checkout")
def test_bcrypt_hash_verifies_with_yourls(tmp_path, monkeypatch):
    """YOURLS' own yourls_check_password_hash() accepts the locally made hash."""
    monkeypatch.setenv("YM_TEST_YOURLS_SRC", str(_yourls_src()))
    cfg = tmp_path / "config.php"
    users = {"ruben": sc.bcrypt_phpass(SECRET_ADMIN)}
    cfg.write_text(sc.set_user_passwords("<?php\n$yourls_user_passwords = [];\n", users))
    assert yourls_password_ok(cfg, "ruben", SECRET_ADMIN)
    assert not yourls_password_ok(cfg, "ruben", SECRET_ADMIN + "!")


# --------------------------------------------------------------------------- target config


def sc_site(tmp_path: Path, account) -> Path:
    site = tmp_path / "site"
    write_files(
        site,
        {
            "site.toml": textwrap.dedent(
                """\
                [site]
                name = "T"
                slug = "t"
                url = "https://go.example.com"
                [core]
                version = "1.10.6"
                """
            )
            + account.target_toml(path='"go"', url='"https://sftp.example.com"'),
            "server/config.example.php": EXAMPLE,
        },
    )
    return site


@pytest.mark.sftp
@needs_sshd
def test_server_config_over_sftp(tmp_path, account, ssh_home, monkeypatch, capsys):
    monkeypatch.setenv("YM_PROD_PASSWORD", account.password)
    monkeypatch.setenv("YM_DB_PASSWORD", SECRET_DB)
    monkeypatch.setenv("YM_ADMIN_PASSWORD", SECRET_ADMIN)
    site = sc_site(tmp_path, account)
    S = ["--site-dir", str(site), "-v"]
    argv = [*S, "target", "config", "prod", "--db-name", "yourls", "--db-user", "yu"]
    argv += ["--admin-user", "ruben"]
    assert main(argv) == 0
    out, err = capsys.readouterr()
    assert "locally (bcrypt, cost 10)" in out
    assert SECRET_DB not in out + err and SECRET_ADMIN not in out + err
    cfg = account.real("yourls-config/t.php")
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    text = cfg.read_text()
    assert "define( 'YOURLS_SITE', 'https://sftp.example.com' );" in text
    assert re.search(r"'ruben' => 'phpass:!2y!10![A-Za-z0-9./]{53}',", text)
    if _yourls_src() is not None:
        monkeypatch.setenv("YM_TEST_YOURLS_SRC", str(_yourls_src()))
    assert yourls_password_ok(cfg, "ruben", SECRET_ADMIN)
    assert not yourls_password_ok(cfg, "ruben", "wrong")

    # refuses to overwrite; --force keeps a 0600 backup
    assert main(argv) == 2
    assert "--force" in capsys.readouterr().err
    assert main([*argv, "--force"]) == 0
    backups = list(cfg.parent.glob("t.php.bak-*"))
    assert len(backups) == 1 and backups[0].read_text() == text
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert cfg.read_text() != text
    assert not [p for p in account.home.rglob("*") if p.name.endswith(".ym-tmp")]


@pytest.mark.sftp
@needs_sshd
def test_server_config_from_existing_over_sftp(tmp_path, sftp_only, ssh_home, monkeypatch,
                                               capsys):  # fmt: skip
    monkeypatch.setenv("YM_PROD_PASSWORD", sftp_only.password)
    site = sc_site(tmp_path, sftp_only)
    S = ["--site-dir", str(site), "target", "config", "prod", "--from-existing"]
    assert main(S) == 2
    assert "no existing YOURLS config" in capsys.readouterr().err
    real = "<?php\ndefine( 'YOURLS_DB_USER', 'u' );\n$yourls_user_passwords = [ 'bob' => 'pw' ];\n"
    sftp_only.write("go/user/config.php", real)
    assert main(S) == 0
    cfg = sftp_only.real("yourls-config/t.php")
    assert cfg.read_text() == real and stat.S_IMODE(cfg.stat().st_mode) == 0o600
    assert "clear text for: bob" in capsys.readouterr().err
    assert main(S) == 2  # exists
    assert main([*S, "--force"]) == 0
    assert len(list(cfg.parent.glob("t.php.bak-*"))) == 1
    shim = "<?php getenv('YOURLS_CONFIG_FILE'); // server-local configuration missing\n"
    sftp_only.write("go/user/config.php", shim)
    assert main([*S, "--force"]) == 2
    assert "shim" in capsys.readouterr().err


# --------------------------------------------------------------------------- target login


def _keygen(argv) -> int:
    """Stand-in for the terminal: run the real ssh-keygen without a passphrase prompt."""
    assert argv[0] == "ssh-keygen"
    return subprocess.run([*argv, "-N", ""], capture_output=True).returncode


@pytest.mark.sftp
@needs_sshd
def test_ssh_setup_installs_key_over_sftp(tmp_path, sftp_only, ssh_home, monkeypatch, capsys):
    """SFTP-only (chrooted, no shell) account: trust the host key, create a key, log in
    with the password and append the key to .ssh/authorized_keys over SFTP."""
    (ssh_home / ".ssh" / "known_hosts").unlink()  # first connection ever
    monkeypatch.setenv("YM_PROD_PASSWORD", sftp_only.password)
    site = sc_site(tmp_path, sftp_only)
    spec = load_site(site).target("prod")
    assert check_login(spec).reason == "hostkey"
    questions: list[str] = []

    def confirm(q: str) -> bool:
        questions.append(q)
        return True

    assert login(spec, confirm=confirm, interactive=_keygen) is True
    assert any("Trust this host key" in q for q in questions)
    assert any("Create one now" in q for q in questions)
    assert any("over SFTP" in q for q in questions)
    kh = (ssh_home / ".ssh" / "known_hosts").read_text()
    assert kh == f"[127.0.0.1]:{sftp_only.port} {host_public_key()}\n"
    ak = sftp_only.real(".ssh/authorized_keys")
    pub = (ssh_home / ".ssh" / "id_ed25519.pub").read_text().strip()
    assert ak.read_text() == pub + "\n"
    assert stat.S_IMODE(ak.stat().st_mode) == 0o600
    assert stat.S_IMODE(ak.parent.stat().st_mode) == 0o700
    assert check_login(spec).ok  # key login, no password involved

    # idempotent; non-TTY CLI check succeeds with the key alone
    monkeypatch.delenv("YM_PROD_PASSWORD")
    with SftpTransport.for_target(spec, interactive=False) as t:
        assert install_key_sftp(t, pub) is False
    assert ak.read_text() == pub + "\n"
    capsys.readouterr()
    assert main(["--site-dir", str(site), "target", "login", "prod"]) == 0
    assert "login works" in capsys.readouterr().out


@pytest.mark.sftp
@needs_sshd
def test_remove_old_ci_keys_over_sftp(tmp_path, sftp_only, ssh_home, monkeypatch):
    from yourls_manager.ci import remove_old_keys

    monkeypatch.setenv("YM_PROD_PASSWORD", sftp_only.password)
    spec = load_site(sc_site(tmp_path, sftp_only)).target("prod")
    with SftpTransport.for_target(spec, interactive=False) as t:
        for line in ("ssh-ed25519 AAAA1 me@laptop", "ssh-ed25519 AAAA2 ym-ci-s-production-1",
                     "ssh-ed25519 AAAA3 ym-ci-s-production-2"):  # fmt: skip
            install_key_sftp(t, line)
    assert remove_old_keys(spec, "ym-ci-s-production-", "ym-ci-s-production-2") == 1
    ak = sftp_only.real(".ssh/authorized_keys")
    assert ak.read_text() == "ssh-ed25519 AAAA1 me@laptop\nssh-ed25519 AAAA3 ym-ci-s-production-2\n"
    assert stat.S_IMODE(ak.stat().st_mode) == 0o600
    assert remove_old_keys(spec, "ym-ci-s-production-", "ym-ci-s-production-2") == 0


@pytest.mark.sftp
@needs_sshd
def test_ssh_setup_sftp_declined_and_non_tty(tmp_path, account, ssh_home, capsys):
    site = sc_site(tmp_path, account)
    spec = load_site(site).target("prod")
    assert check_login(spec).reason == "auth"
    assert login(spec, confirm=lambda q: False, interactive=_keygen) is False
    out = capsys.readouterr().out
    assert "authorized_keys" in out and "YM_PROD_PASSWORD" in out
    assert main(["--site-dir", str(site), "target", "login", "prod"]) == 2
    assert "needs a terminal" in capsys.readouterr().err


# --------------------------------------------------------------------------- wizard


SFTP_PROD = """
[deploy.targets.prod]
type = "sftp"
host = "ssh.example.com"
user = "ruben"
path = "go.example.com"

[[plugins]]
name = "site-branding"
"""


def test_wizard_lists_manual_steps_for_sftp(tmp_path, capsys):
    calls: list[list[str]] = []

    def ym(argv) -> int:
        argv = list(argv)
        calls.append(argv)
        if argv[0] == "new":
            dest = Path(argv[1])
            (dest / ".git").mkdir(parents=True)
            (dest / "site.toml").write_text(
                '[site]\nname = "T"\nslug = "t"\nurl = "https://go.example.com"\n'
                '[core]\nversion = "1.10.6"\n' + SFTP_PROD
            )
        return 0

    ask = Script(["new", "s", "y"])  # new site, dir, dry-run preview
    res = run_wizard(ask, invoke=ym, config_exists=lambda s, t: True,
                     ssh_ok=lambda s, t: True, cwd=tmp_path, home=tmp_path / "st")  # fmt: skip
    assert calls[-1][-3:] == ["deploy", "prod", "--dry-run"]
    out = capsys.readouterr().out
    assert "prod is an sftp target" in out
    assert "https://go.example.com/admin/plugins.php" in out and "site-branding" in out
    assert any("admin/install.php" in r for r in res.remaining)
    assert any("admin/plugins.php" in r for r in res.remaining)


def test_wizard_ssh_message_for_sftp(tmp_path, capsys):
    def ym(argv) -> int:
        argv = list(argv)
        if argv[0] == "new":
            dest = Path(argv[1])
            (dest / ".git").mkdir(parents=True)
            (dest / "site.toml").write_text(
                '[site]\nname = "T"\nslug = "t"\nurl = "https://go.example.com"\n'
                '[core]\nversion = "1.10.6"\n' + SFTP_PROD
            )
        return 0

    res = run_wizard(Script(["new", "s", "n"]), invoke=ym, ssh_ok=lambda s, t: False,
                     cwd=tmp_path, home=tmp_path / "st")  # fmt: skip
    assert "YM_PROD_PASSWORD" in capsys.readouterr().out
    assert res.resumable and any("target login prod" in r for r in res.remaining)


# --------------------------------------------------------------------------- template


@needs_copier
def test_new_site_with_sftp_target(tmp_path, fake_core, no_uv_lock, capfd):
    """The real template renders a valid sftp target (deploy_type=sftp)."""
    git_insteadof(YOURLS_CORE_URL, fake_core.url)
    site = tmp_path / "s"
    args = new_site_args(
        site,
        "yourls_version=1.1.0",
        "deploy_type=sftp",
        "deploy_host=ssh.example.com",
        "deploy_user=ruben",
        "include_example_plugins=false",
    )
    assert main(args) == 0
    out, err = capfd.readouterr()
    assert "site.toml:" not in err  # no config warnings
    assert "sftp target: ym prints the browser steps" in out
    cfg = load_site(site)
    spec = cfg.target("prod")
    assert spec.is_sftp and spec.host == "ssh.example.com" and spec.user == "ruben"
    assert spec.state_dir == ".yourls-manager/go-example-com"
    assert "php_binary" not in (site / ".copier-answers.yml").read_text()
    assert "YM_PROD_PASSWORD" in (site / "README.md").read_text()
