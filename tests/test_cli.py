"""CLI surface: command spellings and release parsing."""

from __future__ import annotations

import pytest

from yourls_manager.cli import build_parser, main


def _help(capsys) -> str:
    with pytest.raises(SystemExit):
        main(["--help"])
    return capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["plugins", "prod"],
        ["ssh-setup", "prod"],
        ["server-config", "prod"],
        ["sync"],
        ["status"],
        ["check"],
        ["setup"],
        ["template-update"],
        ["core", "--version", "1.10.6"],
        ["release", "--list"],
        ["release", "--promote"],
    ],
)
def test_spellings_removed_in_0_8(argv, capsys):
    """The 0.7 spellings are gone: argparse rejects them (exit 2)."""
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "invalid choice" in err or "unrecognized arguments" in err


def test_help_lists_command_groups(capsys):
    out = _help(capsys)
    for cmd in ("artifact", "target", "plugin", "release", "template", "init"):
        assert f"\n    {cmd} " in out


def test_new_spellings_parse():
    parse = build_parser().parse_args
    assert parse(["core", "latest"]).core_version == "latest"
    assert parse(["plugin", "sync", "a", "--dry-run"]).names == ["a"]
    assert parse(["update", "--force"]).force is True
    assert parse(["update", "--check", "--json"]).json is True
    assert parse(["doctor", "--local"]).local is True
    assert parse(["init", "site"]).dir is not None


def test_update_check_rejects_update_options(tmp_path, capsys):
    (tmp_path / "site.toml").write_text(
        '[site]\nname = "T"\nslug = "t"\nurl = "https://x.example"\n[core]\nversion = "1.0.0"\n'
    )
    S = ["--site-dir", str(tmp_path)]
    assert main([*S, "update", "--check", "--commit"]) == 2
    assert main([*S, "update", "--json"]) == 2
    assert main([*S, "doctor", "prod", "--local"]) == 2


@pytest.mark.parametrize(
    ("argv", "expect"),
    [
        (["release"], {"release_cmd": None, "rc": False, "push": False}),
        (["release", "--rc", "--push", "-m", "x"], {"rc": True, "push": True, "message": "x"}),
        (["release", "promote", "--push"], {"release_cmd": "promote", "push": True}),
        # options before the action are kept (subparser defaults are suppressed)
        (["release", "--push", "--dry-run", "promote"], {"push": True, "dry_run": True}),
        (["release", "list"], {"release_cmd": "list"}),
    ],
)
def test_release_parsing(argv, expect):
    args = build_parser().parse_args(argv)
    for key, value in expect.items():
        assert getattr(args, key) == value
