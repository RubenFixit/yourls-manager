"""GitLab: `ym repo create` and `ym ci setup|status`.

A fake `glab` on PATH owns the login (auth status/login, config: token, api_host,
api_protocol, git_protocol); a fake GitLab HTTP server in this process answers the API calls
python-gitlab makes, and checks they carry glab's token. Each GitLab host is served under
its own path prefix (glab's ``api_host`` for it points there).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from yourls_manager import ci, gitlab
from yourls_manager.cli import main
from yourls_manager.config import load_site

from .test_github import remote, site  # noqa: F401  (fixtures)

FAKE_GLAB = r"""
import json, os, sys
state_file = os.environ["FAKE_GLAB_STATE"]
state = json.load(open(state_file))
args = sys.argv[1:]
state["calls"].append(args)
json.dump(state, open(state_file, "w"))
def opt(name):
    return args[args.index(name) + 1] if name in args else "gitlab.com"
if args[:2] == ["auth", "status"]:
    if not state["logged_in"]:
        sys.exit(1)
    if "--show-token" in args:  # glab prints its status on stderr
        sys.stderr.write(f"gitlab.com\n  ✓ Logged in to {opt('--hostname')} as me\n"
                         f"  ✓ Token found: {state['keyring_token']}\n")
    sys.exit(0)
if args[:2] == ["auth", "login"]:
    state["logged_in"] = True
    json.dump(state, open(state_file, "w")); sys.exit(0)
if args[:2] == ["config", "get"]:
    key, host = args[2], opt("--host")
    values = {"token": state["config_token"], "api_protocol": "http",
              "api_host": f"127.0.0.1:{state['port']}/h/{host}",
              "git_protocol": state.get("proto", "ssh")}
    print(values.get(key, "")); sys.exit(0)
if args[0] == "api":  # any API request refreshes an expired OAuth token
    state["config_token"] = state["server_token"]
    json.dump(state, open(state_file, "w")); sys.exit(0)
