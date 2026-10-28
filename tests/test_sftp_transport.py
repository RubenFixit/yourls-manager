"""SFTP transport (design §10.2) against a real OpenSSH server, plus sftp target config."""

from __future__ import annotations

import base64
import hashlib
import stat
import sys
from pathlib import Path

import pytest

from yourls_manager.config import ConfigError, parse_site
from yourls_manager.transport.sftp import (
    SftpAuthError,
    SftpHostKeyError,
    SftpTransport,
    password_env,
)

from .conftest import install_client_key, needs_sshd
from .sftp_server import host_public_key

# --------------------------------------------------------------------------- units


def test_password_env_name():
    assert password_env("prod") == "YM_PROD_PASSWORD"
    assert password_env("go.example-com") == "YM_GO_EXAMPLE_COM_PASSWORD"


def _site(target: dict) -> dict:
    return {
        "site": {"name": "T", "slug": "t", "url": "https://go.example.com"},
        "core": {"version": "1.10.6"},
        "deploy": {"targets": {"prod": target}},
    }


def test_sftp_target_config(tmp_path):
    cfg = parse_site(
        _site({"type": "sftp", "host": "h.example", "user": "u", "path": "go.example.com"}),
        tmp_path,
    )
    spec = cfg.targets["prod"]
    assert spec.is_sftp and not spec.is_local
    assert spec.state_dir == ".yourls-manager/t"
    assert spec.config_file == "yourls-config/t.php"
    assert "port" not in spec.given and "host" in spec.given
    assert cfg.warnings == []

    cfg = parse_site(
        _site(
            {
                "type": "sftp",
                "host": "h",
                "path": "/srv/www/go",
                "port": 2222,
                "php": "php83",
                "backup_dir": "b",
                "backup_keep": 3,
                "state_dir": "~/state/go",
            }
        ),
        tmp_path,
    )
    spec = cfg.targets["prod"]
    assert spec.state_dir == "state/go" and spec.config_file == "/srv/www/yourls-config/t.php"
    assert "port" in spec.given
    assert sorted(w.split("'")[1] for w in cfg.warnings) == [
        "deploy.targets.prod.backup_dir",
        "deploy.targets.prod.backup_keep",
        "deploy.targets.prod.php",
    ]
    assert all("does not apply to sftp targets" in w for w in cfg.warnings)

    with pytest.raises(ConfigError, match="host: is required for sftp"):
        parse_site(_site({"type": "sftp", "path": "x"}), tmp_path)
    with pytest.raises(ConfigError, match="state_dir"):
        parse_site(_site({"type": "sftp", "host": "h", "path": "x", "state_dir": "a b"}), tmp_path)
    # state_dir means nothing for rsync targets
    cfg = parse_site(_site({"path": "x", "state_dir": "s"}), tmp_path)
    assert any("unknown key 'deploy.targets.prod.state_dir'" in w for w in cfg.warnings)


def test_ssh_config_is_honored(tmp_path):
    conf = tmp_path / "config"
    conf.write_text(
        "Host go\n  HostName real.example\n  Port 2200\n  User cfguser\n  IdentityFile ~/keys/go\n"
    )
    t = SftpTransport("go", ssh_config=conf, target_name="prod")
    ep = t.endpoint
    assert (ep.host, ep.port, ep.user) == ("real.example", 2200, "cfguser")
    assert ep.identity_files == (str(Path("~/keys/go").expanduser()),)
    t = SftpTransport("go", port=22, user="me", ssh_config=conf)
    assert (t.endpoint.port, t.endpoint.user) == (22, "me")  # the target wins
    assert t.describe() == "sftp://me@real.example"


