"""CLI surface: deprecated command spellings (design §12)."""

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
    ],
)
def test_deprecated_alias_parses_as_new_command(old, new):
    args = build_parser().parse_args(old)
    for key, value in new.items():
        assert getattr(args, key) == value
    assert args.deprecated[0] == old[0]


def test_deprecated_aliases_hidden_from_help(capsys):
    out = _help(capsys)
    for old in ("plugins", "ssh-setup", "server-config", "sync"):
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
