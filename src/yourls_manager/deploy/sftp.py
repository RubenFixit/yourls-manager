"""``type = "sftp"`` deploy target.

There is no rsync and no PHP CLI on the server, so ym keeps a manifest of what it deployed
in ``<state_dir>/manifest.json`` (outside the web root) and computes the changes itself:

- upload new files and files whose sha256 changed (everything when there is no manifest);
- delete files that the previous manifest lists but ``dist/`` no longer has, unless
  ``protect``ed. Files ym never deployed are never deleted.

YOURLS install/upgrade state is read over HTTP, and plugin activation is left to the admin
UI (YOURLS has no API for it); ym prints the manual steps.
"""

from __future__ import annotations

import json
import posixpath
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import __version__
from ..build import build_site
from ..config import REDIRECTS_FILE, SiteConfig, TargetSpec, join_remote
from ..transport.sftp import SftpTransport
from ..util import UsageError, YMError, console, iso_utc
from ..vendor import file_sha256, glob_match, iter_files
from ..verify import fetch, verify
from .base import DeployOptions, Target, release_info

MANIFEST_FILE = "manifest.json"
RELEASE_FILE = "release.json"
# Uploaded last, so requests only reach the new code once everything else is in place.
ENTRY_POINTS = (".htaccess", "user/config.php", "yourls-loader.php")
REDIRECT_CODES = (301, 302, 303, 307, 308)
BACKUP_NOTE = (
    "SFTP deploys don't back up the database (no shell on the server); use your host's"
    " backup tool before deploying"
)

Manifest = dict[str, dict[str, Any]]  # relpath -> {"sha256": hex, "size": int}


# --------------------------------------------------------------------------- planning


def local_manifest(dist: Path) -> Manifest:
    return {
        rel: {"sha256": file_sha256(path), "size": path.stat().st_size}
        for rel, path in iter_files(dist)
    }


def protect_matcher(patterns: Iterable[str]):
    """rsync-like ``protect`` patterns: a leading ``/`` (or an inner ``/``) anchors at the
    web root, otherwise the pattern matches at any depth; a match on a directory covers
    everything below it."""
    pats = []
    for raw in patterns:
        core = raw.strip().strip("/")
        if not core:
            continue
        anchored = raw.strip().startswith("/") or "/" in core
        pats.append(core if anchored else f"**/{core}")

    def protected(rel: str) -> bool:
        return any(glob_match(p, rel) for p in pats)

    return protected


@dataclass
class SyncPlan:
    upload: list[str] = field(default_factory=list)  # in upload order (entry points last)
    reasons: dict[str, str] = field(default_factory=dict)  # new | changed | server-changed
    delete: list[str] = field(default_factory=list)
    protected: list[str] = field(default_factory=list)  # gone from dist, kept (protect)
    modified_kept: list[str] = field(default_factory=list)  # gone from dist, edited on server
    unchanged: int = 0
    has_manifest: bool = False

    @property
    def empty(self) -> bool:
        return not self.upload and not self.delete

    def count(self, reason: str) -> int:
        return sum(1 for r in self.reasons.values() if r == reason)


def upload_order(paths: Iterable[str]) -> list[str]:
    paths = set(paths)
    entry = [p for p in ENTRY_POINTS if p in paths]
    return sorted(paths - set(entry)) + entry


def compute_plan(
    local: Manifest,
    remote: Manifest | None,
    protected,
    server_state: dict[str, int | None] | None = None,
) -> SyncPlan:
    """``server_state`` (``--verify-remote``): relpath -> size on the server (None: missing),
    for every file in the remote manifest."""
    plan = SyncPlan(has_manifest=remote is not None)
    remote = remote or {}
    for rel, info in local.items():
        old = remote.get(rel)
        if old is None:
            plan.reasons[rel] = "new"
        elif old.get("sha256") != info["sha256"]:
            plan.reasons[rel] = "changed"
        elif server_state is not None and server_state.get(rel) != old.get("size"):
            plan.reasons[rel] = "server-changed"
        else:
            plan.unchanged += 1
    plan.upload = upload_order(plan.reasons)
    for rel in sorted(remote.keys() - local.keys()):
        if protected(rel):
            plan.protected.append(rel)
        elif server_state is not None and server_state.get(rel) is None:
            continue  # already gone
        elif server_state is not None and server_state.get(rel) != remote[rel].get("size"):
            plan.modified_kept.append(rel)
        else:
            plan.delete.append(rel)
    return plan


