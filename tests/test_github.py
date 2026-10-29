"""`ym repo create` and `ym ci setup|status` (design §13) against a fake `gh` on PATH."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

from yourls_manager import ci, github
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
    if len(args) > 2 and not args[2].startswith("-"):
        done(0 if args[2] in state["repos"] else 1, "{}")
    done(0, state["repo"] + "\n") if state["repo"] else done(1)
if args[:2] == ["repo", "create"]:
    import subprocess
    name = args[2]
    bare = os.path.join(state["gh_root"], name + ".git")
    os.makedirs(os.path.dirname(bare), exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
    state["repos"].append(name)
    state["created"] = args
    if "--source" in args:
        src, remote = args[args.index("--source") + 1], args[args.index("--remote") + 1]
        subprocess.run(["git", "-C", src, "remote", "add", remote,
                        f"https://github.com/{name}.git"], check=True)
    done()
if args[:3] == ["api", "user", "-q"]:
    done(0, state["login"] + "\n")
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
    gh_root = tmp_path / "github"
    state.write_text(json.dumps({"logged_in": True, "repo": "me/site", "envs": {}, "calls": [],
                                 "repos": ["me/site"], "login": "me",
                                 "gh_root": str(gh_root)}))  # fmt: skip
    # pushes to https://github.com/<repo> land in local bare repos
    gcfg = os.environ["GIT_CONFIG_GLOBAL"]
    with open(gcfg, "a") as fh:
        fh.write(f'[url "{gh_root.as_uri()}/"]\n\tinsteadOf = https://github.com/\n')
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
    monkeypatch.setattr(ci, "install_public_key",
                        lambda spec, pub: installed.append((spec.name, pub)))  # fmt: skip
    monkeypatch.setattr(ci, "check_key", lambda spec, key: None)
    monkeypatch.setattr(ci, "scan_host_keys", lambda spec: [])
    return installed


def test_environment_mapping():
    assert ci.environment_for("prod") == "production"
    assert ci.environment_for("staging") == "staging"
    assert ci.environment_for("eu") == "staging"


def test_ci_setup_prod(site, fake_gh, remote, capsys):
    S = ["--site-dir", str(site)]
    assert main([*S, "ci", "setup", "prod"]) == 0
    st = fake_gh.load()
    secrets = st["envs"]["production"]
    assert secrets["DEPLOY_SSH_KEY"].startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    hosts = secrets["DEPLOY_KNOWN_HOSTS"]
    assert hosts.startswith("|1|") and "ssh-ed25519" in hosts  # hashed line kept as is
    assert len(remote) == 1 and remote[0][0] == "prod"
    assert remote[0][1].startswith("ssh-ed25519 ") and "ym-ci-t-production-" in remote[0][1]
    assert ["api", "--method", "PUT", "repos/me/site/environments/production"] in st["calls"]
    out = capsys.readouterr().out
    assert "created environment production" in out and "commit site.toml" in out
    # ci = true lands inside [deploy.targets.prod], before the next table
    text = (site / "site.toml").read_text()
    prod = text.split("[deploy.targets.prod]")[1].split("[deploy.targets.staging]")[0]
    assert "ci          = true" in prod
    assert load_site(site).target("prod").ci and not load_site(site).target("staging").ci

    # second run keeps the key, refreshes known hosts, leaves site.toml alone
    old_key = secrets["DEPLOY_SSH_KEY"]
    assert main([*S, "ci", "setup", "prod"]) == 0
    assert fake_gh.load()["envs"]["production"]["DEPLOY_SSH_KEY"] == old_key
    assert len(remote) == 1 and (site / "site.toml").read_text() == text
    out = capsys.readouterr().out
    assert "already set; kept" in out and "commit site.toml" not in out
    assert main([*S, "ci", "setup", "prod", "--rotate"]) == 0
    assert fake_gh.load()["envs"]["production"]["DEPLOY_SSH_KEY"] != old_key
    assert len(remote) == 2
    assert "previous CI key is still authorized" in capsys.readouterr().out

    assert main([*S, "ci", "status"]) == 0
    assert "ok    prod: environment 'production' has both secrets" in capsys.readouterr().out
    assert main([*S, "ci", "status", "staging"]) == 1
    out = capsys.readouterr().out
    assert "FAIL  staging" in out and "ci = false" in out and "ym ci setup staging" in out


def test_set_ci_existing_key(site):
    toml = site / "site.toml"
    toml.write_text(toml.read_text().replace('user = "u"', 'user = "u"\nci = false  # later'))
    assert ci.set_ci(load_site(site), "prod")
    assert "ci = true  # later" in toml.read_text()
    assert not ci.set_ci(load_site(site), "prod")


def test_set_ci_template_hint(site):
    toml = site / "site.toml"
    text = toml.read_text().replace(
        'user = "u"\n',
        'user = "u"\n# ci          = true     # deploy from GitHub Actions\n#\n'
        "# [deploy.targets.x]\n# ci = true\n",
    )
    toml.write_text(text)
    assert ci.set_ci(load_site(site), "prod")
    new = toml.read_text()
    assert 'user = "u"\nci          = true     # deploy from GitHub Actions\n#\n' in new
    assert "# [deploy.targets.x]\n# ci = true\n" in new  # other tables' examples untouched


def test_ci_local_target_rejected(site):
    toml = site / "site.toml"
    toml.write_text(toml.read_text().replace('path = "/srv/www"', 'path = "/srv/www"\nci = true'))
    with pytest.raises(UsageError, match="needs a host"):
        load_site(site)


def test_ci_setup_dry_run_and_unknown_host(site, fake_gh, remote, monkeypatch, capsys):
    S = ["--site-dir", str(site)]
    assert main([*S, "ci", "setup", "--dry-run", "prod"]) == 0
    assert fake_gh.load()["envs"] == {}
    out = capsys.readouterr().out
    assert "would generate and install a new CI key" in out and "would set ci = true" in out
    # default = prod + staging; staging's host isn't known and nobody can confirm (no tty)
    pub = (site.parent / "home/hostkey.pub").read_text().split()
    monkeypatch.setattr(ci, "scan_host_keys", lambda spec: [f"x {pub[0]} {pub[1]}"])
    assert main([*S, "ci", "setup"]) == 2
    err = capsys.readouterr()
    assert "skipping target(s) box, dev" in err.out and "SHA256:" in err.out
    assert "ym ssh-setup staging" in err.err
    assert fake_gh.load()["envs"] == {}  # nothing changed, prod included
    assert "ci = true" not in (site / "site.toml").read_text()


def test_pick_targets(site, fake_gh):
    cfg = load_site(site)
    with pytest.raises(UsageError, match="no host"):
        ci.pick_targets(cfg, ["box"])
    with pytest.raises(UsageError, match="'staging' environment"):
        ci.pick_targets(cfg, ["staging", "dev"])
    assert [p.env for p in ci.pick_targets(cfg, ["dev"])] == ["staging"]


def test_gh_login_prompt(site, fake_gh, remote, monkeypatch, capsys):
    fake_gh.update(logged_in=False)
    assert main(["--site-dir", str(site), "ci", "setup", "prod"]) == 2  # no terminal
    assert "gh auth login" in capsys.readouterr().err
    calls = []

    def terminal(argv):
        calls.append(argv)
        return subprocess.run(argv, check=False).returncode

    monkeypatch.setattr(github, "_terminal", terminal)
    ci.ci_setup(load_site(site), ["prod"], confirm=lambda q: True)
    assert calls == [["gh", "auth", "login"]]
    assert "DEPLOY_SSH_KEY" in fake_gh.load()["envs"]["production"]


def test_no_gh(site, monkeypatch, capsys):
    monkeypatch.setenv("PATH", "/nonexistent")
    assert main(["--site-dir", str(site), "ci", "setup", "prod"]) == 2
    assert "cli.github.com" in capsys.readouterr().err
    assert main(["--site-dir", str(site), "repo", "create"]) == 2


def _commit(root):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    subprocess.run(["git", "tag", "v1.10.6-1"], cwd=root, check=True)


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True).stdout.strip()


def test_repo_create(site, fake_gh, tmp_path, capsys):
    fake_gh.update(repo="", repos=[])
    S = ["--site-dir", str(site)]
    assert main([*S, "repo", "create"]) == 2  # not a git repo with commits
    assert "no commits" in capsys.readouterr().err
    _commit(site)
    assert main([*S, "repo", "create", "--dry-run"]) == 0
    assert "would create me/site (private)" in capsys.readouterr().out
    assert fake_gh.load()["repos"] == []
    assert main([*S, "repo", "create", "--name", "bad name"]) == 2
    assert main([*S, "repo", "create"]) == 0
    st = fake_gh.load()
    assert st["created"][:4] == ["repo", "create", "me/site", "--private"]
    bare = tmp_path / "github/me/site.git"
    assert _git(bare, "rev-parse", "main") == _git(site, "rev-parse", "HEAD")
    assert _git(bare, "tag") == ""  # release tags would start deploys on CI sites
    assert "tags are not pushed" in capsys.readouterr().out
    assert st["envs"] == {}  # nothing about CI
    fake_gh.update(repo="me/site")
    assert main([*S, "repo", "create"]) == 0
    assert "already on GitHub: me/site" in capsys.readouterr().out


def test_repo_create_from_origin(site, fake_gh, tmp_path, capsys):
    """origin names a GitHub repo that doesn't exist yet: create that one, keep origin."""
    fake_gh.update(repo="", repos=[])
    _commit(site)
    _git(site, "remote", "add", "origin", "git@github.com:RubenFixit/links.git")
    with open(os.environ["GIT_CONFIG_GLOBAL"], "a") as fh:
        fh.write(f'[url "{(tmp_path / "github").as_uri()}/"]\n\tinsteadOf = git@github.com:\n')
    S = ["--site-dir", str(site)]
    assert main([*S, "repo", "create", "--name", "other"]) == 2
    assert "origin already names RubenFixit/links" in capsys.readouterr().err
    assert main([*S, "repo", "create", "--public"]) == 0
    assert fake_gh.load()["created"] == ["repo", "create", "RubenFixit/links", "--public"]
    assert (tmp_path / "github/RubenFixit/links.git/refs/heads/main").is_file()


