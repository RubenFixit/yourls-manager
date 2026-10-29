"""`ym gh-setup` (design §13) against a fake `gh` on PATH."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

from yourls_manager import ghsetup
from yourls_manager.cli import main
from yourls_manager.config import load_site
from yourls_manager.util import UsageError

from .conftest import write_files

FAKE_GH = r"""
import json, os, sys
state_file = os.environ["FAKE_GH_STATE"]
state = json.load(open(state_file))
args = sys.argv[1:]
stdin = "" if sys.stdin.isatty() else sys.stdin.read()
state["calls"].append(args)
def save():
    json.dump(state, open(state_file, "w"))
def done(code=0, out=""):
    save(); sys.stdout.write(out); sys.exit(code)
if args[:2] == ["auth", "status"]:
    done(0 if state["logged_in"] else 1)
if args[:2] == ["auth", "login"]:
    state["logged_in"] = True; done()
if args[:2] == ["repo", "view"]:
    done(0, state["repo"] + "\n") if state["repo"] else done(1)
if args[0] == "api":
    path = args[-1]
    env = path.rsplit("/", 1)[-1]
    if "--method" in args:
        state["envs"].setdefault(env, {}); done(0, "{}")
    done(0 if env in state["envs"] else 1, "{}")
if args[:2] == ["secret", "list"]:
    env = args[args.index("--env") + 1]
    done(0, json.dumps([{"name": n} for n in state["envs"].get(env, {})]))
if args[:2] == ["secret", "set"]:
    env = args[args.index("--env") + 1]
    state["envs"][env][args[2]] = stdin; done()
