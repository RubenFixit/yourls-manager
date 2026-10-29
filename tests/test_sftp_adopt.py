"""`ym adopt` from sftp targets (design §10.4), then target config + deploy over SFTP."""

from __future__ import annotations

import json
import re
import stat
import sys
import textwrap
import tomllib

import pytest

from yourls_manager import adopt as adopt_mod
from yourls_manager.cli import main
from yourls_manager.config import load_site

from .conftest import git, install_client_key, needs_rsync, needs_sshd
from .test_adopt import (
    FAKE_PHP,
    REAL_CONFIG,
    check_adopted,
    make_install,
    make_site,
)


def sftp_site(tmp_path, fake_core, account, url: str):
    web = account.real("go.example.com")
    make_install(web)
    (web / "private").mkdir()
    (web / "private/big.bin").write_bytes(b"x" * 10)
    account.chown()
    target = account.target_toml(
        path='"go.example.com"', protect='[".well-known/", "private/"]', url=f'"{url}"'
    )
    return make_site(tmp_path, fake_core.url, target), web


def server_files(web):
    return {p.relative_to(web).as_posix(): p.read_bytes() for p in web.rglob("*") if p.is_file()}


@pytest.mark.sftp
@needs_sshd
def test_adopt_from_sftp_target_then_deploy(
    tmp_path, fake_core, account, ssh_home, client_key, state_server, capsys
):  # fmt: skip
    url, _ = state_server
    account.authorize(client_key.with_name("id_ed25519.pub").read_text())
    install_client_key(ssh_home, client_key)
    site, web = sftp_site(tmp_path, fake_core, account, url)
    S = ["--site-dir", str(site)]
    before = server_files(web)
    state = account.real(".yourls-manager/t")

    # ---- dry run: nothing written anywhere
    assert main([*S, "adopt", "--from-target", "prod", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "read-only SFTP download" in out and "dry run: nothing written" in out
    assert git(site, "status", "--porcelain") == "" and not state.exists()

    # ---- adopt with the active plugins given explicitly (--active)
    assert main([*S, "adopt", "--from-target", "prod", "--active", "x", "--dry-run"]) == 0
    capsys.readouterr()
    res = adopt_mod.adopt(
        load_site(site), from_target="prod", active=["custom-plug", "hyphens-in-urls"]
    )
    check_adopted(site, res, todo=False)
    assert "Active plugins: given with `--active`" in res.report.read_text()
    data = tomllib.loads((site / "site.toml").read_text())
    assert {p["name"]: p["active"] for p in data["plugins"]} == {
        "site-branding": True,
        "custom-plug": True,
        "hyphens-in-urls": True,
    }
    assert not (site / "overlay/private").exists()
    assert server_files(web) == before  # read-only

    # the snapshot's manifest is on the server now (ignored/protected files not in it)
    manifest = json.loads((state / "manifest.json").read_text())
    files = manifest["files"]
    assert manifest["core_version"] == "1.0.0" and manifest["adopted_from"].startswith("sftp://")
    assert "custom.php" in files and "user/config.php" in files and "README.md" in files
    assert not any(f.startswith(("private/", ".well-known/")) or "error_log" in f for f in files)
    assert stat.S_IMODE((state / "manifest.json").stat().st_mode) == 0o600
    git(site, "add", "-A")
    git(site, "commit", "-q", "-m", "adopt")
    capsys.readouterr()

    # ---- target config --from-existing over SFTP, then deploy
    assert main([*S, "target", "config", "prod", "--from-existing"]) == 0
    cfg_file = account.real("yourls-config/t.php")
    assert cfg_file.read_text() == REAL_CONFIG
    assert stat.S_IMODE(cfg_file.stat().st_mode) == 0o600
    assert main([*S, "deploy", "prod", "--dry-run", "-v"]) == 0
    out = capsys.readouterr().out
    assert "no manifest" not in out
    deletes = sorted(re.findall(r"^\s+delete (\S+)$", out, re.MULTILINE))
    assert deletes == ["CHANGELOG.md", "README.md"]  # adopted-install files dropped by exclude
    uploads = dict(re.findall(r"^\s+upload (\S+) \((\S+)\)$", out, re.MULTILINE))
    assert uploads[".htaccess"] == "changed" and uploads["user/config.php"] == "changed"
    assert uploads["health.php"] == "new" and "custom.php" not in uploads

    assert main([*S, "deploy", "prod", "--skip-verify"]) == 0
    assert not (web / "README.md").exists() and not (web / "CHANGELOG.md").exists()
    assert (web / "error_log").exists() and (web / "private/big.bin").exists()  # never ym's
    assert (web / ".well-known/acme-challenge/token").exists()
    assert "shim" in (web / "user/config.php").read_text()
    assert (web / "custom.php").read_bytes() == before["custom.php"]


@pytest.mark.sftp
@needs_sshd
def test_adopt_sftp_prompts_for_active_plugins(
    tmp_path, fake_core, sftp_only, ssh_home, monkeypatch, capsys
):  # fmt: skip
    """No --active, on a terminal: ask per plugin found (prompt injected)."""
    monkeypatch.setenv("YM_PROD_PASSWORD", sftp_only.password)
    site, _ = sftp_site(tmp_path, fake_core, sftp_only, "https://go.example.com")
    asked: list[str] = []

    def ask(question: str) -> bool:
        asked.append(question)
        return "custom-plug" in question

    res = adopt_mod.adopt(load_site(site), from_target="prod", ask_active=ask)
    assert [re.search(r"'([^']+)'", q)[1] for q in asked] == [
        "custom-plug",
        "hyphens-in-urls",
        "sample-plugin",
    ]
    data = tomllib.loads((site / "site.toml").read_text())
    assert {p["name"]: p["active"] for p in data["plugins"]} == {
        "site-branding": True,
        "custom-plug": True,
    }
    assert "answered at the prompt" in res.report.read_text()
    assert sftp_only.real(".yourls-manager/t/manifest.json").is_file()


@pytest.mark.sftp
@needs_sshd
def test_adopt_sftp_without_active_is_todo(tmp_path, fake_core, sftp_only, ssh_home,
                                           monkeypatch, capsys):  # fmt: skip
    monkeypatch.setenv("YM_PROD_PASSWORD", sftp_only.password)
    site, _ = sftp_site(tmp_path, fake_core, sftp_only, "https://go.example.com")
    res = adopt_mod.adopt(load_site(site), from_target="prod")  # not a TTY: no prompt
    check_adopted(site, res, todo=True)
    assert "--active a,b" in capsys.readouterr().err


@needs_rsync
def test_adopt_active_flag_for_rsync_target(tmp_path, fake_core, monkeypatch, capsys):
    """--active also works for rsync targets: the helper is not needed then."""
    monkeypatch.setenv("HOME", str(tmp_path))
    log = tmp_path / "php.log"
    monkeypatch.setenv("FAKE_PHP_LOG", str(log))
    php = tmp_path / "fake-php"
    php.write_text(f"#!{sys.executable}\n{FAKE_PHP}")
    php.chmod(0o755)
    web = tmp_path / "www" / "go"
    make_install(web)
    targets = textwrap.dedent(f'[deploy.targets.prod]\npath = "{web}"\nphp = "{php}"\n')
    site = make_site(tmp_path, fake_core.url, targets)
    argv = ["--site-dir", str(site), "adopt", "--from-target", "prod", "--active", ""]
    assert main(argv) == 0
    assert not log.exists()  # helper never ran
    data = tomllib.loads((site / "site.toml").read_text())
    assert {p["name"]: p["active"] for p in data["plugins"]} == {
        "site-branding": True,
        "custom-plug": False,
    }
