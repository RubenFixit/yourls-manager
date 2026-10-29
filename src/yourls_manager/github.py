"""GitHub via the ``gh`` CLI, and `ym repo create`.

`ym repo create` only stores the site on GitHub: create the repository (private unless
``--public``), make it ``origin`` and push the current branch, without tags (a pushed
release tag starts the deploy workflow on CI-enabled sites). Deploying from GitHub Actions
is a separate, opt-in step: `ym ci setup` (``ci.py``).
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from .config import SiteConfig, ci_provider
from .util import UsageError, YMError, console, run

GH_INSTALL = "https://cli.github.com (Debian/Ubuntu/WSL: `sudo apt install gh`)"

Confirm = Callable[[str], bool]
Interactive = Callable[[Sequence[str]], int]

_GITHUB_REMOTE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"(?P<repo>[^/\s]+/[^/\s]+?)(?:\.git)?/?$"
)
_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


class Gh:
    """Thin wrapper around the ``gh`` CLI (no shell; stdin for secret values)."""

    def __init__(self, exe: str = "gh", cwd: Path | None = None):
        self.exe = exe
        self.cwd = cwd

    def run(self, *args: str, input: bytes | None = None, check: bool = True
            ) -> subprocess.CompletedProcess[bytes]:  # fmt: skip
        console.detail("$ gh " + shlex.join(args))
        try:
            proc = subprocess.run([self.exe, *args], cwd=self.cwd, input=input,
                                  capture_output=True, check=False)  # fmt: skip
        except FileNotFoundError:
            raise UsageError(f"the GitHub CLI (gh) is not installed: {GH_INSTALL}") from None
        if check and proc.returncode != 0:
            err = proc.stderr.decode(errors="replace").strip()
            raise YMError(f"gh {' '.join(args[:3])} failed ({proc.returncode})" +
                          (f": {err}" if err else ""))  # fmt: skip
        return proc

    def out(self, *args: str) -> str:
        return self.run(*args).stdout.decode(errors="replace").strip()


def _terminal(argv: Sequence[str]) -> int:
    console.info(f"$ {' '.join(argv)}")
    try:
        return subprocess.run(list(argv), check=False).returncode
    except FileNotFoundError:
        return 127


def gh_available(gh: Gh) -> bool:
    return shutil.which(gh.exe) is not None


def gh_logged_in(gh: Gh) -> bool:
    return gh_available(gh) and gh.run("auth", "status", check=False).returncode == 0


def ensure_gh(gh: Gh, *, confirm: Confirm | None, interactive: Interactive | None = None) -> None:
    """gh installed and authenticated; offers `gh auth login` when a terminal is there."""
    if not gh_available(gh):
        raise UsageError(f"the GitHub CLI (gh) is not installed: {GH_INSTALL}\n"
                         "Then run `gh auth login`.")  # fmt: skip
    if gh.run("auth", "status", check=False).returncode == 0:
        return
    if confirm is None:
        raise UsageError("gh is not logged in to GitHub; run `gh auth login` first")
    console.info("gh is installed but not logged in to GitHub.")
    if not confirm("Run `gh auth login` now?"):
        raise UsageError("gh is not logged in; run `gh auth login` first")
    if (interactive or _terminal)([gh.exe, "auth", "login"]) != 0:
        raise YMError("`gh auth login` failed")
    if gh.run("auth", "status", check=False).returncode != 0:
        raise YMError("gh is still not logged in (see `gh auth status`)")


# --------------------------------------------------------------------------- repo


def github_repo_of(url: str) -> str | None:
    m = _GITHUB_REMOTE.match(url.strip())
    return m["repo"] if m else None


def git_out(root: Path, *args: str) -> str | None:
    proc = run(["git", *args], cwd=root, check=False)
    return proc.stdout.decode(errors="replace").strip() if proc.returncode == 0 else None


def origin_url(root: Path) -> str | None:
    """``remote.origin.url`` as written (``remote get-url`` would apply insteadOf)."""
    return git_out(root, "config", "--get", "remote.origin.url")


def repo_exists(gh: Gh, repo: str) -> bool:
    return gh.run("repo", "view", repo, "--json", "name", check=False).returncode == 0


def current_repo(gh: Gh) -> str | None:
    """OWNER/NAME of the site's existing GitHub repository (from its remotes), or None."""
    proc = gh.run("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner",
                  check=False)  # fmt: skip
    name = proc.stdout.decode(errors="replace").strip()
    return name if proc.returncode == 0 and "/" in name else None