def test_paramiko_is_imported_lazily(tmp_path):
    import subprocess

    code = (
        "import sys; from yourls_manager.cli import main;"
        f" main(['--site-dir', {str(tmp_path)!r}, 'status']);"
        " print('paramiko' in sys.modules, 'bcrypt' in sys.modules)"
    )
    (tmp_path / "site.toml").write_text(
        '[site]\nname="T"\nslug="t"\nurl="https://x.example"\n[core]\nversion="1.10.6"\n'
        '[deploy.targets.prod]\ntype="sftp"\nhost="h"\npath="p"\n'
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.stdout.strip().endswith("False False"), out.stdout + out.stderr


# --------------------------------------------------------------------------- real server


def transport(account, home: Path, **kw) -> SftpTransport:
    kw.setdefault("interactive", False)
    return SftpTransport(
        account.host,
        port=account.port,
        user=account.user,
        target_name="prod",
        **kw,
    )


@pytest.mark.sftp
@needs_sshd
def test_key_auth_default_key_file(account, ssh_home, client_key):
    """No agent; the key is found as ~/.ssh/id_ed25519 like ssh would."""
    account.authorize(client_key.with_name("id_ed25519.pub").read_text())
    install_client_key(ssh_home, client_key)
    with transport(account, ssh_home, env={}) as t:
        assert t.home() == account.login_dir
        assert t.abspath("www") == f"{account.login_dir}/www"


@pytest.mark.sftp
@needs_sshd
def test_key_auth_identity_file_from_ssh_config(account, ssh_home, client_key):
    account.authorize(client_key.with_name("id_ed25519.pub").read_text())
    (ssh_home / ".ssh" / "config").write_text(
        f"Host myhost\n  HostName 127.0.0.1\n  IdentityFile {client_key}\n"
    )
    t = SftpTransport("myhost", port=account.port, user=account.user, interactive=False, env={})
    with t:
        assert t.home() == account.login_dir


@pytest.mark.sftp
@needs_sshd
def test_password_from_env(account, ssh_home):
    env = {"YM_PROD_PASSWORD": account.password}
    with transport(account, ssh_home, env=env) as t:
        assert t.home() == account.login_dir
    with pytest.raises(SftpAuthError, match="password was rejected"):
        transport(account, ssh_home, env={"YM_PROD_PASSWORD": "wrong"}).connect()
    with pytest.raises(SftpAuthError, match=r"no key was accepted[\s\S]*YM_PROD_PASSWORD"):
        transport(account, ssh_home, env={}).connect()


@pytest.mark.sftp
@needs_sshd
def test_password_prompt_once_per_command(sftp_only, ssh_home):
    asked: list[str] = []

    def prompt(q):
        asked.append(q)
        return sftp_only.password

    for _ in range(2):
        with transport(sftp_only, ssh_home, env={}, interactive=True, secret_prompt=prompt) as t:
            t.home()
    assert len(asked) == 1 and "ymsftponly@127.0.0.1" in asked[0]


@pytest.mark.sftp
@needs_sshd
def test_unknown_host_key(sftp_only, ssh_home, tmp_path):
    kh = tmp_path / "kh" / "known_hosts"  # injectable; the real ~/.ssh is never touched
    env = {"YM_PROD_PASSWORD": sftp_only.password}
    with pytest.raises(SftpHostKeyError) as exc:
        transport(sftp_only, ssh_home, env=env, known_hosts=kh).connect()
    msg = str(exc.value)
    blob = base64.b64decode(host_public_key().split()[1])
    fp = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    assert "not known yet" in msg and fp in msg and "ssh-keyscan -p" in msg
    assert "ym ssh-setup prod" in msg and not kh.exists()

    # declined on a terminal: nothing written
    t = transport(sftp_only, ssh_home, env=env, known_hosts=kh, interactive=True,
                  confirm=lambda q: False)  # fmt: skip
    with pytest.raises(SftpHostKeyError, match="not trusted"):
        t.connect()
    assert not kh.exists()

    # trusted on a terminal: appended (and trusted on the next connect without asking)
    questions: list[str] = []
    t = transport(sftp_only, ssh_home, env=env, known_hosts=kh, interactive=True,
                  confirm=lambda q: questions.append(q) or True)  # fmt: skip
    with t:
        t.home()
    assert len(questions) == 1
    line = kh.read_text()
    assert line == f"[127.0.0.1]:{sftp_only.port} {host_public_key()}\n"
    with transport(sftp_only, ssh_home, env=env, known_hosts=kh) as t:
        t.home()
    assert kh.read_text() == line


@pytest.mark.sftp
@needs_sshd
def test_changed_host_key_always_fails(sftp_only, ssh_home, tmp_path):
    import paramiko

    other = paramiko.Ed25519Key.from_private_key_file(str(_new_key(tmp_path)))
    kh = tmp_path / "known_hosts"
    kh.write_text(f"[127.0.0.1]:{sftp_only.port} ssh-ed25519 {other.get_base64()}\n")
    env = {"YM_PROD_PASSWORD": sftp_only.password}
    for interactive in (False, True):
        t = transport(sftp_only, ssh_home, env=env, known_hosts=kh, interactive=interactive,
                      confirm=lambda q: True)  # fmt: skip
        with pytest.raises(SftpHostKeyError, match="HOST KEY CHANGED") as exc:
            t.connect()
        assert exc.value.changed and "ssh-keygen -R" in str(exc.value)


def _new_key(tmp_path: Path) -> Path:
    import subprocess

    key = tmp_path / "otherkey"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    return key


@pytest.mark.sftp
@needs_sshd
def test_file_operations(account, ssh_home):
    env = {"YM_PROD_PASSWORD": account.password}
    with transport(account, ssh_home, env=env) as t:
        t.mkdirs("w/a/b")
        t.mkdirs("w/a/b")  # idempotent
        t.write_bytes("w/a/b/secret.php", b"<?php // s", 0o600)
        t.write_bytes("w/a/b/secret.php", b"<?php // s2", 0o600)  # atomic replace
        src = ssh_home / "local.txt"
        src.write_text("local")
        t.upload(src, t.abspath("w/up.txt"))
        assert t.read_bytes("w/a/b/secret.php") == b"<?php // s2"
        assert stat.S_IMODE(t.stat("w/a/b/secret.php").st_mode) == 0o600
        assert stat.S_IMODE(t.stat("w/up.txt").st_mode) == 0o644
        assert t.is_dir("w/a") and t.is_file("w/up.txt") and not t.exists("w/nope")
        assert [a.filename for a in t.listdir("w")] == ["a", "up.txt"]
        t.write_bytes("w/a/x.log", b"log")
        res = t.walk(t.abspath("w"), skip=lambda rel: rel.endswith(".log"))
        assert [r for r, _ in res.files] == ["a/b/secret.php", "up.txt"]
        assert res.dirs == ["a", "a/b"]
        dl = ssh_home / "dl" / "s.php"
        t.download(t.abspath("w/a/b/secret.php"), dl)
        assert dl.read_bytes() == b"<?php // s2"
        t.remove(t.abspath("w/a/b/secret.php"))
        t.rmdir(t.abspath("w/a/b"))
        t.chmod(t.abspath("w/up.txt"), 0o640)
        assert stat.S_IMODE(t.stat("w/up.txt").st_mode) == 0o640
    # no temp files left behind; the server sees exactly what was written
    leftovers = [p for p in account.home.rglob("*") if p.name.endswith(".ym-tmp")]
    assert leftovers == []
    assert account.real("w/up.txt").read_text() == "local"