done(2)
"""


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "gh"
    script.write_text(f"#!{sys.executable}\n{FAKE_GH}")
    script.chmod(0o755)
    state = tmp_path / "gh-state.json"
    state.write_text(json.dumps({"logged_in": True, "repo": "me/site", "envs": {}, "calls": []}))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_STATE", str(state))

    class S:
        def load(self):
            return json.loads(state.read_text())

        def update(self, **kw):
            d = self.load()
            d.update(kw)
            state.write_text(json.dumps(d))

    return S()


@pytest.fixture
def site(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "site"
    write_files(root, {
        "site.toml": """
            [site]
            name = "T"
            slug = "t"
            url = "https://go.example.com"
            [core]
            version = "1.0.0"
            [deploy.targets.prod]
            host = "web.example.com"
            user = "u"
            path = "www"
            [deploy.targets.staging]
            host = "stage.example.com"
            port = 2222
            path = "www"
            [deploy.targets.dev]
            host = "dev.example.com"
            path = "www"
            [deploy.targets.box]
            path = "/srv/www"
            """,
        ".github/workflows/deploy.yml": "name: deploy\n",
    })  # fmt: skip
    # host keys: prod is known (hashed entry), staging isn't
    key = home / "hostkey"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    pub = (home / "hostkey.pub").read_text().split()
    (home / ".ssh/known_hosts").write_text(f"web.example.com {pub[0]} {pub[1]}\n")
    subprocess.run(["ssh-keygen", "-q", "-H", "-f", str(home / ".ssh/known_hosts")], check=True)
    return root


@pytest.fixture
def remote(monkeypatch):
    """Record key installs instead of touching a server."""
    import yourls_manager.sshsetup as sshsetup

    installed: list[tuple[str, str]] = []
    monkeypatch.setattr(sshsetup, "check_ssh", lambda spec, *a, **k: sshsetup.SshCheck(True, "ok"))
    monkeypatch.setattr(ghsetup, "install_public_key",
                        lambda spec, pub: installed.append((spec.name, pub)))  # fmt: skip
    monkeypatch.setattr(ghsetup, "check_key", lambda spec, key: None)
    monkeypatch.setattr(ghsetup, "scan_host_keys", lambda spec: [])
    return installed


def test_environment_mapping():
    assert ghsetup.environment_for("prod") == "production"
    assert ghsetup.environment_for("staging") == "staging"
    assert ghsetup.environment_for("eu") == "staging"


def test_gh_setup_prod(site, fake_gh, remote, capsys):
    S = ["--site-dir", str(site)]
    assert main([*S, "gh-setup", "prod"]) == 0
    st = fake_gh.load()
    secrets = st["envs"]["production"]
    assert secrets["DEPLOY_SSH_KEY"].startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    hosts = secrets["DEPLOY_KNOWN_HOSTS"]
    assert hosts.startswith("|1|") and "ssh-ed25519" in hosts  # hashed line kept as is
    assert len(remote) == 1 and remote[0][0] == "prod"
    assert remote[0][1].startswith("ssh-ed25519 ") and "ym-ci-t-production-" in remote[0][1]
    assert ["api", "--method", "PUT", "repos/me/site/environments/production"] in st["calls"]
    out = capsys.readouterr().out
    assert "created environment production" in out

    # second run keeps the key, refreshes known hosts
    old_key = secrets["DEPLOY_SSH_KEY"]
    assert main([*S, "gh-setup", "prod"]) == 0
    assert fake_gh.load()["envs"]["production"]["DEPLOY_SSH_KEY"] == old_key
    assert len(remote) == 1
    assert "already set; kept" in capsys.readouterr().out
    assert main([*S, "gh-setup", "prod", "--rotate"]) == 0
    assert fake_gh.load()["envs"]["production"]["DEPLOY_SSH_KEY"] != old_key
    assert len(remote) == 2
    assert "previous CI key is still authorized" in capsys.readouterr().out


def test_gh_setup_dry_run_and_unknown_host(site, fake_gh, remote, monkeypatch, capsys):
    S = ["--site-dir", str(site)]
    assert main([*S, "gh-setup", "--dry-run", "prod"]) == 0
    assert fake_gh.load()["envs"] == {}
    assert "would generate and install a new CI key" in capsys.readouterr().out
    # default = prod + staging; staging's host isn't known and nobody can confirm (no tty)
    pub = (site.parent / "home/hostkey.pub").read_text().split()
    monkeypatch.setattr(ghsetup, "scan_host_keys", lambda spec: [f"x {pub[0]} {pub[1]}"])
    assert main([*S, "gh-setup"]) == 2
    err = capsys.readouterr()
    assert "skipping target(s) box, dev" in err.out and "SHA256:" in err.out
    assert "ym ssh-setup staging" in err.err
    assert fake_gh.load()["envs"] == {}  # nothing changed, prod included


def test_pick_targets(site, fake_gh):
    cfg = load_site(site)
    with pytest.raises(UsageError, match="no host"):
        ghsetup.pick_targets(cfg, ["box"])
    with pytest.raises(UsageError, match="'staging' environment"):
        ghsetup.pick_targets(cfg, ["staging", "dev"])
    assert [p.env for p in ghsetup.pick_targets(cfg, ["dev"])] == ["staging"]


def test_gh_login_prompt(site, fake_gh, remote, capsys):
    fake_gh.update(logged_in=False)
    assert main(["--site-dir", str(site), "gh-setup", "prod"]) == 2  # no terminal
    assert "gh auth login" in capsys.readouterr().err
    calls = []

    def interactive(argv):
        calls.append(argv)
        return subprocess.run(argv, check=False).returncode

    ghsetup.gh_setup(load_site(site), ["prod"], confirm=lambda q: True, interactive=interactive)
    assert calls == [["gh", "auth", "login"]]
    assert "DEPLOY_SSH_KEY" in fake_gh.load()["envs"]["production"]


def test_no_gh(site, monkeypatch, capsys):
    monkeypatch.setenv("PATH", "/nonexistent")
    assert main(["--site-dir", str(site), "gh-setup", "prod"]) == 2
    assert "cli.github.com" in capsys.readouterr().err


def test_no_repo(site, fake_gh, remote, capsys):
    fake_gh.update(repo="")
    assert main(["--site-dir", str(site), "gh-setup", "prod"]) == 2
    assert "gh repo create" in capsys.readouterr().err


def test_authorized_keys_script(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path)}
    line = "ssh-ed25519 AAAAC3Nz test-key"
    for _ in range(2):
        subprocess.run(["sh", "-c", ghsetup.AUTHORIZED_KEYS_SCRIPT, "sh", line], env=env,
                       check=True)  # fmt: skip
    ak = tmp_path / ".ssh/authorized_keys"
    assert ak.read_text() == line + "\n"
    assert oct(ak.stat().st_mode & 0o777) == "0o600"
    assert oct((tmp_path / ".ssh").stat().st_mode & 0o777) == "0o700"


def test_real_key_and_fingerprints(tmp_path):
    key = ghsetup.generate_key(tmp_path, "ym-ci-x")
    assert key.public.endswith(" ym-ci-x")
    fp = ghsetup.fingerprints([key.public])
    assert fp and "SHA256:" in fp[0]
    assert textwrap.dedent(key.private.read_text()).startswith("-----BEGIN OPENSSH")