def planned_repo(gh: Gh, root: Path, name: str | None, owner: str | None) -> str:
    """The repository `ym repo create` makes: origin's GitHub repo, else OWNER/NAME."""
    origin = origin_url(root)
    if origin is not None:
        named = github_repo_of(origin)
        if named is None:
            raise UsageError(
                f"origin is already {origin} (not GitHub); this site is stored there. To use"
                " GitHub instead: `git remote rename origin <other>`, then run this again"
            )
        if (name and name != named.split("/")[1]) or (owner and owner != named.split("/")[0]):
            raise UsageError(f"origin already names {named}; drop --name/--owner or change origin")
        return named
    name = name or root.name
    if not _NAME.match(name):
        raise UsageError(f"invalid repository name {name!r}; pass --name")
    owner = owner or gh.out("api", "user", "-q", ".login")
    return f"{owner}/{name}"


def create_repo(cfg: SiteConfig, gh: Gh, repo: str, *, public: bool = False) -> None:
    """Create ``repo`` on GitHub as ``origin`` and push the current branch (no tags)."""
    root = cfg.root
    if git_out(root, "rev-parse", "--verify", "-q", "HEAD") is None:
        raise UsageError("this site repo has no commits yet; commit it first")
    visibility = "public" if public else "private"
    if origin_url(root) is not None:  # origin already names this (missing) GitHub repo
        gh.run("repo", "create", repo, f"--{visibility}")
    else:
        gh.run("repo", "create", repo, f"--{visibility}", "--source", str(root),
               "--remote", "origin")  # fmt: skip
    console.info(f"created {repo} ({visibility}) as origin")
    branch = git_out(root, "rev-parse", "--abbrev-ref", "HEAD") or "HEAD"
    push = run(["git", "push", "-u", "origin", branch], cwd=root, check=False, capture=False)
    if push.returncode != 0:
        raise YMError(f"`git push -u origin {branch}` failed; push it by hand")
    console.info(f"pushed {branch}; tags are not pushed (`git push --tags` when you want them)")


def require_github(cfg: SiteConfig, command: str, *, allow_none: bool = False) -> None:
    """GitHub-only commands refuse sites whose template CI is GitLab."""
    provider = ci_provider(cfg.root)
    if provider == "gitlab":
        raise UsageError(
            f"`ym {command}` works with GitHub only, and this site uses GitLab CI"
            " (ci_provider = gitlab in .copier-answers.yml). Set up the GitLab project, its"
            ' environments and CI/CD variables by hand: see "GitLab CI" in the site README'
        )
    if provider == "none" and not allow_none:
        raise UsageError(
            f"`ym {command}` sets up the template's GitHub Actions workflows, but this site has"
            " none (ci_provider = none). Add them with"
            " `ym template update --data ci_provider=github`"
        )


def repo_create(cfg: SiteConfig, *, name: str | None = None, owner: str | None = None,
                public: bool = False, dry_run: bool = False,
                confirm: Confirm | None = None, gh: Gh | None = None) -> str:  # fmt: skip
    """`ym repo create`: store the site on GitHub. Returns OWNER/NAME."""
    require_github(cfg, "repo create", allow_none=True)
    gh = gh or Gh(cwd=cfg.root)
    ensure_gh(gh, confirm=confirm)
    existing = current_repo(gh)
    if existing and repo_exists(gh, existing):
        console.info(f"this site is already on GitHub: {existing}")
        return existing
    repo = planned_repo(gh, cfg.root, name, owner)
    if repo_exists(gh, repo):
        raise UsageError(f"{repo} already exists on GitHub; add it as origin"
                         f" (`git remote add origin https://github.com/{repo}.git`)"
                         " or pick another --name")  # fmt: skip
    if dry_run:
        console.info(f"would create {repo} ({'public' if public else 'private'}) as origin and"
                     " push the current branch")  # fmt: skip
        return repo
    create_repo(cfg, gh, repo, public=public)
    return repo
