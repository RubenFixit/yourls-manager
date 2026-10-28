from __future__ import annotations

import http.server
import os
import shutil
import subprocess
import textwrap
import threading
from pathlib import Path

import pytest

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
}


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch, tmp_path_factory):
    for k, v in GIT_ENV.items():
        monkeypatch.setenv(k, v)
    # isolate from the developer's global git config
    gcfg = tmp_path_factory.mktemp("gitcfg") / "gitconfig"
    gcfg.write_text("[init]\n\tdefaultBranch = main\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gcfg))


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def write_files(root: Path, files: dict[str, str | bytes]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(textwrap.dedent(content))


class FakeUpstream:
    """A bare git repo served via file:// with a working clone for making commits."""

    def __init__(self, base: Path, name: str):
        self.work = base / f"{name}-work"
        self.bare = base / f"{name}.git"
        self.work.mkdir(parents=True)
        git(self.work, "init", "-q", "-b", "main")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.bare)], check=True)
        git(self.work, "remote", "add", "origin", str(self.bare))

    @property
    def url(self) -> str:
        return self.bare.as_uri()

    def commit(
        self,
        files: dict[str, str | bytes],
        message: str = "update",
        tag: str | None = None,
        remove: tuple[str, ...] = (),
    ) -> str:
        write_files(self.work, files)
        for rel in remove:
            (self.work / rel).unlink()
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", message)
        if tag:
            git(self.work, "tag", "-a", tag, "-m", tag)
        git(self.work, "push", "-q", "origin", "main", "--tags")
        return git(self.work, "rev-parse", "HEAD")


@pytest.fixture
def upstream(tmp_path):
    base = tmp_path / "upstreams"
    base.mkdir()

    def make(name: str) -> FakeUpstream:
        return FakeUpstream(base, name)

    return make


def core_files(version: str, db: str = "507") -> dict[str, str]:
    return {
        "includes/version.php": f"<?php\ndefine( 'YOURLS_VERSION', '{version}' );\n"
        f"define( 'YOURLS_DB_VERSION', '{db}' );\n",
        "includes/load-yourls.php": "<?php // stub\n",
        "admin/index.php": f"<?php // admin {version}\n",
        "yourls-loader.php": "<?php // loader\n",
        "user/plugins/hyphens-in-urls/plugin.php": "<?php /* Plugin Name: Hyphens */\n",
        "user/plugins/sample-plugin/plugin.php": "<?php /* Plugin Name: Sample */\n",
        "LICENSE": "MIT\n",
        "readme.html": "<html></html>\n",
        "README.md": "# YOURLS\n",
        "CHANGELOG.md": "changes\n",
        "includes/vendor/lib/README.md": "nested md\n",
        ".github/workflows/ci.yml": "on: push\n",
        ".gitattributes": "* text=auto\n",
        "tests/test.php": "<?php\n",
    }


@pytest.fixture
def fake_core(upstream):
    """Fake YOURLS core upstream with tags 1.0.0 and 1.1.0."""
    core = upstream("core")
    core.commit(core_files("1.0.0"), tag="1.0.0")
    core.commit(core_files("1.1.0"), tag="1.1.0")
    return core


needs_rsync = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")
needs_php = pytest.mark.skipif(shutil.which("php") is None, reason="php CLI not installed")


ROUTES = {
    "/health.php": (200, None, b'{"ok": true}'),
    "/g": (301, "https://git.example.org/", b""),
    "/g/repo?ref=main": (301, "https://git.example.org/repo?ref=main", b""),
    "/tmp": (302, "https://example.org/temp", b""),
    "/": (200, None, b"<html>home</html>"),
}


class Handler(http.server.BaseHTTPRequestHandler):
    routes = ROUTES

    def do_GET(self):
        status, location, body = self.routes.get(self.path, (404, None, b"not found"))
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


# --------------------------------------------------------------------------- onboarding (v0.2)

YOURLS_CORE_URL = "https://github.com/YOURLS/YOURLS.git"


def _find_template() -> Path | None:
    import os

    env = os.environ.get("YM_TEST_TEMPLATE")
    if env:
        return Path(env)
    sibling = Path(__file__).resolve().parents[2] / "yourls-site-template"
    return sibling if (sibling / "copier.yml").is_file() else None


TEMPLATE_DIR = _find_template()
needs_copier = pytest.mark.skipif(
    TEMPLATE_DIR is None or shutil.which("uv") is None,
    reason="needs uv (for `uvx copier`) and the yourls-site-template checkout"
    " (sibling dir or YM_TEST_TEMPLATE)",
)


