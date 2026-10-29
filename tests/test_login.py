"""`ym target login` and ssh failure hints."""

from __future__ import annotations

import subprocess

import pytest

from yourls_manager.config import TargetSpec
from yourls_manager.login import LoginCheck, classify, login
from yourls_manager.remote import SshRunner
from yourls_manager.util import YMError

SPEC = TargetSpec(name="prod", type="rsync", path="site", host="h.example", user="u", port=2222)


@pytest.mark.parametrize(
    ("stderr", "reason"),
    [
        ("u@h: Permission denied (publickey,password).", "auth"),
        ("Host key verification failed.", "hostkey"),
        ("ssh: Could not resolve hostname h: Name or service not known", "network"),
        ("ssh: connect to host h port 22: Connection timed out", "network"),
        ("something odd", "other"),
    ],
)
def test_classify(stderr, reason):
    assert classify(255, stderr).reason == reason
    assert classify(0, "").ok


class Checks:
    def __init__(self, *results: LoginCheck):
        self.results = list(results)

    def __call__(self, spec, identity=None):
        return self.results.pop(0)


def test_already_working_does_nothing():
    calls = []
    ok = login(
        SPEC, confirm=lambda q: True, interactive=calls.append, check=Checks(LoginCheck(True, "ok"))
    )
    assert ok and calls == []


def test_creates_and_installs_key(tmp_path, monkeypatch):
    key = tmp_path / ".ssh" / "id_ed25519"
    calls: list[list[str]] = []

    def interactive(argv):
        calls.append(list(argv))
        if argv[0] == "ssh-keygen":
            key.write_text("PRIVATE")
            key.with_name("id_ed25519.pub").write_text("ssh-ed25519 AAAA test")
        return 0

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
    ok = login(
        SPEC,
        confirm=lambda q: True,
        interactive=interactive,
        key=key,
        check=Checks(LoginCheck(False, "auth", "denied"), LoginCheck(True, "ok")),
    )
    assert ok
    assert calls[0][:3] == ["ssh-keygen", "-t", "ed25519"]
    assert calls[1] == ["ssh-copy-id", "-i", str(key) + ".pub", "-p", "2222", "u@h.example"]


def test_fallback_without_ssh_copy_id(tmp_path, monkeypatch):
    key = tmp_path / "k"
    key.write_text("PRIVATE")
    (tmp_path / "k.pub").write_text("ssh-ed25519 AAAA it's")
    calls: list[list[str]] = []
    monkeypatch.setattr("shutil.which", lambda name: None)
    ok = login(
        SPEC,
        confirm=lambda q: True,
        interactive=lambda a: calls.append(a) or 0,
        key=key,
        check=Checks(LoginCheck(False, "hostkey"), LoginCheck(True, "ok")),
    )
    assert ok and calls[0][:4] == ["ssh", "-p", "2222", "u@h.example"]
    script = calls[0][4]
    # the remote script must be valid sh even with a quote in the key comment
    assert subprocess.run(["sh", "-n", "-c", script]).returncode == 0
    assert "authorized_keys" in script


def test_declined_prints_manual_steps(tmp_path, capsys):
    ok = login(
        SPEC,
        confirm=lambda q: False,
        interactive=lambda a: 0,
        key=tmp_path / "none",
        check=Checks(LoginCheck(False, "auth")),
    )
    assert not ok and "ssh-copy-id" in capsys.readouterr().out


def test_network_error_raises():
    with pytest.raises(YMError, match="cannot reach"):
        login(
            SPEC,
            confirm=lambda q: True,
            interactive=lambda a: 0,
            check=Checks(LoginCheck(False, "network", "timed out")),
        )


def test_still_failing_mentions_agent(tmp_path, monkeypatch):
    key = tmp_path / "k"
    key.write_text("x")
    (tmp_path / "k.pub").write_text("ssh-ed25519 AAAA")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
    with pytest.raises(YMError, match="ssh-add"):
        login(
            SPEC,
            confirm=lambda q: True,
            interactive=lambda a: 0,
            key=key,
            check=Checks(
                LoginCheck(False, "auth"), LoginCheck(False, "auth"), LoginCheck(False, "auth")
            ),
        )


def test_runner_hint_on_permission_denied(monkeypatch):
    r = SshRunner("h.example", "u")

    def fake_exec(script, input):
        return subprocess.CompletedProcess([], 255, b"", b"u@h: Permission denied (publickey).")

    monkeypatch.setattr(r, "_exec", fake_exec)
    with pytest.raises(YMError, match="ym target login <target>"):
        r.run(["true"])


def test_key_works_only_with_explicit_identity(tmp_path, monkeypatch):
    key = tmp_path / "k"
    key.write_text("x")
    (tmp_path / "k.pub").write_text("ssh-ed25519 AAAA")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
    checks = Checks(LoginCheck(False, "auth"), LoginCheck(False, "auth"), LoginCheck(True, "ok"))
    with pytest.raises(YMError, match="IdentityFile"):
        login(SPEC, confirm=lambda q: True, interactive=lambda a: 0, key=key, check=checks)


def test_sftp_password_login_makes_key_optional(monkeypatch, capsys):
    spec = TargetSpec(name="prod", type="sftp", path="site", host="h.example", user="u")
    monkeypatch.setenv("YM_PROD_PASSWORD", "secret")

    class Probe:
        login = "u@h.example"

    def check(spec, identity=None, *, password=False):
        return LoginCheck(True, "ok") if password else LoginCheck(False, "auth", "denied")

    questions: list[str] = []
    ok = login(
        spec,
        confirm=lambda q: questions.append(q) or False,
        interactive=lambda a: pytest.fail("no key should be created"),
        check=check,
        transport=lambda *a, **k: Probe(),
    )
    assert ok is True
    assert "password login works ($YM_PROD_PASSWORD)" in capsys.readouterr().out
    assert questions == ["Set up an SSH key anyway, so no password is needed?"]
