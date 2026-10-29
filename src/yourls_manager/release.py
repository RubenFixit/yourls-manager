"""`ym release` and the deploy policy (design §11.4, §11.5).

Release tags: ``v<yourls>-<rev>`` with an optional ``-rc.<n>``, e.g. ``v1.10.6-1``,
``v1.10.6-2-rc.1``, ``v1.10.6-2``. ``<yourls>`` is the locked core version; ``<rev>``
counts from 1 per YOURLS version. Other tags are ignored.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .config import LOCK_FILE, SiteConfig, TargetSpec
from .lockfile import Lock, load_lock, parse_lock
from .util import UsageError, YMError, console, run

TAG_RE = re.compile(r"^v(?P<core>\d+\.\d+(?:\.\d+)?)-(?P<rev>[1-9]\d*)(?:-rc\.(?P<rc>[1-9]\d*))?$")
MARKER_PREFIX = "deployed/"


@dataclass(frozen=True, order=True)
class ReleaseTag:
    core: tuple[int, ...]
    rev: int
    rc: int  # 0 = final; sorts after every rc because final_rank below
    name: str = ""

    @property
    def is_rc(self) -> bool:
        return self.rc > 0

    @property
    def core_str(self) -> str:
        return ".".join(map(str, self.core))

    @property
    def sort_key(self) -> tuple:
        # finals sort after their release candidates
        return (self.core, self.rev, self.rc if self.rc else 10**9)


def parse_tag(name: str) -> ReleaseTag | None:
    m = TAG_RE.match(name)
    if not m:
        return None
    core = tuple(int(x) for x in m["core"].split("."))
    return ReleaseTag(core, int(m["rev"]), int(m["rc"] or 0), name)


def format_tag(core: str, rev: int, rc: int = 0) -> str:
    return f"v{core}-{rev}" + (f"-rc.{rc}" if rc else "")


# --------------------------------------------------------------------------- git


def _git(root: Path, *args: str, check: bool = True) -> str:
    return run(["git", *args], cwd=root, check=check).stdout.decode().strip()


def is_git_repo(root: Path) -> bool:
    return run(["git", "rev-parse", "--git-dir"], cwd=root, check=False).returncode == 0


def is_dirty(root: Path) -> bool:
    return bool(_git(root, "status", "--porcelain"))


def release_tags(root: Path) -> list[ReleaseTag]:
    tags = [parse_tag(t) for t in _git(root, "tag", "--list", "v*").splitlines()]
    return sorted((t for t in tags if t), key=lambda t: t.sort_key)


def tags_at(root: Path, rev: str = "HEAD") -> list[ReleaseTag]:
    out = _git(root, "tag", "--points-at", rev, check=False)
    tags = [parse_tag(t) for t in out.splitlines()]
    return sorted((t for t in tags if t), key=lambda t: t.sort_key)


def commit_of(root: Path, ref: str) -> str | None:
    proc = run(["git", "rev-parse", "--verify", "-q", f"{ref}^{{commit}}"], cwd=root, check=False)
    return proc.stdout.decode().strip() or None


# --------------------------------------------------------------------------- versions


def next_tag(tags: list[ReleaseTag], core: str, *, rc: bool) -> str:
    """The tag ``ym release`` creates next for this core version."""
    same = [t for t in tags if t.core_str == core]
    if not same:
        return format_tag(core, 1, 1 if rc else 0)
    top = max(same, key=lambda t: t.sort_key)
    if top.is_rc:  # an rc series without its final yet
        return format_tag(core, top.rev, top.rc + 1 if rc else 0)
    return format_tag(core, top.rev + 1, 1 if rc else 0)


def _short(commit: str | None) -> str:
    return commit[:7] if commit else "?"


def lock_diff(old: Lock | None, new: Lock) -> list[str]:
    """Human summary of what changed between two locks."""
    lines: list[str] = []
    oc = old.core if old else None
    if new.core and (not oc or oc.commit != new.core.commit):
        lines.append(
            f"YOURLS {oc.version} -> {new.core.version}" if oc else f"YOURLS {new.core.version}"
        )
    oldp = old.plugins if old else {}
    for name, p in sorted(new.plugins.items()):
        if name not in oldp:
            lines.append(f"added plugin {name} ({_short(p.commit)})")
        elif oldp[name].commit != p.commit:
            lines.append(f"plugin {name} {_short(oldp[name].commit)} -> {_short(p.commit)}")
    for name in sorted(set(oldp) - set(new.plugins)):
        lines.append(f"removed plugin {name}")
    return lines


def lock_at(root: Path, ref: str) -> Lock | None:
    proc = run(["git", "show", f"{ref}:{LOCK_FILE}"], cwd=root, check=False)
    if proc.returncode != 0:
        return None
    try:
        return parse_lock(json.loads(proc.stdout.decode()))
    except (ValueError, YMError):
        return None


def release_notes(cfg: SiteConfig, tags: list[ReleaseTag], message: str | None) -> str:
    finals = [t for t in tags if not t.is_rc]
    prev = finals[-1].name if finals else None
    changes = lock_diff(lock_at(cfg.root, prev) if prev else None, load_lock(cfg.lock_file))
    log = (
        _git(cfg.root, "log", "--format=- %s", f"{prev}..HEAD", check=False)
        if prev
        else _git(cfg.root, "log", "--format=- %s", "-n", "20", check=False)
    )
    parts = [f"Changes since {prev}:" if prev else "First release."]
    parts += [f"- {c}" for c in changes] or ["- no YOURLS or plugin changes"]
    if message:
        parts += ["", message.strip()]
    if log:
        parts += ["", "Commits:", log]
    return "\n".join(parts) + "\n"


# --------------------------------------------------------------------------- ym release


def _require_clean(cfg: SiteConfig) -> None:
    from .drift import compute_drift

    if not is_git_repo(cfg.root):
        raise UsageError(f"{cfg.root} is not a git repository")
    if is_dirty(cfg.root):
        raise UsageError("uncommitted changes: commit them first (`git status`)")
    if not compute_drift(cfg, load_lock(cfg.lock_file)).clean:
        raise UsageError("yourls/ differs from yourls.lock.json (see `ym doctor --local`)")


def has_remote(root: Path, name: str | None = None) -> bool:
    """Any git remote (or the named one)."""
    remotes = _git(root, "remote", check=False).split()
    return bool(remotes) if name is None else name in remotes


def on_remote_branch(root: Path, rev: str = "HEAD") -> bool:
    return bool(_git(root, "branch", "-r", "--contains", rev, check=False))


def release(
    cfg: SiteConfig,
    *,
    rc: bool = False,
    promote: bool = False,
    push: bool = False,
    dry_run: bool = False,
    message: str | None = None,
) -> str:
    from .build import build_site

    _require_clean(cfg)
    if push and not has_remote(cfg.root, "origin"):
        raise UsageError(
            "--push: no git remote `origin`. Add one (`git remote add origin <url>`) or create"
            " a GitHub repository with `ym repo create`; or release without --push"
        )
    tags = release_tags(cfg.root)
    if promote:
        rcs = [t for t in tags if t.is_rc]
        if not rcs:
            raise UsageError("no release candidate to promote; create one with `ym release --rc`")
        cand = rcs[-1]
        name = format_tag(cand.core_str, cand.rev)
        if commit_of(cfg.root, f"refs/tags/{name}"):
            raise UsageError(f"{name} already exists; the newest rc ({cand.name}) was released")
        target_rev = commit_of(cfg.root, cand.name) or cand.name  # the commit, not the rc tag
    else:
        lock = load_lock(cfg.lock_file)
        if not lock.core:
            raise UsageError("no core in yourls.lock.json; run `ym core` first")
        name = next_tag(tags, lock.core.version.removeprefix("v"), rc=rc)
        target_rev = "HEAD"
        existing = [t.name for t in tags_at(cfg.root) if not t.is_rc or rc]
        if existing:
            raise UsageError(f"HEAD is already released as {', '.join(existing)}")
    notes = release_notes(cfg, tags, message)
    console.info(f"release {name} at {_short(commit_of(cfg.root, target_rev))}")
    for line in notes.splitlines():
        console.info(f"  {line}")
    if dry_run:
        console.info("dry run: no tag created")
        return name
    if not promote:
        console.step("build")
        build_site(cfg)
    _git(cfg.root, "tag", "-a", name, "-m", f"{name}\n\n{notes}", target_rev)
    console.info(f"tagged {name}")
    if push:
        console.step(f"push {name}")
        if not on_remote_branch(cfg.root, target_rev):
            console.warn(
                "the released commit is not on any remote branch: push your branch too"
                " (`git push`), or the tag points at a commit no branch on origin has"
            )
        run(["git", "push", "origin", f"refs/tags/{name}"], cwd=cfg.root)
    else:
        console.info(f"push it with: git push origin {name}")
    return name


def list_releases(cfg: SiteConfig) -> None:
    tags = release_tags(cfg.root)
    markers = {t: commit_of(cfg.root, MARKER_PREFIX + t) for t in cfg.targets}
    if not tags:
        console.info("no releases yet (create one with `ym release`)")
    for t in reversed(tags):
        commit = commit_of(cfg.root, t.name)
        date = _git(cfg.root, "log", "-1", "--format=%cs", t.name, check=False)
        live = [name for name, c in markers.items() if c and c == commit]
        suffix = f"  <- deployed: {', '.join(live)}" if live else ""
        console.info(f"{t.name:<22} {date}  {_short(commit)}{suffix}")


# --------------------------------------------------------------------------- deploy policy


def check_deploy(
    cfg: SiteConfig, spec: TargetSpec, *, allow_dirty: bool, force: bool
) -> dict[str, object]:
    """Enforce the target's ``accept`` policy before a deploy. Returns release.json extras."""
    root = cfg.root
    if not is_git_repo(root):
        if spec.accept == "release" and not force:
            raise UsageError(f"target {spec.name} only accepts releases, but this isn't a git repo")
        console.warn("not a git repository: the deploy can't be traced to a commit")
        return {"tag": None, "forced": force}
    dirty = is_dirty(root)
    if dirty and not allow_dirty:
        raise UsageError(
            "uncommitted changes: commit first, or pass --allow-dirty to deploy them anyway"
        )
    at_head = tags_at(root)
    finals = [t.name for t in at_head if not t.is_rc]
    tag = finals[-1] if finals else (at_head[-1].name if at_head else None)
    forced = False
    if spec.accept == "release" and (dirty or not finals):
        why = "there are uncommitted changes" if dirty else "HEAD has no final release tag"
        if not force:
            raise UsageError(
                f'target {spec.name} only accepts releases (accept = "release") and {why}.\n'
                "Create one with `ym release` (or promote an rc with `ym release promote`),"
                " check it out, and deploy again; --force overrides."
            )
        console.warn(f"FORCED deploy to {spec.name}: {why} (recorded in release.json)")
        forced = True
    if not dirty and has_remote(root) and not on_remote_branch(root):
        console.warn("HEAD is not on any remote branch yet: push it so the deploy is traceable")
    return {"tag": tag, "forced": forced}


def move_marker(cfg: SiteConfig, spec: TargetSpec) -> None:
    """Point ``deployed/<target>`` at the deployed commit (clean deploys only)."""
    root = cfg.root
    if not is_git_repo(root) or is_dirty(root):
        return
    branch = MARKER_PREFIX + spec.name
    _git(root, "branch", "-f", branch, "HEAD")
    console.info(f"marker {branch} -> {_short(commit_of(root, 'HEAD'))}")
    if cfg.push_markers and not has_remote(root, "origin"):
        console.warn(f"[release] push_markers: no git remote `origin`; {branch} stays local")
    elif cfg.push_markers:
        proc = run(["git", "push", "-f", "origin", f"{branch}:refs/heads/{branch}"],
                   cwd=root, check=False)  # fmt: skip
        if proc.returncode != 0:
            console.warn(f"could not push {branch}: {proc.stderr.decode().strip()}")