def git_insteadof(real_url: str, fake_url: str) -> None:
    """Redirect ``real_url`` to ``fake_url`` for every git call (isolated global config)."""
    import os

    cfg = Path(os.environ["GIT_CONFIG_GLOBAL"])
    with cfg.open("a") as fh:
        fh.write(f'[url "{fake_url}"]\n\tinsteadOf = {real_url}\n')


def new_site_args(dest: Path, *data: str) -> list[str]:
    """argv for `ym new` from the local template with non-interactive answers."""
    assert TEMPLATE_DIR is not None
    argv = [
        "new",
        str(dest),
        "--template",
        str(TEMPLATE_DIR),
        "--vcs-ref",
        "HEAD",
        "--defaults",
        "--data",
        "site_name=Test Links",
        "--data",
        "site_url=https://go.example.com",
    ]
    for d in data:
        argv += ["--data", d]
    return argv


@pytest.fixture
def no_uv_lock(monkeypatch):
    """`uv lock` would try to fetch the pinned yourls-manager tag from GitHub."""
    from yourls_manager import newsite

    calls: list[Path] = []

    def fake(site: Path) -> bool:
        calls.append(site)
        return False

    monkeypatch.setattr(newsite, "uv_lock", fake)
    return calls


# --------------------------------------------------------------------------- sftp (v0.3)

from . import sftp_server  # noqa: E402

SFTP_SKIP = sftp_server.unavailable_reason()
needs_sshd = pytest.mark.skipif(SFTP_SKIP is not None, reason=f"real sshd: {SFTP_SKIP}")


@pytest.fixture(autouse=True)
def _forget_sftp_passwords():
    """The SFTP transport caches a prompted password per process; isolate tests."""
    from yourls_manager.transport import sftp

    sftp._PASSWORDS.clear()
    yield
    sftp._PASSWORDS.clear()


@pytest.fixture(scope="session")
def sshd(tmp_path_factory):
    """A real OpenSSH server on 127.0.0.1 (random port) with two test accounts."""
    if SFTP_SKIP is not None:
        pytest.skip(SFTP_SKIP)
    srv = sftp_server.SshServer(tmp_path_factory.mktemp("sshd"))
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture(scope="session")
def client_key(tmp_path_factory) -> Path:
    """An unencrypted ed25519 key pair (private key path; ``.pub`` beside it)."""
    key = tmp_path_factory.mktemp("clientkey") / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "ym-test", "-f", str(key)],
        check=True,
    )
    return key


@pytest.fixture(params=[False, True], ids=["shell-account", "sftp-only-account"])
def account(request, sshd) -> sftp_server.Account:
    """A freshly emptied account: an ordinary one, and a chrooted SFTP-only one."""
    return sshd.account(request.param)


@pytest.fixture
def sftp_only(sshd) -> sftp_server.Account:
    return sshd.account(True)


@pytest.fixture
def ssh_home(tmp_path, monkeypatch, sshd) -> Path:
    """$HOME for the client side: ~/.ssh/known_hosts trusts the test server, no agent."""
    home = tmp_path / "client-home"
    (home / ".ssh").mkdir(parents=True, mode=0o700)
    (home / ".ssh" / "known_hosts").write_text(sshd.known_hosts_line())
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    for name in list(os.environ):
        if name.startswith("YM_") and name.endswith("_PASSWORD"):
            monkeypatch.delenv(name)
    return home


def install_client_key(home: Path, key: Path) -> None:
    """Make ``key`` the default key (~/.ssh/id_ed25519), as ssh would find it."""
    dest = home / ".ssh" / "id_ed25519"
    shutil.copy(key, dest)
    shutil.copy(key.with_name(key.name + ".pub"), dest.with_name("id_ed25519.pub"))
    dest.chmod(0o600)


class StateHandler(http.server.BaseHTTPRequestHandler):
    """Emulates YOURLS' install/upgrade redirects (mode: ok | install | upgrade)."""

    mode = "ok"

    def do_GET(self):
        path = self.path
        status, location, body = 404, None, b"not found"
        if path == "/":
            status, location, body = 302, "/admin/", b""  # template .htaccess: root -> admin
        elif path == "/admin/" and self.mode in ("install", "upgrade"):
            host = self.headers.get("Host")
            status, location, body = 307, f"http://{host}/admin/{self.mode}.php", b""
        elif path == "/admin/":
            status, body = 200, b"<html>login</html>"
        elif path == "/health.php":
            status, body = 200, b'{"ok": true}'
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def state_server():
    """(url, handler class): set ``handler.mode`` to "install" / "upgrade" / "ok"."""
    handler = type("H", (StateHandler,), {"mode": "ok"})
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", handler
    httpd.shutdown()
    httpd.server_close()
