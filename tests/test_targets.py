"""`ym target add` / `ym target list` (design §12)."""

from __future__ import annotations

import json
import textwrap
import tomllib

import pytest

from yourls_manager.cli import main
from yourls_manager.config import load_site
from yourls_manager.targets import TargetOptions, add_target

SITE = """\
# my site
[site]
name = "T"
slug = "t"
url = "https://go.example.com"

[core]
version = "1.10.6"

# [deploy.targets.prod]
# host = "..."
"""


@pytest.fixture
def site(tmp_path):
    (tmp_path / "site.toml").write_text(SITE)
    return tmp_path


def test_add_rsync_non_interactive(site, capsys):
    php = "/usr/local/php83/bin/php"
    argv = ["--site-dir", str(site), "target", "add", "prod", "--host", "h.example.com",
            "--user", "ruben", "--path", "go.example.com", "--php", php]  # fmt: skip
    assert main(argv) == 0
    text = (site / "site.toml").read_text()
    assert text.startswith(SITE.rstrip("\n"))  # comments and layout kept
    prod = tomllib.loads(text)["deploy"]["targets"]["prod"]
    assert prod == {
        "type": "rsync",
        "host": "h.example.com",
        "user": "ruben",
        "path": "go.example.com",
        "php": php,
        "accept": "release",  # prod default
    }
    out = capsys.readouterr().out
    assert "ym target login prod" in out and "ym target config prod" in out


def test_add_sftp_and_list(site, capsys):
    S = ["--site-dir", str(site)]
    argv = [*S, "target", "add", "staging", "--type", "sftp", "--host", "s.example.com",
            "--port", "2222", "--path", "staging.example.com", "--php", "php8",
            "--url", "https://staging.example.com"]  # fmt: skip
    assert main(argv) == 0
    err = capsys.readouterr().err
    assert "--php does not apply to sftp" in err
    spec = load_site(site).target("staging")
    assert (spec.type, spec.port, spec.accept, spec.url) == (
        "sftp", 2222, "any", "https://staging.example.com"
    )  # fmt: skip
    assert main([*S, "target", "add", "local", "--path", str(site / "www")]) == 0
    capsys.readouterr()
    assert main([*S, "target", "list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [(r["name"], r["where"]) for r in rows] == [
        ("staging", "sftp://s.example.com:staging.example.com"),
        ("local", f"local:{site / 'www'}"),
    ]
    assert main([*S, "target", "list"]) == 0
    assert "sftp://s.example.com:staging.example.com" in capsys.readouterr().out


def test_add_refusals_leave_site_toml_alone(site, capsys):
    S = ["--site-dir", str(site), "target", "add"]
    assert main([*S, "prod"]) == 2  # no --path, not a terminal
    assert main([*S, "prod", "--type", "sftp", "--path", "x"]) == 2  # sftp needs --host
    assert main([*S, "prod", "--host", "bad host!", "--path", "x"]) == 2  # validation
    assert main([*S, "bad/name", "--path", "x"]) == 2
    assert (site / "site.toml").read_text() == SITE
    assert main([*S, "prod", "--path", "x"]) == 0
    assert main([*S, "prod", "--path", "y"]) == 2  # exists
    assert "already exists" in capsys.readouterr().err


def test_add_interactive(site):
    answers = iter(["sftp", "h.example.com", "", "22", "web", "https://go.example.com"])
    asked: list[str] = []

    def ask(question, default):
        asked.append(question)
        return next(answers) or (default or "")

    spec = add_target(load_site(site), "prod", TargetOptions(), ask=ask)
    assert (spec.type, spec.host, spec.user, spec.path) == ("sftp", "h.example.com", None, "web")
    assert not any("PHP" in q for q in asked)  # sftp: no PHP question
    block = (site / "site.toml").read_text()[len(SITE) :]
    assert block == textwrap.dedent(
        """
        [deploy.targets.prod]
        type   = "sftp"
        host   = "h.example.com"
        path   = "web"
        accept = "release"
        """
    )


def test_dotted_target_name_is_quoted(site):
    spec = add_target(load_site(site), "eu.prod", TargetOptions(path="web"))
    assert spec.name == "eu.prod"
    assert '[deploy.targets."eu.prod"]' in (site / "site.toml").read_text()
