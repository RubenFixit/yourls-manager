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
    ],
)
def test_deprecated_alias_parses_as_new_command(old, new):
    args = build_parser().parse_args(old)
    for key, value in new.items():
        assert getattr(args, key) == value
    assert args.deprecated[0] == old[0]


def test_deprecated_aliases_hidden_from_help(capsys):
    out = _help(capsys)
    for old in ("plugins",):
        assert f"\n    {old} " not in out


def test_deprecated_alias_warns_and_runs(tmp_path, capsys):
    # no site.toml here: the new command runs and fails the same way (exit 2)
    assert main(["--site-dir", str(tmp_path), "plugins", "prod"]) == 2
    err = capsys.readouterr().err
    assert "`ym plugins` is deprecated" in err and "use `ym plugin apply`" in err