def parent_dirs(rel: str) -> list[str]:
    """``a/b/c.php`` -> ``["a", "a/b"]``."""
    parts = rel.split("/")[:-1]
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


# --------------------------------------------------------------------------- http state


def yourls_state(url: str, max_hops: int = 5) -> tuple[str, str | None]:
    """Request ``<url>/`` without following redirects (same-site hops are inspected one by
    one). Returns ("install" | "upgrade", where) when YOURLS redirects to its install or
    upgrade page, ("ok", None) otherwise, ("unknown", error) when unreachable."""
    origin = urllib.parse.urlsplit(url)[:2]
    current = url + "/"
    seen: set[str] = set()
    for _ in range(max_hops):
        seen.add(current)
        try:
            resp = fetch(current)
        except YMError as exc:
            return "unknown", str(exc)
        if resp.status not in REDIRECT_CODES or not resp.location:
            return "ok", None
        nxt = urllib.parse.urljoin(current, resp.location)
        path = urllib.parse.urlsplit(nxt).path
        if path.endswith("/admin/install.php"):
            return "install", nxt
        if path.endswith("/admin/upgrade.php"):
            return "upgrade", nxt
        if urllib.parse.urlsplit(nxt)[:2] != origin or nxt in seen:
            return "ok", None
        current = nxt
    return "ok", None


def manual_steps(cfg: SiteConfig, spec: TargetSpec, state: str | None = None) -> list[str]:
    """What the admin has to do in the browser after an SFTP deploy."""
    steps: list[str] = []
    if state == "install":
        steps.append(f"install YOURLS (creates the tables): open {spec.url}/admin/install.php")
    elif state == "upgrade":
        steps.append(f"upgrade the database: open {spec.url}/admin/upgrade.php and run it")
    elif state is None:
        steps.append(
            f"if `ym deploy {spec.name}` says so: open {spec.url}/admin/install.php (new"
            f" database) or {spec.url}/admin/upgrade.php (after a core update)"
        )
    active = cfg.active_plugins
    steps.append(
        f"plugins: ym can't (de)activate plugins over SFTP. In {spec.url}/admin/plugins.php"
        " make exactly these active: " + (", ".join(active) if active else "(none)")
    )
    return steps


# --------------------------------------------------------------------------- target


