"""GitLab: `ym repo create` and the GitLab side of `ym ci`.

Two tools, each for what it does best:

- the GitLab CLI (``glab``) owns the login: ym checks ``glab auth status``, offers
  ``glab auth login``, and reads glab's token and API host for the GitLab host, so ym never
  stores or asks for GitLab credentials;
- python-gitlab makes the API calls with that token (typed errors with HTTP status codes,
  pagination). It is imported only by the commands that talk to GitLab.

The host comes from the site's ``origin`` (or the template's ``repo_url`` answer, or
``--host``), so self-managed GitLab works the same as gitlab.com.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import quote

from .config import SiteConfig, template_answer
from .util import UsageError, YMError, console, run

if TYPE_CHECKING:
    import gitlab as pygitlab

GLAB_INSTALL = "https://gitlab.com/gitlab-org/cli#installation"
DEFAULT_HOST = "gitlab.com"
# glab's own token variables, in its order of precedence
TOKEN_ENV = ("GITLAB_TOKEN", "GITLAB_ACCESS_TOKEN", "OAUTH_TOKEN")

Confirm = Callable[[str], bool]
Interactive = Callable[[Sequence[str]], int]
T = TypeVar("T")

_REMOTE = re.compile(
    r"^(?:https?://(?:[^@/\s]+@)?(?P<h1>[^/:\s]+)(?::\d+)?/"
    r"|ssh://(?:[^@/\s]+@)?(?P<h2>[^/:\s]+)(?::\d+)?/"
    r"|[^@/\s]+@(?P<h3>[^:/\s]+):)"
    r"(?P<path>[^\s]+?)(?:\.git)?/?$"
)
_SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
_SHOWN_TOKEN = re.compile(r"Token(?: found)?:\s*(\S+)", re.IGNORECASE)


@dataclass(frozen=True)
class Project:
    host: str
    path: str  # namespace/.../name

    @property
    def namespace(self) -> str:
        return self.path.rsplit("/", 1)[0]

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[1]

    def __str__(self) -> str:
        return self.path if self.host == DEFAULT_HOST else f"{self.host}/{self.path}"


def project_of(url: str) -> Project | None:
    """``host`` + ``namespace/name`` from a GitLab remote or web URL (subgroups allowed)."""
    m = _REMOTE.match(url.strip())
    if m is None:
        return None
    host = (m["h1"] or m["h2"] or m["h3"]).lower()
    path = m["path"].strip("/")
    if path.endswith("/-") or "/-/" in path:  # web URL of a page inside the project
        path = path.split("/-", 1)[0]
    parts = path.split("/")
    if len(parts) < 2 or not all(_SEGMENT.match(p) for p in parts):
        return None
    return Project(host, path)


class ApiError(YMError):
    def __init__(self, status: int | None, message: str):
        super().__init__(message)
        self.status = status


class Glab:
    """The ``glab`` CLI (login, config) plus a python-gitlab client using glab's login.

    ``host`` may change (e.g. once the project is known); clients are kept per host."""

    def __init__(self, exe: str = "glab", cwd: Path | None = None, host: str = DEFAULT_HOST):
        self.exe = exe
        self.cwd = cwd
        self.host = host
        self._clients: dict[str, pygitlab.Gitlab] = {}

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        console.detail("$ glab " + shlex.join(args))  # never a secret: tokens come back on stdout
        try:
            proc = subprocess.run([self.exe, *args], cwd=self.cwd, stdin=subprocess.DEVNULL,
                                  capture_output=True, check=False)  # fmt: skip
        except FileNotFoundError:
            raise UsageError(f"the GitLab CLI (glab) is not installed: {GLAB_INSTALL}") from None
        if check and proc.returncode != 0:
            err = proc.stderr.decode(errors="replace").strip()
            raise YMError(f"glab {' '.join(args[:3])} failed ({proc.returncode})" +
                          (f": {err}" if err else ""))  # fmt: skip
        return proc

    def config(self, key: str) -> str:
        """``glab config get KEY --host HOST`` ('' when unset or on error)."""
        proc = self.run("config", "get", key, "--host", self.host, check=False)
        return proc.stdout.decode(errors="replace").strip() if proc.returncode == 0 else ""

    # ----------------------------------------------------------------- glab's login

    def token(self) -> str:
        """glab's token for the host: its environment variables, its config file, or (for a
        token in the system keyring) ``glab auth status --show-token``."""
        for name in TOKEN_ENV:
            if os.environ.get(name):
                return os.environ[name]
        token = self.config("token")
        if token:
            return token
        proc = self.run("auth", "status", "--hostname", self.host, "--show-token", check=False)
        text = (proc.stdout + proc.stderr).decode(errors="replace")
        for m in _SHOWN_TOKEN.finditer(text):
            if m.group(1).strip("*"):  # not a masked "****"
                return m.group(1)
        raise UsageError(f"can't read glab's token for {self.host}; log in with"
                         f" `glab auth login --hostname {self.host}`")  # fmt: skip

    def api_url(self) -> str:
        """Base URL of the API host (glab's ``api_host`` / ``api_protocol`` for the host)."""
        host = self.config("api_host") or self.host
        proto = self.config("api_protocol") or "https"
        return f"{proto}://{host}"

    def client(self) -> pygitlab.Gitlab:
        if self.host not in self._clients:
            import gitlab as pygitlab

            from . import __version__

            # Bearer works for personal/project access tokens and OAuth tokens alike
            self._clients[self.host] = pygitlab.Gitlab(
                self.api_url(), oauth_token=self.token(), timeout=60,
                user_agent=f"yourls-manager/{__version__}", retry_transient_errors=True,
            )  # fmt: skip
        return self._clients[self.host]

    def call(self, fn: Callable[[pygitlab.Gitlab], T]) -> T:
        """Run ``fn(client)``; errors become ApiError (with the HTTP status). On a 401 the
        token may be an expired OAuth token: let glab refresh it (any glab API request
        does), then retry once."""
        import gitlab as pygitlab

        for attempt in (1, 2):
            try:
                return fn(self.client())
            except pygitlab.GitlabAuthenticationError as exc:
                if attempt == 1:
                    self.run("api", "--hostname", self.host, "user", check=False)
                    self._clients.pop(self.host, None)
                    continue
                raise ApiError(
                    exc.response_code,
                    f"GitLab {self.host}: {exc.error_message}"
                    f" (check `glab auth status --hostname {self.host}`)",
                ) from None
            except pygitlab.GitlabError as exc:
                raise ApiError(exc.response_code,
                               f"GitLab {self.host}: {exc.error_message}") from None  # fmt: skip
        raise AssertionError("unreachable")

    def get_or_none(self, fn: Callable[[pygitlab.Gitlab], T]) -> T | None:
        """Like call(), but None for a 404."""
        try:
            return self.call(fn)
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise


def _terminal(argv: Sequence[str]) -> int:
    console.info(f"$ {' '.join(argv)}")
    try:
        return subprocess.run(list(argv), check=False).returncode
    except FileNotFoundError:
        return 127


def glab_available(glab: Glab) -> bool:
    return shutil.which(glab.exe) is not None


def glab_logged_in(glab: Glab) -> bool:
    return (
        glab_available(glab)
        and glab.run("auth", "status", "--hostname", glab.host, check=False).returncode == 0
    )


def ensure_glab(glab: Glab, *, confirm: Confirm | None,
                interactive: Interactive | None = None) -> None:  # fmt: skip
    """glab installed and logged in to glab.host; offers `glab auth login` on a terminal."""
    login = f"glab auth login --hostname {glab.host}"
    if not glab_available(glab):
        raise UsageError(f"the GitLab CLI (glab) is not installed: {GLAB_INSTALL}\n"
                         f"Then run `{login}`.")  # fmt: skip
    if glab_logged_in(glab):
        return
    if confirm is None:
        raise UsageError(f"glab is not logged in to {glab.host}; run `{login}` first")
    console.info(f"glab is installed but not logged in to {glab.host}.")
    if not confirm(f"Run `{login}` now?"):
        raise UsageError(f"glab is not logged in; run `{login}` first")
    if (interactive or _terminal)([glab.exe, "auth", "login", "--hostname", glab.host]) != 0:
        raise YMError("`glab auth login` failed")
    if not glab_logged_in(glab):
        raise YMError(f"glab is still not logged in to {glab.host} (see `glab auth status`)")


# --------------------------------------------------------------------------- project


def git_out(root: Path, *args: str) -> str | None:
    proc = run(["git", *args], cwd=root, check=False)
    return proc.stdout.decode(errors="replace").strip() if proc.returncode == 0 else None


def origin_url(root: Path) -> str | None:
    return git_out(root, "config", "--get", "remote.origin.url")


def site_project(root: Path) -> Project | None:
    """The site's GitLab project: from ``origin``, else the template's ``repo_url``."""
    for url in (origin_url(root), template_answer(root, "repo_url")):
        if url:
            proj = project_of(url)
            if proj is not None and proj.host != "github.com":
                return proj
    return None


def _project(gl: pygitlab.Gitlab, proj: Project) -> Any:
    return gl.projects.get(proj.path, lazy=True)


def get_project(glab: Glab, proj: Project) -> dict[str, Any] | None:
    found = glab.get_or_none(lambda gl: gl.projects.get(proj.path))
    return dict(found.attributes) if found is not None else None


def username(glab: Glab) -> str:
    def me(gl: pygitlab.Gitlab) -> str:
        gl.auth()
        assert gl.user is not None
        return str(gl.user.username)

    return glab.call(me)


def planned_project(glab: Glab, root: Path, name: str | None, owner: str | None,
                    host: str | None) -> Project:  # fmt: skip
    """The project `ym repo create` makes: origin's, else repo_url's, else OWNER/NAME."""
    origin = origin_url(root)
    if origin is not None:
        named = project_of(origin)
        if named is None or named.host == "github.com":
            raise UsageError(
                f"origin is already {origin} (not GitLab); this site is stored there. To use"
                " GitLab instead: `git remote rename origin <other>`, then run this again"
            )
        source = "origin"
    else:
        url = template_answer(root, "repo_url") or ""
        named = project_of(url) if url else None
        if named is not None and named.host == "github.com":
            named = None
        source = "repo_url in .copier-answers.yml"
    if named is not None:
        if ((name and name != named.name) or (owner and owner != named.namespace)
                or (host and host.lower() != named.host)):  # fmt: skip
            raise UsageError(f"{source} already names {named}; drop --name/--owner/--host"
                             " or change it")  # fmt: skip
        return named
    name = name or root.name
    if not _SEGMENT.match(name):
        raise UsageError(f"invalid project name {name!r}; pass --name")
    host = (host or glab.host).lower()
    glab.host = host
    owner = owner or username(glab)
    if not all(_SEGMENT.match(p) for p in owner.split("/")):
        raise UsageError(f"invalid --owner {owner!r} (a user or group path)")
    return Project(host, f"{owner}/{name}")


def create_project(cfg: SiteConfig, glab: Glab, proj: Project, *, public: bool = False) -> None:
    """Create ``proj`` on GitLab, make it ``origin`` (unless origin already names it) and
    push the current branch (no tags)."""
    root = cfg.root
    if git_out(root, "rev-parse", "--verify", "-q", "HEAD") is None:
        raise UsageError("this site repo has no commits yet; commit it first")
    glab.host = proj.host
    visibility = "public" if public else "private"
    data: dict[str, Any] = {"name": proj.name, "path": proj.name, "visibility": visibility}
    if proj.namespace != username(glab):
        ns = glab.get_or_none(lambda gl: gl.namespaces.get(proj.namespace))
        if ns is None:
            raise UsageError(f"namespace {proj.namespace!r} not found on {proj.host}"
                             " (or you can't create projects in it)")  # fmt: skip
        data["namespace_id"] = ns.id
    created = glab.call(lambda gl: gl.projects.create(data))
    console.info(f"created {proj} ({visibility})")
    if origin_url(root) is None:
        https = glab.config("git_protocol") == "https"
        url = str(created.http_url_to_repo if https else created.ssh_url_to_repo)
        run(["git", "remote", "add", "origin", url], cwd=root)
        console.info(f"origin = {url}")
    branch = git_out(root, "rev-parse", "--abbrev-ref", "HEAD") or "HEAD"
    push = run(["git", "push", "-u", "origin", branch], cwd=root, check=False, capture=False)
    if push.returncode != 0:
        raise YMError(f"`git push -u origin {branch}` failed; push it by hand")
    console.info(f"pushed {branch}; tags are not pushed (`git push --tags` when you want them)")


def repo_create(cfg: SiteConfig, *, name: str | None = None, owner: str | None = None,
                host: str | None = None, public: bool = False, dry_run: bool = False,
                confirm: Confirm | None = None, glab: Glab | None = None) -> Project:  # fmt: skip
    """`ym repo create` on a GitLab site: store it in a GitLab project."""
    known = site_project(cfg.root)
    glab = glab or Glab(cwd=cfg.root, host=(host or (known.host if known else DEFAULT_HOST)))
    ensure_glab(glab, confirm=confirm)
    proj = planned_project(glab, cfg.root, name, owner, host)
    glab.host = proj.host
    if get_project(glab, proj) is not None:
        if origin_url(cfg.root) is not None:
            console.info(f"this site is already on GitLab: {proj}")
            return proj
        raise UsageError(f"{proj} already exists on {proj.host}; add it as origin"
                         f" (`git remote add origin git@{proj.host}:{proj.path}.git`)"
                         " or pick another --name")  # fmt: skip
    if dry_run:
        console.info(f"would create {proj} ({'public' if public else 'private'}) as origin and"
                     " push the current branch")  # fmt: skip
        return proj
    create_project(cfg, glab, proj, public=public)
    return proj


# --------------------------------------------------------------------------- CI settings


def env_exists(glab: Glab, proj: Project, env: str) -> bool:
    found = glab.call(lambda gl: _project(gl, proj).environments.list(name=env, get_all=True))
    return any(e.name == env for e in found)


def create_env(glab: Glab, proj: Project, env: str) -> None:
    glab.call(lambda gl: _project(gl, proj).environments.create({"name": env}))


def variables(glab: Glab, proj: Project) -> list[dict[str, Any]]:
    found = glab.call(lambda gl: _project(gl, proj).variables.list(get_all=True))
    return [dict(v.attributes) for v in found]


def has_variable(found: list[dict[str, Any]], key: str, scope: str = "*") -> bool:
    return any(v.get("key") == key and v.get("environment_scope", "*") == scope for v in found)


def set_variable(glab: Glab, proj: Project, key: str, value: str, *, scope: str = "*",
                 file: bool = False, protected: bool = False, masked: bool = False,
                 exists: bool = False) -> None:  # fmt: skip
    data = {"value": value, "variable_type": "file" if file else "env_var",
            "protected": protected, "masked": masked, "environment_scope": scope}  # fmt: skip
    if exists:  # several scopes may share the key: address this one
        glab.call(lambda gl: _project(gl, proj).variables.update(
            key, data, query_data={"filter[environment_scope]": scope}))  # fmt: skip
    else:
        glab.call(lambda gl: _project(gl, proj).variables.create({"key": key, **data}))


TAG_PATTERN = "v*"


def tag_protected(glab: Glab, proj: Project) -> bool:
    return (
        glab.get_or_none(lambda gl: _project(gl, proj).protectedtags.get(TAG_PATTERN)) is not None
    )


def protect_tags(glab: Glab, proj: Project) -> None:
    glab.call(lambda gl: _project(gl, proj).protectedtags.create(
        {"name": TAG_PATTERN, "create_access_level": 40}))  # fmt: skip


SCHEDULE = "ym check-updates"
SCHEDULE_CRON = "23 5 * * 1"  # Mondays 05:23 UTC


def find_schedule(glab: Glab, proj: Project) -> dict[str, Any] | None:
    found = glab.call(lambda gl: _project(gl, proj).pipelineschedules.list(get_all=True))
    return next((dict(s.attributes) for s in found if s.description == SCHEDULE), None)


def create_schedule(glab: Glab, proj: Project, ref: str) -> None:
    glab.call(lambda gl: _project(gl, proj).pipelineschedules.create(
        {"description": SCHEDULE, "ref": ref, "cron": SCHEDULE_CRON,
         "cron_timezone": "UTC", "active": True}))  # fmt: skip


def create_access_token(glab: Glab, proj: Project, expires: str) -> str:
    """A project access token for check-updates (Developer, write_repository)."""
    token = glab.call(lambda gl: _project(gl, proj).access_tokens.create(
        {"name": SCHEDULE, "scopes": ["write_repository"], "access_level": 30,
         "expires_at": expires}))  # fmt: skip
    return str(token.token)


def project_api_path(proj: Project) -> str:
    """REST path of the project (for messages and docs)."""
    return "projects/" + quote(proj.path, safe="")
