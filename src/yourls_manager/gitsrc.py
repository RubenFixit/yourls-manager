"""Git operations: ls-remote, fetch a commit, pick semver tags."""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .util import YMError, console, run

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
STABLE_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true", "LC_ALL": "C"}


def git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    proc = run(["git", *args], cwd=cwd, env=_GIT_ENV, check=check)
    return proc.stdout.decode(errors="replace").strip()


def _check_url(url: str) -> None:
    if not url or url.startswith("-"):
        raise YMError(f"invalid git URL {url!r}")


def ls_remote(url: str, *patterns: str) -> dict[str, str]:
    """Return {refname: sha}; annotated tags are reported with their peeled commit."""
    _check_url(url)
    out = git("ls-remote", "--", url, *patterns)
    refs: dict[str, str] = {}
    peeled: dict[str, str] = {}
    for line in out.splitlines():
        if "\t" not in line:
            continue
        sha, ref = line.split("\t", 1)
        if ref.endswith("^{}"):
            peeled[ref[:-3]] = sha
        else:
            refs[ref] = sha
    refs.update(peeled)
    return refs


def parse_stable_semver(tag: str) -> tuple[int, int, int] | None:
    m = STABLE_TAG_RE.match(tag)
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def remote_tags(url: str) -> dict[str, str]:
    refs = ls_remote(url, "refs/tags/*")
    return {r.removeprefix("refs/tags/"): sha for r, sha in refs.items()}


def latest_stable_tag(tags: dict[str, str]) -> tuple[str, str] | None:
    """Highest stable X.Y.Z tag (with or without 'v'); -rc/-beta etc. are skipped."""
    best: tuple[tuple[int, int, int], int, str] | None = None
    for tag in tags:
        ver = parse_stable_semver(tag)
        if ver is None:
            continue
        # prefer the un-prefixed spelling when both exist
        key = (ver, 0 if tag.startswith("v") else 1, tag)
        if best is None or key > best:
            best = key
    if best is None:
        return None
    return best[2], tags[best[2]]


def resolve_core_tag(url: str, version: str) -> tuple[str, str]:
    """Return (tag, commit) for a core version; 'latest' picks the highest stable tag."""
    tags = remote_tags(url)
    if version == "latest":
        found = latest_stable_tag(tags)
        if not found:
            raise YMError(f"no stable X.Y.Z tags found at {url}")
        return found
    for candidate in (version, f"v{version}", version.removeprefix("v")):
        if candidate in tags:
            return candidate, tags[candidate]
    raise YMError(f"tag {version!r} not found at {url}")


def resolve_ref(url: str, ref: str) -> str:
    """Resolve a branch, tag, HEAD or full sha to a commit sha via ls-remote."""
    if SHA_RE.match(ref):
        return ref
    refs = ls_remote(url)
    if ref == "HEAD":
        if "HEAD" in refs:
            return refs["HEAD"]
        raise YMError(f"{url}: remote has no HEAD")
    for candidate in (ref, f"refs/heads/{ref}", f"refs/tags/{ref}"):
        if candidate in refs:
            return refs[candidate]
    raise YMError(f"{url}: ref {ref!r} not found (branch, tag, HEAD or 40-char sha expected)")


@dataclass
class Checkout:
    path: Path
    commit: str


def fetch_commit(url: str, commit: str, dest: Path) -> Checkout:
    """Materialize ``commit`` of ``url`` at ``dest`` (which must not exist).

    Tries a shallow fetch of the exact sha first, falling back to a full clone when the
    server refuses sha wants.
    """
    _check_url(url)
    if not SHA_RE.match(commit):
        raise YMError(f"not a full commit sha: {commit!r}")
    dest.mkdir(parents=True)
    git("init", "-q", cwd=dest)
    git("remote", "add", "origin", url, cwd=dest)
    shallow = run(
        ["git", "fetch", "-q", "--depth", "1", "origin", commit],
        cwd=dest,
        env=_GIT_ENV,
        check=False,
    )
    if shallow.returncode != 0:
        console.detail(f"shallow sha fetch refused by {url}; falling back to full fetch")
        git("fetch", "-q", "--tags", "origin", "+refs/heads/*:refs/remotes/origin/*", cwd=dest)
        target = commit
    else:
        target = "FETCH_HEAD"
    _reject_links(dest, target)
    git("-c", "advice.detachedHead=false", "checkout", "-q", "--detach", target, cwd=dest)
    head = git("rev-parse", "HEAD", cwd=dest)
    if head != commit:
        raise YMError(f"{url}: checked out {head}, expected {commit}")
    return Checkout(dest, head)


def _reject_links(repo: Path, rev: str) -> None:
    for entry in git("ls-tree", "-r", rev, cwd=repo).splitlines():
        mode, _, rest = entry.partition(" ")
        if mode == "160000":
            console.warn(f"submodule {rest.split(chr(9))[-1]!r} is not fetched (left empty)")


def temp_checkout_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="ym-git-"))
