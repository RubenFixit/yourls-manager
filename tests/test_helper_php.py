"""Tests for php/ym-helper.php. Skipped when no php CLI is available.

The ``mysql`` test runs only when YM_TEST_YOURLS_ROOT (an installed YOURLS web root) and
YM_TEST_YOURLS_CONFIG (its config file) are set.
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from yourls_manager.deploy.rsync import helper_source

from .conftest import needs_php, write_files

pytestmark = [pytest.mark.php, needs_php]

HELPER = Path(str(helper_source()))


def helper(*args: str, env: dict | None = None, cwd: Path | None = None):
    full_env = dict(os.environ, **(env or {}))
    proc = subprocess.run(
        ["php", str(HELPER), *args], capture_output=True, text=True, env=full_env, cwd=cwd
    )
    lines = proc.stdout.strip().splitlines()
    assert len(lines) == 1, f"stdout must be exactly one JSON line, got: {proc.stdout!r}"
    return proc.returncode, json.loads(lines[0]), proc.stderr


def test_lint():
    out = subprocess.run(["php", "-l", str(HELPER)], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr


def test_version_and_usage():
    from yourls_manager import __version__

    rc, data, _ = helper("version")
    assert rc == 0 and data["ok"] is True and data["helper_version"] == __version__
    rc, data, _ = helper()
    assert rc == 1 and data["ok"] is False and "usage" in data["error"]
    rc, data, _ = helper("frobnicate")
    assert rc == 1 and "unknown command" in data["error"]
    rc, data, _ = helper("info", "positional")
    assert rc == 1 and "unexpected argument" in data["error"]


def test_info_requires_root(tmp_path):
    rc, data, _ = helper("info", f"--root={tmp_path}")
    assert rc == 1 and "not a YOURLS root" in data["error"]
    rc, data, _ = helper("info")
    assert rc == 1 and "--root" in data["error"]


FAKE_YOURLS = {
    "user/config.php": """\
        <?php
        define('YOURLS_SITE', 'https://go.example.com');
        $yourls_user_passwords = ['a' => 'b'];
        """,
    "includes/load-yourls.php": """\
        <?php
        // Minimal stand-in for YOURLS' bootstrap, enough for the helper's info command.
        if (getenv('FAKE_EXIT')) { echo "<html>redirecting to install.php</html>"; exit(); }
        require_once YOURLS_CONFIGFILE;
        define('YOURLS_VERSION', '1.10.6');
        define('YOURLS_DB_VERSION', '507');
        define('YOURLS_PLUGINDIR', __DIR__ . '/../user/plugins');
        echo "some chatter that must not reach stdout\\n";
        function yourls_is_installed() { return getenv('FAKE_INSTALLED') === '1'; }
        function yourls_get_current_version_from_sql() { return ['1.9.2', '506']; }
        function yourls_get_option($name) {
            return $name === 'active_plugins' ? ['kqs/plugin.php', 'b/plugin.php'] : null;
        }
        global $yourls_user_passwords;
        if (!is_array($yourls_user_passwords)) { throw new Exception('config not global'); }
        if ($_SERVER['HTTP_HOST'] !== 'go.example.com' || ($_SERVER['HTTPS'] ?? '') !== 'on') {
            throw new Exception('fake request env not set');
        }
        """,
}


def test_info_with_fake_yourls(tmp_path):
    write_files(tmp_path, FAKE_YOURLS)
    rc, data, err = helper("info", f"--root={tmp_path}", env={"FAKE_INSTALLED": "1"})
    assert rc == 0, data
    assert data == {
        "ok": True,
        "command": "info",
        "php": data["php"],
        "yourls_version": "1.10.6",
        "db_version_code": 507,
        "db_version_installed": 506,
        "installed": True,
        "upgrade_needed": True,
        "active_plugins": ["kqs", "b"],
    }
    assert "some chatter" in err

    rc, data, _ = helper("info", f"--root={tmp_path}", env={"FAKE_INSTALLED": "0"})
    assert rc == 0 and data["installed"] is False and data["upgrade_needed"] is False


def test_shutdown_guard_reports_premature_exit(tmp_path):
    write_files(tmp_path, FAKE_YOURLS)
    rc, data, _ = helper("upgrade", f"--root={tmp_path}", env={"FAKE_EXIT": "1"})
    assert rc == 1 and data["ok"] is False
    assert "exited prematurely during YOURLS bootstrap" in data["error"]
    assert "redirecting to install.php" in data["output"]


def test_dump_validation(tmp_path):
    rc, data, _ = helper("dump", f"--out={tmp_path}/x.sql.gz")
    assert rc == 1 and "--config" in data["error"]
    rc, data, _ = helper("dump", f"--config={tmp_path}/none.php", f"--out={tmp_path}/x.sql.gz")
    assert rc == 1 and "not readable" in data["error"]
    cfg = tmp_path / "c.php"
    cfg.write_text("<?php define('YOURLS_DB_USER', 'u');\n")
    rc, data, _ = helper("dump", f"--config={cfg}", f"--out={tmp_path}/x.sql.gz")
    assert rc == 1 and "YOURLS_DB_PASS is not defined" in data["error"]


def test_dump_reports_connection_failure(tmp_path):
    cfg = tmp_path / "c.php"
    cfg.write_text(
        textwrap.dedent(
            """\
            <?php
            define('YOURLS_DB_USER', 'u');
            define('YOURLS_DB_PASS', 'p');
            define('YOURLS_DB_NAME', 'n');
            define('YOURLS_DB_HOST', '127.0.0.1:1');
            """
        )
    )
    rc, data, _ = helper("dump", f"--config={cfg}", f"--out={tmp_path}/b/x.sql.gz")
    assert rc == 1
    assert "database" in data["error"] or "mysqldump" in data["error"]
    assert not (tmp_path / "b/x.sql.gz").exists()


@pytest.mark.mysql
@pytest.mark.skipif(
    not (os.environ.get("YM_TEST_YOURLS_ROOT") and os.environ.get("YM_TEST_YOURLS_CONFIG")),
    reason="set YM_TEST_YOURLS_ROOT and YM_TEST_YOURLS_CONFIG to run against a real YOURLS",
)
def test_real_yourls_info_and_dump(tmp_path):
    root = os.environ["YM_TEST_YOURLS_ROOT"]
    config = os.environ["YM_TEST_YOURLS_CONFIG"]
    env = {"YOURLS_CONFIG_FILE": config}
    rc, data, _ = helper("info", f"--root={root}", env=env)
    assert rc == 0 and data["installed"] is True
    rc, data, _ = helper("upgrade", f"--root={root}", env=env)
    assert rc == 0 and data["ok"] is True
    rc, before, _ = helper("info", f"--root={root}", env=env)
    active = ",".join(before["active_plugins"])
    rc, data, _ = helper("plugins", f"--root={root}", f"--active={active}", env=env)
    assert rc == 0 and data["activated"] == [] and data["deactivated"] == []
    out = tmp_path / "d/dump.sql.gz"
    rc, data, _ = helper("dump", f"--config={config}", f"--out={out}")
    assert rc == 0 and out.stat().st_size == data["bytes"]
