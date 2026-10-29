"""`ym init` wizard (design §9), driven with scripted answers."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from yourls_manager.cli import main
from yourls_manager.wizard import InitState, run_wizard


@pytest.fixture(autouse=True)
def _isolated_wizard(monkeypatch, tmp_path):
    """No real ssh checks; setup state goes to a temp dir."""
    monkeypatch.setitem(run_wizard.__kwdefaults__, "ssh_ok", lambda s, t: True)
    monkeypatch.setitem(run_wizard.__kwdefaults__, "home", tmp_path / "state")


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
        words = [a for a in argv if not a.startswith("-") and not a.startswith("/")]
        cmd = " ".join(words[:2]) if words[0] == "target" else words[0]
        if cmd == self.fail:
            return 1
        if cmd == "target add":
            site = Path(argv[argv.index("--site-dir") + 1])
            with (site / "site.toml").open("a") as f:
                f.write(PROD)
        if argv[0] == "new":
            dest = Path(argv[1])
            dest.mkdir(parents=True)
            (dest / ".git").mkdir()  # real `ym new` runs git init
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
        [*S, "target", "config", "prod"],
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
        [*S, "target", "config", "prod", "--from-existing"],
    ]
    assert "ruben@example.dreamhost.com:go.example.com" in ask.questions[2]
    remaining = "\n".join(res.remaining)
    assert "adopt-review" in remaining and "deploy prod --dry-run" in remaining
    assert "update --commit" in remaining


def test_adopt_local_dir_without_prod(tmp_path):
    ym = FakeYm(prod=False)
    install = tmp_path / "old-install"
    ask = Script(["adopt", "s", "n", str(install)])  # add prod target? no
    res = run_wizard(ask, invoke=ym, cwd=tmp_path)
    site = str(tmp_path / "s")
    assert ym.calls[1] == ["--site-dir", site, "adopt", "--from-dir", str(install)]
    assert len(ym.calls) == 2  # no prod target: target config/deploy are left to do
    assert any("target add prod" in r for r in res.remaining)


def test_adopt_stop_to_configure_prod(tmp_path):
    ym = FakeYm(prod=False)
    res = run_wizard(Script(["adopt", "s", "n", ""]), invoke=ym, cwd=tmp_path)
    assert len(ym.calls) == 1
    assert res.resumable and any("ym init" in r for r in res.remaining)
    assert any("target add prod" in r for r in res.remaining)


def test_new_site_adds_missing_prod_target(tmp_path):
    ym = FakeYm(prod=False)
    # add prod? yes; target config? no; dry run? no
    res = run_wizard(Script(["new", "s", "y", "n", "n"]), invoke=ym, cwd=tmp_path)
    site = str(tmp_path / "s")
    assert ym.calls[1] == ["--site-dir", site, "target", "add", "prod"]
    assert res.exit_code == 0


def test_config_exists_skips_server_config(tmp_path):
    ym = FakeYm(prod=True)
    run_wizard(Script(["new", "s", "n"]), invoke=ym, config_exists=lambda s, t: True,
               cwd=tmp_path)  # fmt: skip
    assert [c for c in ym.calls if "config" in c] == []


def test_invalid_answers_reprompt(tmp_path):
    ym = FakeYm(prod=True)
    ask = Script(["what", "new", "s", "maybe", "n"])
    run_wizard(ask, invoke=ym, config_exists=lambda s, t: False, cwd=tmp_path)
    assert len(ym.calls) == 1 and not ask.answers


def test_failing_new_stops(tmp_path):
    ym = FakeYm(prod=True, fail="new")
    res = run_wizard(Script(["new", "s"]), invoke=ym, cwd=tmp_path)
    assert res.exit_code == 1 and len(ym.calls) == 1
    assert "re-run" in res.remaining[0]


def test_init_requires_tty(capsys):
    assert main(["init"]) == 2
    assert "terminal" in capsys.readouterr().err


@pytest.mark.parametrize(
    "cmd", ["new", "template-update", "target config", "adopt", "init", "target login"]
)
def test_help(cmd, capsys):
    with pytest.raises(SystemExit) as exc:
        main([*cmd.split(), "--help"])
    assert exc.value.code == 0
    assert f"ym {cmd}" in capsys.readouterr().out


# --------------------------------------------------------------------------- resume


def test_adopt_failure_then_resume(tmp_path, capsys):
    ym = FakeYm(prod=True, fail="adopt")
    res = run_wizard(Script(["adopt", "s", "y"]), invoke=ym, cwd=tmp_path)
    site = tmp_path / "s"
    assert res.exit_code == 1 and res.resumable
    assert "run `ym init` again to resume" in capsys.readouterr().out
    assert InitState(site, tmp_path / "state").data["completed"] == ["new"]

    # second run from an unrelated directory: offered via the remembered pointer
    ym2 = FakeYm(prod=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    ask = Script(["", "y", "y", "n"])  # resume? adopt prod? target config? dry run?
    res = run_wizard(ask, invoke=ym2, config_exists=lambda s, t: False, cwd=elsewhere)
    S = ["--site-dir", str(site)]
    assert ym2.calls == [
        [*S, "adopt", "--from-target", "prod"],
        [*S, "target", "config", "prod", "--from-existing"],
    ]
    assert res.exit_code == 0 and not ask.answers
    st = InitState(site, tmp_path / "state")
    assert st.data["finished"] and st.data["completed"] == ["new", "adopt"]
    assert not (tmp_path / "state" / "init.json").exists()  # pointer cleared when finished


def test_resume_skips_completed_adopt(tmp_path, capsys):
    ym = FakeYm(prod=True, fail="target config")
    res = run_wizard(Script(["adopt", "s", "y", "y"]), invoke=ym,
                     config_exists=lambda s, t: False, cwd=tmp_path)  # fmt: skip
    assert res.exit_code == 1
    site = tmp_path / "s"
    ym2 = FakeYm(prod=True)
    res = run_wizard(Script(["y", "n"]), invoke=ym2, config_exists=lambda s, t: False,
                     cwd=tmp_path, start=site)  # fmt: skip
    assert ym2.calls == [["--site-dir", str(site), "target", "config", "prod", "--from-existing"]]
    assert "already done: adopt" in capsys.readouterr().out


def test_declining_resume_starts_fresh(tmp_path):
    ym = FakeYm(prod=True, fail="adopt")
    run_wizard(Script(["adopt", "s", "y"]), invoke=ym, cwd=tmp_path)
    ym2 = FakeYm(prod=True)
    ask = Script(["n", "new", "t", "n", "n"])
    run_wizard(ask, invoke=ym2, config_exists=lambda s, t: False, cwd=tmp_path)
    assert ym2.calls[0] == ["new", str(tmp_path / "t")]


def test_start_dir_must_be_site(tmp_path):
    from yourls_manager.util import YMError

    with pytest.raises(YMError):
        run_wizard(Script([]), invoke=FakeYm(prod=True), cwd=tmp_path, start=tmp_path)


def test_existing_dest_is_reused(tmp_path):
    ym = FakeYm(prod=True)
    run_wizard(Script(["new", "s", "n", "n"]), invoke=ym, config_exists=lambda s, t: True,
               cwd=tmp_path)  # fmt: skip
    # state finished; a second fresh run pointing at the same dir does not re-run `ym new`
    ym2 = FakeYm(prod=True)
    run_wizard(Script(["new", "s", "n"]), invoke=ym2, config_exists=lambda s, t: True,
               cwd=tmp_path)  # fmt: skip
    assert all(c[0] != "new" for c in ym2.calls)


# --------------------------------------------------------------------------- ssh step


def test_ssh_setup_step_when_key_login_fails(tmp_path):
    ym = FakeYm(prod=True)
    ask = Script(["new", "s", "y", "n"])  # set up key? dry run?
    res = run_wizard(ask, invoke=ym, config_exists=lambda s, t: True,
                     ssh_ok=lambda s, t: False, cwd=tmp_path)  # fmt: skip
    site = str(tmp_path / "s")
    assert ym.calls[1] == ["--site-dir", site, "target", "login", "prod"]
    assert res.exit_code == 0


def test_ssh_setup_declined_is_resumable(tmp_path):
    ym = FakeYm(prod=True)
    res = run_wizard(Script(["new", "s", "n"]), invoke=ym, ssh_ok=lambda s, t: False,
                     cwd=tmp_path)  # fmt: skip
    assert res.resumable and any("target login prod" in r for r in res.remaining)
    assert len(ym.calls) == 1


def test_legacy_setup_progress_is_resumed_and_migrated(tmp_path, capsys):
    """Progress saved by `ym setup` (< 0.7) is picked up by `ym init`."""
    import json

    ym = FakeYm(prod=True, fail="adopt")
    run_wizard(Script(["adopt", "s", "y"]), invoke=ym, cwd=tmp_path)
    site, home = tmp_path / "s", tmp_path / "state"
    # rewrite the new-style files as the old names
    (site / ".git" / "ym-init.json").rename(site / ".git" / "ym-setup.json")
    (home / "init.json").rename(home / "setup.json")
    st = InitState(site, home)
    assert st.mode == "adopt" and st.is_done("new")
    st.mark("adopt")
    assert (site / ".git" / "ym-init.json").is_file()
    assert not (site / ".git" / "ym-setup.json").exists() and not (home / "setup.json").exists()
    assert json.loads((home / "init.json").read_text())["site"] == str(site)