def test_repo_create_origin_not_github(site, fake_gh, capsys):
    fake_gh.update(repo="", repos=[])
    _commit(site)
    _git(site, "remote", "add", "origin", "https://git.example.com/x.git")
    assert main(["--site-dir", str(site), "repo", "create"]) == 2
    assert "not GitHub" in capsys.readouterr().err


def test_ci_setup_offers_repo_create(site, fake_gh, remote, capsys):
    fake_gh.update(repo="", repos=[])
    _commit(site)
    assert main(["--site-dir", str(site), "ci", "setup", "prod"]) == 2  # no terminal
    assert "ym repo create" in capsys.readouterr().err
    asked = []
    ci.ci_setup(load_site(site), ["prod"], confirm=lambda q: asked.append(q) or True)
    assert "Create me/site (private)" in asked[0]
    assert "DEPLOY_SSH_KEY" in fake_gh.load()["envs"]["production"]


def test_doctor_ci(site, fake_gh, remote, monkeypatch, capsys):
    import yourls_manager.deploy.sftp as sftp_mod

    monkeypatch.setattr(sftp_mod, "yourls_state", lambda url: ("ok", None))
    monkeypatch.setattr("yourls_manager.deploy.rsync.RsyncTarget.php_version",
                        lambda self: (8, 3, 0))  # fmt: skip
    monkeypatch.setattr("yourls_manager.remote.SshRunner.home", lambda self: "/home/u")
    monkeypatch.setattr(
        "yourls_manager.remote.SshRunner.run_script",
        lambda self, script, **kw: subprocess.CompletedProcess([], 1, b"", b""),
    )
    toml = site / "site.toml"
    toml.write_text(toml.read_text().replace('user = "u"', 'user = "u"\nci = true'))
    main(["--site-dir", str(site), "doctor", "prod"])
    assert "FAIL  ci: me/site: environment 'production' does not exist" in capsys.readouterr().out
    main(["--site-dir", str(site), "ci", "setup", "prod"])
    capsys.readouterr()
    main(["--site-dir", str(site), "doctor", "prod"])
    assert "ok    ci: me/site: environment 'production' has both secrets" in capsys.readouterr().out


def test_github_repo_of():
    assert github.github_repo_of("git@github.com:o/r.git") == "o/r"
    assert github.github_repo_of("https://github.com/o/r") == "o/r"
    assert github.github_repo_of("ssh://git@github.com/o/r.git") == "o/r"
    assert github.github_repo_of("https://gitlab.com/o/r") is None


def test_authorized_keys_script(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path)}
    line = "ssh-ed25519 AAAAC3Nz test-key"
    for _ in range(2):
        subprocess.run(["sh", "-c", ci.AUTHORIZED_KEYS_SCRIPT, "sh", line], env=env,
                       check=True)  # fmt: skip
    ak = tmp_path / ".ssh/authorized_keys"
    assert ak.read_text() == line + "\n"
    assert oct(ak.stat().st_mode & 0o777) == "0o600"
    assert oct((tmp_path / ".ssh").stat().st_mode & 0o777) == "0o700"


def test_real_key_and_fingerprints(tmp_path):
    key = ci.generate_key(tmp_path, "ym-ci-x")
    assert key.public.endswith(" ym-ci-x")
    fp = ci.fingerprints([key.public])
    assert fp and "SHA256:" in fp[0]
    assert textwrap.dedent(key.private.read_text()).startswith("-----BEGIN OPENSSH")
