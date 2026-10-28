from __future__ import annotations

import shlex

import pytest

from yourls_manager.config import TargetSpec
from yourls_manager.remote import (
    LocalRunner,
    SshRunner,
    runner_for,
    shell_join,
    to_home_relative,
)

NASTY = ["a b", "it's", 'say "hi"', "$HOME", "`id`", "x;rm -rf /", "*", "~/t", ""]


def test_shell_join_round_trips():
    script = shell_join(["printf", "%s\\n", *NASTY], env={"YOURLS_CONFIG_FILE": "/a b/c.php"})
    assert shlex.split(script) == [
        "env",
        "YOURLS_CONFIG_FILE=/a b/c.php",
        "printf",
        "%s\\n",
        *NASTY,
    ]


def test_local_runner_passes_arguments_verbatim(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    r = LocalRunner()
    out = r.run(["printf", "%s\\n", *NASTY]).stdout.decode()
    assert out.split("\n")[:-1] == NASTY


def test_local_runner_env_and_cwd_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    r = LocalRunner()
    assert r.run(["pwd"]).stdout.decode().strip() == str(tmp_path)
    out = r.run(["sh", "-c", 'printf %s "$X"'], env={"X": "v a l"}).stdout.decode()
    assert out == "v a l"


def test_local_paths_relative_to_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    r = LocalRunner()
    assert r.abspath("site/www") == f"{tmp_path}/site/www"
    assert r.abspath("~/site") == f"{tmp_path}/site"
    assert r.abspath("/srv/www/") == "/srv/www"
    assert r.rsync_dest("www") == f"{tmp_path}/www"
    assert r.rsync_transport() == []


def test_write_file_and_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    r = LocalRunner()
    r.mkdir("dir with space/sub")
    r.write_file("dir with space/sub/f.json", b'{"a": 1}\n', "0600")
    p = tmp_path / "dir with space/sub/f.json"
    assert p.read_bytes() == b'{"a": 1}\n'
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    assert r.exists("dir with space/sub/f.json", "-f")
    assert not r.exists("nope")


def test_to_home_relative():
    assert to_home_relative("~/x/y") == "x/y"
    assert to_home_relative("~") == "."
    assert to_home_relative("/abs") == "/abs"
    assert to_home_relative("rel") == "rel"


def test_ssh_runner_argv_and_rsync():
    r = SshRunner("example.dreamhost.com", "ruben", 2222)
    r._home = "/home/ruben"
    argv = r.ssh_argv(shell_join(["test", "-f", "yourls-config/x.php"]))
    assert argv[:3] == ["ssh", "-p", "2222"]
    assert "BatchMode=yes" in argv
    assert argv[-3:] == ["ruben@example.dreamhost.com", "--", "test -f yourls-config/x.php"]
    assert (
        r.rsync_dest("go.example.com") == "ruben@example.dreamhost.com:/home/ruben/go.example.com"
    )
    transport = r.rsync_transport()
    assert transport[0] == "-e" and shlex.split(transport[1])[:3] == ["ssh", "-p", "2222"]


def test_ssh_runner_quotes_remote_command(monkeypatch):
    r = SshRunner("h")
    seen = {}

    class P:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return P()

    monkeypatch.setattr("yourls_manager.remote.subprocess.run", fake_run)
    r.run(
        ["php", "/home/u/.yourls-manager/ym-helper-0.1.0.php", "plugins", "--active=a b"],
        env={"YOURLS_CONFIG_FILE": "/home/u/c.php"},
    )
    remote_cmd = seen["argv"][-1]
    assert seen["argv"][-2] == "--"
    assert shlex.split(remote_cmd) == [
        "env",
        "YOURLS_CONFIG_FILE=/home/u/c.php",
        "php",
        "/home/u/.yourls-manager/ym-helper-0.1.0.php",
        "plugins",
        "--active=a b",
    ]


@pytest.mark.parametrize(("host", "cls"), [(None, LocalRunner), ("h.example", SshRunner)])
def test_runner_for(host, cls):
    spec = TargetSpec(name="t", type="rsync", path="www", host=host)
    assert isinstance(runner_for(spec), cls)