sys.exit(2)
"""

ANSWERS = "_commit: v0.8.0\nci_provider: gitlab\n"


class FakeGitLab:
    """The REST endpoints ym uses, one in-memory instance per host."""

    def __init__(self, gl_root):
        self.gl_root = gl_root
        self.token = "glpat-ymtestxxxxxxxx"
        self.login = "me"
        self.namespaces = {"team": 7}
        self.projects: dict[str, dict] = {}
        self.hosts: list[str] = []
        self.tokens_allowed = True
        self.token_bodies: list[dict] = []
        self.requests: list[tuple[str, str]] = []

    def add_project(self, key="gitlab.com/me/site"):
        self.projects[key] = {"id": len(self.projects) + 1, "envs": [], "vars": [], "tags": [],
                              "schedules": [], "visibility": "private"}  # fmt: skip

    def handle(self, method, path, query, headers, body):
        if headers.get("Authorization") != f"Bearer {self.token}":
            return 401, {"message": "401 Unauthorized"}
        assert path.startswith("/h/"), path
        _, _, host, api, v4, *parts = path.split("/")
        assert (api, v4) == ("api", "v4"), path
        self.hosts.append(host)
        self.requests.append((method, "/".join(parts)))
        if parts == ["user"]:
            return 200, {"id": 1, "username": self.login}
        if parts[0] == "namespaces":
            ns = unquote(parts[1])
            if ns in self.namespaces:
                return 200, {"id": self.namespaces[ns], "full_path": ns}
            return 404, {"message": "404 Namespace Not Found"}
        if parts == ["projects"] and method == "POST":
            ns = self.login
            if "namespace_id" in body:
                ns = next(k for k, v in self.namespaces.items() if v == body["namespace_id"])
            rel = f"{ns}/{body['path']}"
            bare = self.gl_root / host / f"{rel}.git"
            bare.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
            self.add_project(f"{host}/{rel}")
            self.projects[f"{host}/{rel}"]["visibility"] = body["visibility"]
            return 201, {"id": 99, "path_with_namespace": rel, "default_branch": "main",
                         "ssh_url_to_repo": f"git@{host}:{rel}.git",
                         "http_url_to_repo": f"https://{host}/{rel}.git"}  # fmt: skip
        proj = self.projects.get(f"{host}/{unquote(parts[1])}")
        if proj is None:
            return 404, {"message": "404 Project Not Found"}
        rest = parts[2:]
        if not rest:
            return 200, {"id": proj["id"], "path_with_namespace": unquote(parts[1]),
                         "default_branch": "main"}  # fmt: skip
        if rest == ["environments"]:
            if method == "POST":
                proj["envs"].append(body["name"])
                return 201, {"id": len(proj["envs"]), "name": body["name"]}
            return 200, [{"id": i, "name": e} for i, e in enumerate(proj["envs"])
                         if e == query.get("name", e)]  # fmt: skip
        if rest == ["variables"]:
            if method == "POST":
                if any(v["key"] == body["key"] and v["environment_scope"] ==
                       body["environment_scope"] for v in proj["vars"]):  # fmt: skip
                    return 400, {"message": {"key": ["has already been taken"]}}
                proj["vars"].append(body)
                return 201, body
            return 200, proj["vars"]
        if rest[0] == "variables" and method == "PUT":
            scope = query["filter[environment_scope]"]
            for v in proj["vars"]:
                if v["key"] == unquote(rest[1]) and v["environment_scope"] == scope:
                    v.update(body)
                    return 200, v
            return 404, {"message": "404 Variable Not Found"}
        if rest[0] == "protected_tags":
            if method == "POST":
                proj["tags"].append(body)
                return 201, body
            name = unquote(rest[1])
            if any(t["name"] == name for t in proj["tags"]):
                return 200, {"name": name}
            return 404, {"message": "404 Not found"}
        if rest == ["pipeline_schedules"]:
            if method == "POST":
                body["id"] = 40 + len(proj["schedules"])
                proj["schedules"].append(body)
                return 201, body
            return 200, proj["schedules"]
        if rest == ["access_tokens"]:
            if not self.tokens_allowed:
                return 403, {"message": "403 Forbidden"}
            self.token_bodies.append(body)
            return 201, {"id": 5, "token": f"glpat-made{len(self.token_bodies)}xxxxxxxx"}
        return 500, {"message": f"unhandled {method} {path}"}


def _serve(fake: FakeGitLab) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _do(self):
            url = urlsplit(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            ctype = self.headers.get("Content-Type", "")
            if raw and "json" in ctype:
                body = json.loads(raw)
            else:
                body = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
                body = {k: {"true": True, "false": False}.get(v, v) for k, v in body.items()}
            code, out = fake.handle(self.command, url.path, query, dict(self.headers), body)
            data = json.dumps(out).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_DELETE = _do

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def fake_glab(tmp_path, monkeypatch):
    gl_root = tmp_path / "gitlab"
    fake = FakeGitLab(gl_root)
    server = _serve(fake)
    bin_dir = tmp_path / "glbin"
    bin_dir.mkdir()
    script = bin_dir / "glab"
    script.write_text(f"#!{sys.executable}\n{FAKE_GLAB}")
    script.chmod(0o755)
    state = tmp_path / "glab-state.json"
    state.write_text(json.dumps({
        "logged_in": True, "calls": [], "port": server.server_port,
        "config_token": fake.token, "keyring_token": fake.token, "server_token": fake.token,
    }))  # fmt: skip
    # pushes to git@<host>:<path>.git and https://<host>/<path>.git land in local bare repos
    with open(os.environ["GIT_CONFIG_GLOBAL"], "a") as fh:
        for host in ("gitlab.com", "gitlab.example.com"):
            fh.write(
                f'[url "{(gl_root / host).as_uri()}/"]\n\tinsteadOf = git@{host}:\n'
                f"\tinsteadOf = https://{host}/\n"
            )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GLAB_STATE", str(state))
    for name in gitlab.TOKEN_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    def glab_state(**kw):
        d = json.loads(state.read_text())
        d.update(kw)
        state.write_text(json.dumps(d))
        return d

    fake.glab_state = glab_state
    yield fake
    server.shutdown()


@pytest.fixture
def gl_site(site):  # noqa: F811
    (site / ".copier-answers.yml").write_text(ANSWERS)
    (site / ".gitlab-ci.yml").write_text("stages: [check]\n")
    return site


def _commit(root):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    subprocess.run(["git", "tag", "v1.10.6-1"], cwd=root, check=True)


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True).stdout.strip()


# --------------------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    ("url", "host", "path"),
    [
        ("git@gitlab.com:rubenfixit/sms-yourls.git", "gitlab.com", "rubenfixit/sms-yourls"),
        ("https://gitlab.com/rubenfixit/sms-yourls", "gitlab.com", "rubenfixit/sms-yourls"),
        ("https://gitlab.example.com/team/sub/links.git", "gitlab.example.com", "team/sub/links"),
        ("ssh://git@gitlab.example.com:2222/team/links.git", "gitlab.example.com", "team/links"),
        ("https://gitlab.com/a/b/-/releases/v1", "gitlab.com", "a/b"),
    ],
)
def test_project_of(url, host, path):
    proj = gitlab.project_of(url)
    assert proj == gitlab.Project(host, path)


def test_project_of_rejects():
    assert gitlab.project_of("https://gitlab.com/onlyone") is None
    assert gitlab.project_of("not a url") is None
    assert str(gitlab.Project("gitlab.example.com", "a/b")) == "gitlab.example.com/a/b"
    assert str(gitlab.Project("gitlab.com", "a/b")) == "a/b"


# --------------------------------------------------------------------------- repo create


def test_repo_create(gl_site, fake_glab, tmp_path, capsys):
    S = ["--site-dir", str(gl_site)]
    assert main([*S, "repo", "create"]) == 2
    assert "no commits" in capsys.readouterr().err
    _commit(gl_site)
    assert main([*S, "repo", "create", "--dry-run"]) == 0
    assert "would create me/site (private)" in capsys.readouterr().out
    assert fake_glab.projects == {}
    assert main([*S, "repo", "create"]) == 0
    out = capsys.readouterr().out
    assert "created me/site (private)" in out and "tags are not pushed" in out
    assert _git(gl_site, "config", "--get", "remote.origin.url") == "git@gitlab.com:me/site.git"
    bare = tmp_path / "gitlab/gitlab.com/me/site.git"
    assert _git(bare, "rev-parse", "main") == _git(gl_site, "rev-parse", "HEAD")
    assert _git(bare, "tag") == ""
    assert main([*S, "repo", "create"]) == 0
    assert "already on GitLab: me/site" in capsys.readouterr().out
    assert (
        fake_glab.glab_state()["calls"].count(["auth", "status", "--hostname", "gitlab.com"]) >= 1
    )


def test_repo_create_from_repo_url(gl_site, fake_glab, tmp_path, capsys):
    """repo_url names a self-managed host and a group: create there."""
    (gl_site / ".copier-answers.yml").write_text(
        ANSWERS + "repo_url: https://gitlab.example.com/team/links\n"
    )
    _commit(gl_site)
    S = ["--site-dir", str(gl_site)]
    assert main([*S, "repo", "create", "--host", "gitlab.com"]) == 2
    assert "already names gitlab.example.com/team/links" in capsys.readouterr().err
    fake_glab.glab_state(proto="https")
    assert main([*S, "repo", "create", "--public"]) == 0
    assert fake_glab.projects["gitlab.example.com/team/links"]["visibility"] == "public"
    assert set(fake_glab.hosts) == {"gitlab.example.com"}
    assert (
        _git(gl_site, "config", "--get", "remote.origin.url")
        == "https://gitlab.example.com/team/links.git"
    )


def test_repo_create_origin_not_gitlab(gl_site, fake_glab, capsys):
    _commit(gl_site)
    _git(gl_site, "remote", "add", "origin", "git@github.com:me/site.git")
    assert main(["--site-dir", str(gl_site), "repo", "create"]) == 2
    assert "not GitLab" in capsys.readouterr().err


def test_repo_create_unknown_group(gl_site, fake_glab, capsys):
    _commit(gl_site)
    assert main(["--site-dir", str(gl_site), "repo", "create", "--owner", "nope"]) == 2
    assert "namespace 'nope' not found" in capsys.readouterr().err


def test_no_glab(gl_site, monkeypatch, capsys):
    monkeypatch.setenv("PATH", "/nonexistent")
    assert main(["--site-dir", str(gl_site), "ci", "setup", "prod"]) == 2
    assert "gitlab-org/cli" in capsys.readouterr().err


def test_glab_login_prompt(gl_site, fake_glab, remote, monkeypatch):  # noqa: F811
    fake_glab.glab_state(logged_in=False)
    calls = []
    monkeypatch.setattr(gitlab, "_terminal", lambda argv: calls.append(argv) or
                        subprocess.run(argv, check=False).returncode)  # fmt: skip
    _commit(gl_site)
    fake_glab.add_project()
    _git(gl_site, "remote", "add", "origin", "git@gitlab.com:me/site.git")
    ci.ci_setup(load_site(gl_site), ["prod"], confirm=lambda q: True, skip_updates=True)
    assert calls == [["glab", "auth", "login", "--hostname", "gitlab.com"]]


# --------------------------------------------------------------------------- ci setup


def _vars(project, key):
    return {v["environment_scope"]: v for v in project["vars"] if v["key"] == key}


def test_ci_setup_prod(gl_site, fake_glab, remote, monkeypatch, capsys):  # noqa: F811
    _commit(gl_site)
    _git(gl_site, "remote", "add", "origin", "git@gitlab.com:me/site.git")
    fake_glab.add_project()
    S = ["--site-dir", str(gl_site)]
    assert main([*S, "ci", "setup", "prod"]) == 0
    proj = fake_glab.projects["gitlab.com/me/site"]
    assert proj["envs"] == ["production"]
    key = _vars(proj, "DEPLOY_SSH_KEY")["production"]
    first_key = key["value"]
    assert key["value"].startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert key["variable_type"] == "file" and key["protected"] and not key["masked"]
    hosts = _vars(proj, "DEPLOY_KNOWN_HOSTS")["production"]
    assert hosts["variable_type"] == "file" and "ssh-ed25519" in hosts["value"]
    assert proj["tags"] == [{"name": "v*", "create_access_level": 40}]
    assert [s["description"] for s in proj["schedules"]] == ["ym check-updates"]
    assert proj["schedules"][0]["ref"] == "main"
    token = _vars(proj, "YM_UPDATE_TOKEN")["*"]
    assert token["value"] == "glpat-made1xxxxxxxx" and token["masked"] and not token["protected"]
    tb = fake_glab.token_bodies[0]
    assert tb["scopes"] == ["write_repository"] and tb["access_level"] == 30
    assert len(remote) == 1 and "ym-ci-t-production-" in remote[0][1]
    out = capsys.readouterr().out
    assert "created environment production" in out and "protected the tags v*" in out
    assert "glab schedule run 40" in out and 'git commit -m "Deploy from GitLab CI"' in out
    text = (gl_site / "site.toml").read_text()
    assert "ci          = true             # deploy from GitLab CI" in text
    # no secret value ever reaches argv
    for call in fake_glab.glab_state()["calls"]:
        assert not any("PRIVATE KEY" in a or "glpat-" in a for a in call)

    assert main([*S, "ci", "status"]) == 0
    out = capsys.readouterr().out
    assert "ok    prod: environment 'production' has both secrets" in out
    assert "ok    check-updates: schedule 'ym check-updates' and YM_UPDATE_TOKEN" in out

    # second run: keeps key and token, updates known hosts in place
    assert main([*S, "ci", "setup", "prod"]) == 0
    proj2 = fake_glab.projects["gitlab.com/me/site"]
    assert _vars(proj2, "DEPLOY_SSH_KEY")["production"]["value"] == first_key
    assert len(proj2["vars"]) == 3 and len(proj2["schedules"]) == 1 and len(proj2["tags"]) == 1
    out = capsys.readouterr().out
    assert "already set; kept" in out and "YM_UPDATE_TOKEN already set; kept" in out

    # --rotate: new key and token; the old key is removed when confirmed
    removed = []
    monkeypatch.setattr(ci, "remove_old_keys",
                        lambda spec, prefix, keep: removed.append((prefix, keep)) or 1)  # fmt: skip
    ci.ci_setup(load_site(gl_site), ["prod"], rotate=True, confirm=lambda q: True)
    proj3 = fake_glab.projects["gitlab.com/me/site"]
    assert _vars(proj3, "DEPLOY_SSH_KEY")["production"]["value"] != first_key
    assert _vars(proj3, "YM_UPDATE_TOKEN")["*"]["value"] == "glpat-made2xxxxxxxx"
    assert removed and removed[0][0] == "ym-ci-t-production-"
    assert "removed 1 previous CI key(s)" in capsys.readouterr().out


def test_ci_status_problems(gl_site, fake_glab, capsys):
    _commit(gl_site)
    _git(gl_site, "remote", "add", "origin", "git@gitlab.com:me/site.git")
    toml = gl_site / "site.toml"
    toml.write_text(toml.read_text().replace('user = "u"', 'user = "u"\nci = true'))
    S = ["--site-dir", str(gl_site)]
    assert main([*S, "ci", "status"]) == 1
    assert "FAIL  no GitLab project for this site" in capsys.readouterr().out
    fake_glab.add_project()
    assert main([*S, "ci", "status"]) == 1
    out = capsys.readouterr().out
    assert "FAIL  prod: environment 'production' does not exist" in out
    assert "tags v* are not protected" in out
    assert "warn  check-updates: no schedule 'ym check-updates'; no YM_UPDATE_TOKEN" in out


def test_ci_setup_artifact_only(tmp_path, fake_glab, monkeypatch, capsys):
    """No deploy targets: only check-updates; a refused token is asked for."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "links"
    root.mkdir()
    (root / "site.toml").write_text(
        '[site]\nname = "L"\nslug = "links"\nurl = "https://goto"\n[core]\nversion = "1.0.0"\n'
    )
    (root / ".copier-answers.yml").write_text(ANSWERS + "repo_url: https://gitlab.com/me/links\n")
    (root / ".gitlab-ci.yml").write_text("stages: [check]\n")
    _commit(root)
    fake_glab.add_project("gitlab.com/me/links")
    fake_glab.tokens_allowed = False
    S = ["--site-dir", str(root)]
    assert main([*S, "ci", "setup", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would create the pipeline schedule 'ym check-updates'" in out
    assert fake_glab.projects["gitlab.com/me/links"]["schedules"] == []
    # no terminal: schedule made, token left out with instructions
    assert main([*S, "ci", "setup"]) == 0
    captured = capsys.readouterr()
    assert "no deploy targets to set up" in captured.out and "HTTP 403" in captured.out
    assert "YM_UPDATE_TOKEN not set" in captured.err
    proj = fake_glab.projects["gitlab.com/me/links"]
    assert len(proj["schedules"]) == 1 and proj["vars"] == [] and proj["tags"] == []
    # on a terminal: the pasted token is stored masked
    asked = []
    ci.ci_setup(load_site(root), [], ask_secret=lambda q: asked.append(q) or " glpat-pasted99 ")
    assert "Paste a token with write_repository for me/links" in asked[0]
    assert _vars(fake_glab.projects["gitlab.com/me/links"], "YM_UPDATE_TOKEN")["*"]["value"] == (
        "glpat-pasted99"
    )
    assert main([*S, "ci", "status"]) == 0
    out = capsys.readouterr().out
    assert "no target has ci = true" in out and "ok    check-updates" in out
    assert main([*S, "ci", "setup", "--skip-updates"]) == 2  # nothing left to set up
    assert "no prod or staging target" in capsys.readouterr().err


def test_ci_setup_offers_project_create(gl_site, fake_glab, remote, capsys):  # noqa: F811
    _commit(gl_site)
    S = ["--site-dir", str(gl_site)]
    assert main([*S, "ci", "setup", "prod", "--skip-updates"]) == 2  # no terminal
    assert "ym repo create" in capsys.readouterr().err
    assert main([*S, "ci", "setup", "prod", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would offer to create me/site" in out and "would protect the tags v*" in out
    asked = []
    ci.ci_setup(load_site(gl_site), ["prod"], skip_updates=True,
                confirm=lambda q: asked.append(q) or True)  # fmt: skip
    assert "Create me/site (private)" in asked[0]
    proj = fake_glab.projects["gitlab.com/me/site"]
    assert "production" in proj["envs"] and proj["schedules"] == []


def test_doctor_gitlab_ci(gl_site, fake_glab, remote, monkeypatch, capsys):  # noqa: F811
    import yourls_manager.deploy.sftp as sftp_mod

    monkeypatch.setattr(sftp_mod, "yourls_state", lambda url: ("ok", None))
    monkeypatch.setattr("yourls_manager.deploy.rsync.RsyncTarget.php_version",
                        lambda self: (8, 3, 0))  # fmt: skip
    monkeypatch.setattr("yourls_manager.remote.SshRunner.home", lambda self: "/home/u")
    monkeypatch.setattr(
        "yourls_manager.remote.SshRunner.run_script",
        lambda self, script, **kw: subprocess.CompletedProcess([], 1, b"", b""),
    )
    _commit(gl_site)
    _git(gl_site, "remote", "add", "origin", "git@gitlab.com:me/site.git")
    fake_glab.add_project()
    toml = gl_site / "site.toml"
    toml.write_text(toml.read_text().replace('user = "u"', 'user = "u"\nci = true'))
    main(["--site-dir", str(gl_site), "doctor", "prod"])
    out = capsys.readouterr().out
    assert "FAIL  ci: me/site: environment 'production' does not exist" in out
    main(["--site-dir", str(gl_site), "ci", "setup", "prod", "--skip-updates"])
    capsys.readouterr()
    main(["--site-dir", str(gl_site), "doctor", "prod"])
    assert "ok    ci: me/site: environment 'production' has both secrets" in capsys.readouterr().out
    fake_glab.glab_state(logged_in=False)
    main(["--site-dir", str(gl_site), "doctor", "prod"])
    assert "warn  ci: can't check GitLab" in capsys.readouterr().out


# --------------------------------------------------------------------------- old keys


def test_remove_keys_script(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path)}
    ssh = tmp_path / ".ssh"
    ssh.mkdir(mode=0o700)
    ak = ssh / "authorized_keys"
    ak.write_text(
        "ssh-ed25519 AAAA1 me@laptop\n"
        "ssh-ed25519 AAAA2 ym-ci-t-production-20250101\n"
        "\n"
        'from="1.2.3.4" ssh-ed25519 AAAA3 ym-ci-t-production-20260101\n'
        "ssh-ed25519 AAAA4 ym-ci-t-staging-20250101\n"
        "ssh-ed25519 AAAA5 ym-ci-t-production-20261030\n"
    )
    ak.chmod(0o600)
    proc = subprocess.run(
        ["sh", "-c", ci.REMOVE_KEYS_SCRIPT, "sh", "ym-ci-t-production-",
         "ym-ci-t-production-20261030"],
        env=env, capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert proc.stdout.strip() == "2"
    assert ak.read_text() == (
        "ssh-ed25519 AAAA1 me@laptop\n\n"
        "ssh-ed25519 AAAA4 ym-ci-t-staging-20250101\n"
        "ssh-ed25519 AAAA5 ym-ci-t-production-20261030\n"
    )
    assert oct(ak.stat().st_mode & 0o777) == "0o600"
    assert not (ssh / "authorized_keys.ym-tmp").exists()
    assert ci._old_key("ssh-ed25519 X ym-ci-a-staging-1", "ym-ci-a-staging-", "k")
    assert not ci._old_key(
        "ssh-ed25519 X ym-ci-a-staging-1", "ym-ci-a-staging-", "ym-ci-a-staging-1"
    )


# --------------------------------------------------------------------------- glab's login


def test_token_sources(gl_site, fake_glab, monkeypatch):
    g = gitlab.Glab(cwd=gl_site)
    assert g.token() == fake_glab.token  # glab's config file
    fake_glab.glab_state(config_token="", keyring_token="glpat-fromkeyring")
    assert g.token() == "glpat-fromkeyring"  # keyring: `glab auth status --show-token`
    assert ["auth", "status", "--hostname", "gitlab.com", "--show-token"] in (
        fake_glab.glab_state()["calls"]
    )
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-fromenv")
    assert g.token() == "glpat-fromenv"  # environment first, as glab does
    monkeypatch.delenv("GITLAB_TOKEN")
    fake_glab.glab_state(keyring_token="**************")
    with pytest.raises(gitlab.UsageError, match=r"glab auth login --hostname gitlab\.com"):
        g.token()
    assert g.api_url() == f"http://127.0.0.1:{fake_glab.glab_state()['port']}/h/gitlab.com"


def test_expired_token_refreshed_by_glab(gl_site, fake_glab):
    """A 401 (expired OAuth token): any glab API request refreshes it; ym retries once."""
    fake_glab.add_project()
    fake_glab.glab_state(config_token="glpat-expired")
    g = gitlab.Glab(cwd=gl_site)
    assert gitlab.get_project(g, gitlab.Project("gitlab.com", "me/site"))["id"] == 1
    assert ["api", "--hostname", "gitlab.com", "user"] in fake_glab.glab_state()["calls"]
    fake_glab.glab_state(config_token="glpat-wrong", server_token="glpat-still-wrong")
    with pytest.raises(gitlab.ApiError, match="glab auth status") as err:
        gitlab.get_project(gitlab.Glab(cwd=gl_site), gitlab.Project("gitlab.com", "me/site"))
    assert err.value.status == 401
