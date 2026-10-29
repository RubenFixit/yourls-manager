"""CLI surface: deprecated command spellings (design §14)."""

from __future__ import annotations

import pytest

from yourls_manager.cli import build_parser, main


def _help(capsys) -> str:
    with pytest.raises(SystemExit):
        main(["--help"])
    return capsys.readouterr().out


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (["plugins", "prod"], {"command": "plugin", "plugin_cmd": "apply", "target": "prod"}),
        (["ssh-setup", "prod"], {"command": "target", "target_cmd": "login", "target": "prod"}),
        (
            ["server-config", "prod", "--from-existing"],
            {"command": "target", "target_cmd": "config", "from_existing": True},
        ),
        (
            ["sync", "--plugin", "a", "--plugin", "b", "--update"],
            {"command": "plugin", "plugin_cmd": "sync", "names": ["a", "b"], "update": True},
        ),
        (["status"], {"command": "doctor", "local": True, "target": None, "with_plugins": True}),
        (["check", "--json"], {"command": "update", "check": True, "json": True}),
        (["setup", "site"], {"command": "init"}),
        (
            ["template-update", "--defaults"],
            {"command": "template", "template_cmd": "update", "defaults": True},
        ),
    ],
)
def test_deprecated_alias_parses_as_new_command(old, new):
    args = build_parser().parse_args(old)
    for key, value in new.items():
        assert getattr(args, key) == value
    assert args.deprecated[0] == old[0]


def test_deprecated_aliases_hidden_from_help(capsys):
    out = _help(capsys)
    for old in ("plugins", "ssh-setup", "server-config", "sync", "status", "check", "setup",
                "template-update"):  # fmt: skip
        assert f"\n    {old} " not in out


def test_deprecated_alias_warns_and_runs(tmp_path, capsys):
    # no site.toml here: the new command runs and fails the same way (exit 2)
    assert main(["--site-dir", str(tmp_path), "plugins", "prod"]) == 2
    err = capsys.readouterr().err
    assert "`ym plugins` is deprecated" in err and "use `ym plugin apply`" in err


def test_core_version_positional_and_deprecated_flag():
    parse = build_parser().parse_args
    assert parse(["core", "latest"]).core_version == "latest"
    args = parse(["core", "--version", "1.10.6"])
    assert (args.core_version, args.core_version_flag) == (None, "1.10.6")
    assert parse(["plugin", "sync", "a", "--dry-run"]).names == ["a"]
    assert parse(["update", "--force"]).force is True


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
        (["release", "--list"], {"release_cmd": None, "list_flag": True}),  # deprecated
        (["release", "--promote"], {"release_cmd": None, "promote_flag": True}),  # deprecated
    ],
)
def test_release_parsing(argv, expect):
    args = build_parser().parse_args(argv)
    for key, value in expect.items():
        assert getattr(args, key) == value
