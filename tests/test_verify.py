from __future__ import annotations

import pytest

from yourls_manager.cli import main
from yourls_manager.config import ConfigError
from yourls_manager.verify import Case, check_case, load_cases, location_matches, verify

from .conftest import Handler


def write_cases(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_location_matching():
    assert location_matches("https://a/b", "https://a/b")
    assert not location_matches("https://a/b", "https://a/b/c")
    assert location_matches("https://a/*", "https://a/b/c")
    assert not location_matches("https://a/*", None)


def test_cases_pass(server, tmp_path):
    cases = write_cases(
        tmp_path / "tests/redirects.toml",
        """
[[case]]
path = "/g"
status = 301
location = "https://git.example.org/"

[[case]]
path = "/g/repo?ref=main"
status = [301, 302]
location = "https://git.example.org/repo*"

[[case]]
path = "/tmp"
status = [301, 302]

[[case]]
path = "/missing"
status = 404

[[case]]
path = "/"
status = 200
""",
    )
    assert verify(server, cases) is True


def test_cases_fail(server, tmp_path, capsys):
    cases = write_cases(
        tmp_path / "r.toml",
        '[[case]]\npath = "/g"\nstatus = 302\nlocation = "https://elsewhere/"\n',
    )
    assert verify(server, cases) is False
    out = capsys.readouterr().out
    assert "FAIL  /g" in out and "status 301 not in [302]" in out


def test_check_case_does_not_follow_redirects(server):
    ok, detail = check_case(server, Case("/g", (301,), "https://git.example.org/"))
    assert ok, detail


def test_health_failure(server, tmp_path, monkeypatch):
    monkeypatch.setitem(Handler.routes, "/health.php", (503, None, b'{"ok": false}'))
    assert verify(server, tmp_path / "none.toml") is False


def test_cli_verify_url(server, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_cases(tmp_path / "tests/redirects.toml", '[[case]]\npath = "/g"\nstatus = 301\n')
    assert main(["verify", "--url", server]) == 0
    assert main(["verify"]) == 2


def test_bad_cases_file(tmp_path):
    p = write_cases(tmp_path / "r.toml", '[[case]]\npath = "g"\nstatus = 301\n')
    with pytest.raises(ConfigError, match=r"case\[0\]\.path"):
        load_cases(p)
    p.write_text('[[case]]\npath = "/g"\nstatus = "301"\n')
    with pytest.raises(ConfigError, match=r"case\[0\]\.status"):
        load_cases(p)