class SftpTarget(Target):
    def __init__(self, cfg: SiteConfig, spec: TargetSpec, transport: SftpTransport | None = None):
        super().__init__(cfg, spec)
        self.transport = transport or SftpTransport.for_target(spec)
        self._dirs: set[str] = set()

    # ------------------------------------------------------------------ paths
    @property
    def web_root(self) -> str:
        return self.transport.abspath(self.spec.path)

    @property
    def config_file(self) -> str:
        return self.transport.abspath(self.spec.config_file)

    @property
    def state_dir(self) -> str:
        return self.transport.abspath(self.spec.state_dir)

    @property
    def manifest_file(self) -> str:
        return join_remote(self.state_dir, MANIFEST_FILE)

    @property
    def release_file(self) -> str:
        return join_remote(self.state_dir, RELEASE_FILE)

    def remote(self, rel: str) -> str:
        return join_remote(self.web_root, rel)

    # ------------------------------------------------------------------ steps
    def preflight(self, *, create: bool, need_config: bool = True) -> None:
        t = self.transport
        console.step(f"preflight ({t.describe()})")
        t.connect()
        console.detail(f"login dir: {t.home()}")
        root = self.web_root
        st = t.stat(root)
        if st is None:
            if create:
                t.mkdirs(root)
                console.info(f"  created web root {root}")
            else:
                console.info(f"  web root {root} does not exist yet (deploy creates it)")
        elif not t.is_dir(root):
            raise YMError(f"{t.describe()}:{root} exists but is not a directory")
        else:
            console.info(f"  web root {root}")
        if not need_config:
            return
        if not t.is_file(self.config_file):
            raise UsageError(
                f"server-local config not found: {self.config_file} on {t.describe()}\n"
                f"Create it with `ym target config {self.spec.name}` (or"
                f" `ym target config {self.spec.name} --from-existing` when adopting)."
            )
        console.info(f"  config {self.config_file}")

    def read_manifest(self) -> Manifest | None:
        t = self.transport
        if not t.is_file(self.manifest_file):
            return None
        try:
            data = json.loads(t.read_bytes(self.manifest_file).decode("utf-8"))
            files = data["files"]
            if not isinstance(files, dict):
                raise TypeError("files is not an object")
            return {str(k): dict(v) for k, v in files.items()}
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            console.warn(
                f"{self.manifest_file} is unreadable ({exc}); treating this as a first deploy"
            )
            return None

    def server_state(self, remote: Manifest) -> dict[str, int | None]:
        console.info(f"  --verify-remote: checking {len(remote)} deployed file(s) on the server")
        sizes: dict[str, int | None] = {}
        for rel in remote:
            st = self.transport.stat(self.remote(rel))
            sizes[rel] = None if st is None else st.st_size
        return sizes

    def plan(self, dist: Path, *, verify_remote: bool = False) -> tuple[SyncPlan, Manifest]:
        console.step("plan")
        local = local_manifest(dist)
        remote = self.read_manifest()
        server = self.server_state(remote) if (verify_remote and remote is not None) else None
        if verify_remote and remote is None:
            console.info("  --verify-remote: no manifest yet, nothing to check")
        plan = compute_plan(local, remote, protect_matcher(self.spec.protect), server)
        for rel in sorted(r for r, why in plan.reasons.items() if why == "server-changed"):
            console.warn(f"{rel} was changed on the server since the last deploy; re-uploading")
        for rel in plan.modified_kept:
            console.warn(f"{rel} was changed on the server and is no longer deployed; not deleting")
        return plan, local

    def print_plan(self, plan: SyncPlan) -> None:
        if not plan.has_manifest:
            console.info(
                f"  no manifest at {self.manifest_file}: first deploy with ym. Uploading every"
                " file and deleting nothing (ym only deletes files it deployed itself)."
            )
        changed = f"{plan.count('changed')} changed"
        if plan.count("server-changed"):
            changed += f", {plan.count('server-changed')} changed on the server"
        console.info(
            f"  upload {len(plan.upload)} ({plan.count('new')} new, {changed}),"
            f" delete {len(plan.delete)}, unchanged {plan.unchanged}"
        )
        for rel in plan.upload:
            console.detail(f"upload {rel} ({plan.reasons[rel]})")
        for rel in plan.delete:
            console.detail(f"delete {rel}")
        if plan.protected:
            console.info(f"  kept (protect): {len(plan.protected)}")
            for rel in plan.protected:
                console.detail(f"keep   {rel} (protect)")

    def _ensure_dir(self, rel: str) -> None:
        if not rel or rel in self._dirs:
            return
        parent = posixpath.dirname(rel)
        self._ensure_dir(parent)
        path = self.remote(rel)
        if not self.transport.is_dir(path):
            self.transport.mkdirs(path)
        self._dirs.add(rel)

    def sync(self, plan: SyncPlan, dist: Path, local: Manifest) -> None:
        t = self.transport
        console.step(f"upload -> {t.describe()}:{self.web_root}")
        for i, rel in enumerate(plan.upload, 1):
            self._ensure_dir(posixpath.dirname(rel))
            t.upload(dist / rel, self.remote(rel))
            console.detail(f"[{i}/{len(plan.upload)}] {rel}")
        console.info(f"  uploaded {len(plan.upload)} file(s)")
        if not plan.delete:
            return
        console.step("delete files removed from the build")
        for rel in plan.delete:
            try:
                t.remove(self.remote(rel))
                console.detail(f"deleted {rel}")
            except YMError as exc:
                if t.exists(self.remote(rel)):
                    raise
                console.detail(f"already gone: {rel} ({exc})")
        console.info(f"  deleted {len(plan.delete)} file(s)")
        keep = {d for rel in local for d in parent_dirs(rel)}
        candidates = {d for rel in plan.delete for d in parent_dirs(rel)} - keep
        removed = 0
        for d in sorted(candidates, key=lambda p: (-p.count("/"), p)):
            path = self.remote(d)
            if t.is_dir(path) and not t.listdir(path):
                t.rmdir(path)
                removed += 1
                console.detail(f"removed empty dir {d}/")
        if removed:
            console.info(f"  removed {removed} empty director{'y' if removed == 1 else 'ies'}")

    def record(self, local: Manifest) -> dict[str, Any]:
        console.step("record release")
        t = self.transport
        release = release_info(self.cfg, self.spec)
        manifest = {
            "tool_version": __version__,
            "deployed_at": release["deployed_at"],
            "git_commit": release["git_commit"],
            "core_version": release["core_version"],
            "files": local,
        }
        release.update({"backup": None, "active_plugins": self.cfg.active_plugins})
        t.mkdirs(self.state_dir, 0o700)
        write_json(t, self.manifest_file, manifest)
        write_json(t, self.release_file, release)
        console.detail(self.manifest_file)
        console.detail(self.release_file)
        return release

    def report_state(self) -> str:
        console.step(f"YOURLS state ({self.spec.url})")
        state, where = yourls_state(self.spec.url)
        if state == "install":
            console.warn(
                "YOURLS is not installed in this database yet: open"
                f" {self.spec.url}/admin/install.php to create the tables"
            )
        elif state == "upgrade":
            console.warn(
                "the database needs an upgrade: open"
                f" {self.spec.url}/admin/upgrade.php (logged in as admin) and run it"
            )
        elif state == "unknown":
            console.warn(f"could not check the install/upgrade state: {where}")
        else:
            console.info("  no install or upgrade needed")
        return state

    def print_manual_steps(self, state: str | None) -> None:
        console.info("Manual steps (no PHP on the server, so ym can't do these):")
        for s in manual_steps(self.cfg, self.spec, None if state == "unknown" else state):
            console.info(f"  - {s}")

    # ------------------------------------------------------------------ flows
    def _dist(self, opts: DeployOptions) -> Path:
        if opts.no_build:
            dist = self.cfg.out_dir
            if not dist.is_dir():
                raise UsageError(f"--no-build given but {dist} does not exist")
            return dist
        console.step("build")
        return build_site(self.cfg)

    def deploy(self, opts: DeployOptions) -> bool:
        dist = self._dist(opts)
        with self.transport:
            self.preflight(create=not opts.dry_run)
            console.info(
                f"note: {BACKUP_NOTE}"
                + (" (--skip-backup is implied)." if opts.skip_backup else ".")
            )
            plan, local = self.plan(dist, verify_remote=opts.verify_remote)
            self.print_plan(plan)
            if opts.dry_run:
                console.info("dry run: stopped before making changes")
                return True
            self.sync(plan, dist, local)
            self.record(local)
        state = self.report_state()
        self.print_manual_steps(state)
        if opts.skip_verify:
            console.step("verify (skipped: --skip-verify)")
            return True
        if state in ("install", "upgrade"):
            # Files are deployed; the site can't pass redirect tests until the admin step
            # above is done in a browser, which is expected, not a failed deploy.
            console.step(f"verify (skipped: {state} pending)")
            console.info(
                f"  after finishing the {state} in the browser: ym verify {self.spec.name}"
            )
            return True
        return verify(self.spec.url, self.cfg.root / REDIRECTS_FILE)

    def apply_plugins(self) -> dict[str, Any]:
        raise UsageError(
            f"target {self.spec.name} is an sftp target: ym can't change plugin activation"
            " without PHP on the server.\n"
            + "\n".join(f"  - {s}" for s in manual_steps(self.cfg, self.spec, "ok"))
        )


def write_json(t: SftpTransport, path: str, data: dict[str, Any]) -> None:
    t.write_bytes(path, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode(), 0o600)


def snapshot_manifest(root: Path, source: str, core_version: str | None) -> dict[str, Any]:
    """Manifest describing an adopted install (``ym adopt`` uploads it to state_dir)."""
    return {
        "tool_version": __version__,
        "deployed_at": iso_utc(),
        "git_commit": None,
        "core_version": core_version,
        "adopted_from": source,
        "files": local_manifest(root),
    }
