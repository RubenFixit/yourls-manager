"""`ym setup` wizard (design §9), driven with scripted answers."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from yourls_manager.cli import main
from yourls_manager.wizard import run_wizard

PROD = """
[deploy.targets.prod]
host = "example.dreamhost.com"
user = "ruben"
path = "go.example.com"
"""


class Script:
    def __init__(self, answers: list[str]):
        self.answers = list(answers)
        self.questions: list[str] = []

    def __call__(self, question: str, default: str | None) -> str:
        self.questions.append(question)
        assert self.answers, f"unexpected question: {question}"
        return self.answers.pop(0)


class FakeYm:
    """Records argv; `new` writes a minimal site.toml (with or without a prod target)."""

    def __init__(self, prod: bool, fail: str | None = None):
        self.calls: list[list[str]] = []
        self.prod = prod
        self.fail = fail

    def __call__(self, argv) -> int:
        argv = list(argv)
        self.calls.append(argv)
        cmd = next(a for a in argv if not a.startswith("-") and not a.startswith("/"))
        if cmd == self.fail:
            return 1
        if argv[0] == "new":
            dest = Path(argv[1])
            dest.mkdir(parents=True)
            (dest / "site.toml").write_text(
                textwrap.dedent(
                    """\
                    [site]
                    name = "T"
                    slug = "t"
                    url = "https://go.example.com"
                    [core]
                    version = "1.10.6"
                    """
                )
                + (PROD if self.prod else "")
            )
        return 0


def test_new_site_flow(tmp_path, capsys):
    ym = FakeYm(prod=True)
    ask = Script(["new", "mysite", "y", "y"])
    res = run_wizard(ask, invoke=ym, config_exists=lambda s, t: False, cwd=tmp_path)
    site = str(tmp_path / "mysite")
    S = ["--site-dir", site]
    assert ym.calls == [
        ["new", site],
        [*S, "server-config", "prod"],
        [*S, "deploy", "prod", "--dry-run"],
    ]
    assert res.exit_code == 0 and not ask.answers
    out = capsys.readouterr().out
    assert f"$ ym new {site}" in out and "$ ym --site-dir" in out
    assert f"ym --site-dir {site} deploy prod" in out.split("still to do:")[1]


def test_adopt_from_prod(tmp_path, capsys):
    ym = FakeYm(prod=True)
    ask = Script(["adopt", "s", "", "yes", "n"])
    res = run_wizard(ask, invoke=ym, config_exists=lambda s, t: False, cwd=tmp_path)
    site = str(tmp_path / "s")
    S = ["--site-dir", site]
    assert ym.calls == [
        ["new", site, "--data", "include_example_plugins=false"],
        [*S, "adopt", "--from-target", "prod"],
        [*S, "server-config", "prod", "--from-existing"],
    ]
    assert "ruben@example.dreamhost.com:go.example.com" in ask.questions[2]
    remaining = "\n".join(res.remaining)
    assert "adopt-review" in remaining and "deploy prod --dry-run" in remaining
    assert "core --version latest" in remaining


def test_adopt_local_dir_without_prod(tmp_path):
    ym = FakeYm(prod=False)
    install = tmp_path / "old-install"
    ask = Script(["adopt", "s", str(install)])
    res = run_wizard(ask, invoke=ym, cwd=tmp_path)
    site = str(tmp_path / "s")
    assert ym.calls[1] == ["--site-dir", site, "adopt", "--from-dir", str(install)]
    assert len(ym.calls) == 2  # no prod target: server-config/deploy are left to do
    assert any("[deploy.targets.prod]" in r for r in res.remaining)


def test_adopt_stop_to_configure_prod(tmp_path):
    ym = FakeYm(prod=False)
    res = run_wizard(Script(["adopt", "s", ""]), invoke=ym, cwd=tmp_path)
    assert len(ym.calls) == 1
    assert any("adopt --from-target prod" in r for r in res.remaining)


def test_config_exists_skips_server_config(tmp_path):
    ym = FakeYm(prod=True)
    run_wizard(Script(["new", "s", "n"]), invoke=ym, config_exists=lambda s, t: True,
               cwd=tmp_path)  # fmt: skip
    assert [c for c in ym.calls if "server-config" in c] == []


def test_invalid_answers_reprompt(tmp_path):
    ym = FakeYm(prod=True)
    ask = Script(["what", "new", "s", "maybe", "n", "n"])
    run_wizard(ask, invoke=ym, config_exists=lambda s, t: False, cwd=tmp_path)
    assert len(ym.calls) == 1 and not ask.answers


def test_failing_new_stops(tmp_path):
    ym = FakeYm(prod=True, fail="new")
    res = run_wizard(Script(["new", "s"]), invoke=ym, cwd=tmp_path)
    assert res.exit_code == 1 and len(ym.calls) == 1
    assert "re-run" in res.remaining[0]


def test_setup_requires_tty(capsys):
    assert main(["setup"]) == 2
    assert "terminal" in capsys.readouterr().err


@pytest.mark.parametrize("cmd", ["new", "template-update", "server-config", "adopt", "setup"])
def test_help(cmd, capsys):
    with pytest.raises(SystemExit) as exc:
        main([cmd, "--help"])
    assert exc.value.code == 0
    assert f"ym {cmd}" in capsys.readouterr().out
