from pathlib import Path

from yourls_manager import clidoc

CLI_MD = Path(__file__).resolve().parents[1] / "docs" / "cli.md"


def test_cli_md_is_current():
    assert CLI_MD.read_text(encoding="utf-8") == clidoc.generate(), (
        "docs/cli.md is stale; run `python -m yourls_manager.clidoc`"
    )


def test_every_command_is_documented():
    text = clidoc.generate()
    for name in ("ym core", "ym plugin sync", "ym release promote", "ym target add", "ym ci setup"):
        assert f"## {name}\n" in text or f"### {name}\n" in text
    assert "[-h] [--site-dir PATH] [-v]" not in text
