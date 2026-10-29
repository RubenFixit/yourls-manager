"""`ym doctor [TARGET]`: read-only health report of the tools, site repo and targets (§11.3)."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import SiteConfig, TargetSpec, find_site_dir, load_site
from .util import YMError, console, run

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    status: str
    label: str
    detail: str = ""
    hint: str = ""


class Report:
    def __init__(self) -> None:
        self.checks: list[Check] = []

    def section(self, title: str) -> None:
        console.info(title)

    def add(self, status: str, label: str, detail: str = "", hint: str = "") -> Check:
        c = Check(status, label, detail, hint)
        self.checks.append(c)
        mark = {OK: "ok  ", WARN: "warn", FAIL: "FAIL"}[status]
        line = f"  {mark}  {label}" + (f": {detail}" if detail else "")
        console.info(line)
        if hint and status != OK:
            console.info(f"        -> {hint}")
        return c

    def count(self, status: str) -> int:
        return sum(1 for c in self.checks if c.status == status)

    @property
    def failed(self) -> bool:
        return self.count(FAIL) > 0


def _first_line(argv: list[str]) -> str | None:
    if shutil.which(argv[0]) is None:
        return None
    proc = run(argv, check=False)
    out = (proc.stdout or b"").decode(errors="replace") or (proc.stderr or b"").decode(
        errors="replace"
    )
    return out.strip().splitlines()[0] if out.strip() else argv[0]


# --------------------------------------------------------------------------- local


def check_tools(rep: Report, cfg: SiteConfig | None) -> None:
    rep.section("tools")
    ver = _first_line(["git", "--version"])
    if ver:
        rep.add(OK, "git", ver)
    else:
        rep.add(FAIL, "git", "not found", "install Git")
    ver = _first_line(["uv", "--version"])
    if ver:
        rep.add(OK, "uv", ver)
    else:
        rep.add(WARN, "uv", "not found", "install uv (https://docs.astral.sh/uv/)")
    targets = list(cfg.targets.values()) if cfg else []
    rsync_targets = [t for t in targets if not t.is_sftp]
    if rsync_targets:
        ver = _first_line(["rsync", "--version"])
        if ver:
            rep.add(OK, "rsync", ver)
        else:
            rep.add(FAIL, "rsync", "not found (needed by rsync targets)",
                    "install rsync; on Windows run ym from WSL")  # fmt: skip
    if any(not t.is_local for t in rsync_targets):
        if shutil.which("ssh"):
            rep.add(OK, "ssh", shutil.which("ssh") or "")
        else:
            rep.add(FAIL, "ssh", "not found (needed by rsync targets)", "install OpenSSH")


def _git(root: Path, *args: str) -> tuple[int, str]:
    proc = run(["git", *args], cwd=root, check=False)
    return proc.returncode, proc.stdout.decode(errors="replace").strip()


def check_site(rep: Report, start: Path | None) -> SiteConfig | None:
    rep.section("site repo")
    try:
        root = find_site_dir(start)
    except YMError as exc:
        rep.add(FAIL, "site.toml", str(exc), "run ym inside a site repo or pass --site-dir")
        return None
    try:
        cfg = load_site(root)
    except YMError as exc:
        rep.add(FAIL, "site.toml", str(exc), "fix site.toml")
        return None
    if cfg.warnings:
        rep.add(WARN, "site.toml", "; ".join(cfg.warnings))
    else:
        rep.add(OK, "site.toml", f"{cfg.site_file} is valid")

    from .drift import compute_drift
    from .lockfile import load_lock

    if not cfg.lock_file.is_file():
        rep.add(FAIL, "lock", "yourls.lock.json is missing", "run `ym core` and `ym sync`")
    else:
        try:
            lock = load_lock(cfg.lock_file)
        except YMError as exc:
            rep.add(FAIL, "lock", str(exc))
        else:
            drift = compute_drift(cfg, lock)
            if drift.clean:
                core = lock.core.version if lock.core else "?"
                rep.add(OK, "lock", f"YOURLS {core}, {len(lock.plugins)} vendored plugin(s),"
                        " no drift")  # fmt: skip
            else:
                rep.add(FAIL, "lock", "drift: " + "; ".join(drift.problems),
                        "run `ym status`, then `ym core` / `ym sync`")  # fmt: skip

    code, _ = _git(cfg.root, "rev-parse", "--is-inside-work-tree")
    if code != 0:
        rep.add(WARN, "git", "not a git repository", "git init and commit the site")
        return cfg
    _, status = _git(cfg.root, "status", "--porcelain")
    changes = [ln for ln in status.splitlines() if ln.strip()]
    if changes:
        rep.add(WARN, "working tree", f"{len(changes)} uncommitted change(s)",
                "commit before deploying (deploy refuses a dirty tree)")  # fmt: skip
    else:
        rep.add(OK, "working tree", "clean")
    code, head = _git(cfg.root, "rev-parse", "--short", "HEAD")
    if code != 0:
        rep.add(WARN, "HEAD", "no commits yet")
        return cfg
    _, remotes = _git(cfg.root, "remote")
    if not remotes:
        rep.add(OK, "remote", "none (local only)")
    else:
        _, branches = _git(cfg.root, "branch", "-r", "--contains", "HEAD")
        if branches:
            rep.add(OK, "HEAD pushed", f"{head} is on {branches.splitlines()[0].strip()}")
        else:
            rep.add(WARN, "HEAD pushed", f"{head} is not on any remote branch", "git push")
    return cfg


# --------------------------------------------------------------------------- targets


def _login(rep: Report, spec: TargetSpec) -> bool:
    from .sshsetup import check_ssh

    if spec.is_local and not spec.is_sftp:
        rep.add(OK, "login", "local target (no host)")
        return True
    try:
        res = check_ssh(spec, password=True)
    except YMError as exc:
        rep.add(FAIL, "login", str(exc))
        return False
    if res.ok:
        rep.add(OK, "login", "key authentication works" if not spec.is_sftp else "SFTP login works")
        return True
    hint = f"ym ssh-setup {spec.name}"
    if spec.is_sftp and res.reason == "auth":
        from .transport.sftp import password_env

        hint += f" (or set {password_env(spec.name)})"
    detail = res.reason + (f": {res.detail.splitlines()[-1]}" if res.detail else "")
    rep.add(FAIL, "login", detail, hint)
    return False


def _rsync_checks(rep: Report, cfg: SiteConfig, spec: TargetSpec) -> dict | None:
    from .deploy.rsync import MIN_PHP, RsyncTarget

    t = RsyncTarget(cfg, spec)
    try:
        ver = t.php_version()
    except YMError as exc:
        rep.add(FAIL, "php", str(exc).splitlines()[0], "set [deploy.targets.<name>].php")
    else:
        v = ".".join(map(str, ver))
        if ver >= MIN_PHP:
            rep.add(OK, "php", f"PHP {v} ({spec.php})")
        else:
            rep.add(FAIL, "php", f"PHP {v} is too old; need >= 8.1",
                    "set [deploy.targets.<name>].php to a newer PHP CLI")  # fmt: skip
    if t.runner.exists(t.config_file, "-f"):
        rep.add(OK, "server config", t.config_file)
    else:
        rep.add(FAIL, "server config", f"{t.config_file} not found",
                f"ym server-config {spec.name}")  # fmt: skip
    proc = t.runner.run(["cat", "--", t.release_file], check=False)
    return _parse_release(proc.stdout) if proc.returncode == 0 else None


def _sftp_checks(rep: Report, cfg: SiteConfig, spec: TargetSpec) -> dict | None:
    from .deploy.sftp import SftpTarget
    from .transport.sftp import SftpTransport

    t = SftpTarget(cfg, spec, SftpTransport.for_target(spec, interactive=False))
    try:
        with t.transport:
            if t.transport.is_file(t.config_file):
                rep.add(OK, "server config", t.config_file)
            else:
                rep.add(FAIL, "server config", f"{t.config_file} not found",
                        f"ym server-config {spec.name}")  # fmt: skip
            if not t.transport.is_file(t.release_file):
                return None
            return _parse_release(t.transport.read_bytes(t.release_file))
    except YMError as exc:
        rep.add(FAIL, "server", str(exc).splitlines()[0])
        return None


def _parse_release(data: bytes) -> dict:
    try:
        rel = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"_invalid": True}
    return rel if isinstance(rel, dict) else {"_invalid": True}


def _url_checks(rep: Report, spec: TargetSpec, deployed: bool) -> None:
    from .deploy.sftp import yourls_state

    url = spec.url
    if url.startswith("https://"):
        rep.add(OK, "url", url)
    else:
        rep.add(WARN, "url", f"{url} is not https", "serve the site over https")
    state, where = yourls_state(url.rstrip("/"))
    if state == "ok":
        rep.add(OK, "http", "YOURLS answers; no install or upgrade pending")
    elif state == "install":
        rep.add(WARN, "http", "install pending", f"open {url.rstrip('/')}/admin/install.php")
    elif state == "upgrade":
        hint = f"open {url.rstrip('/')}/admin/upgrade.php"
        if not spec.is_sftp:
            hint = f"ym deploy {spec.name} (runs the upgrade), or " + hint
        rep.add(WARN, "http", "database upgrade pending", hint)
    else:
        rep.add(FAIL if deployed else WARN, "http", f"unreachable: {where}")


def _deployed_check(rep: Report, cfg: SiteConfig, spec: TargetSpec, rel: dict | None) -> None:
    if rel is None:
        rep.add(WARN, "deployed", "nothing deployed yet (no release.json)",
                f"ym deploy {spec.name}")  # fmt: skip
        return
    if rel.get("_invalid"):
        rep.add(WARN, "deployed", "release.json is unreadable")
        return
    commit = rel.get("git_commit") or ""
    _, head = _git(cfg.root, "rev-parse", "HEAD")
    what = commit[:7] or "unknown commit"
    if rel.get("tag"):
        what += f" ({rel['tag']})"
    what += f" at {rel.get('deployed_at', '?')}"
    if rel.get("git_dirty"):
        rep.add(WARN, "deployed", what + ", with uncommitted changes",
                f"commit and ym deploy {spec.name}")  # fmt: skip
    elif commit and commit == head:
        rep.add(OK, "deployed", what + " = HEAD")
    else:
        code, behind = _git(cfg.root, "rev-list", "--count", f"{commit}..HEAD")
        extra = f", {behind} commit(s) behind HEAD" if code == 0 and commit else ", HEAD differs"
        rep.add(WARN, "deployed", what + extra, f"ym deploy {spec.name}")


def _ci_checks(rep: Report, cfg: SiteConfig, spec: TargetSpec, github: dict) -> None:
    """Only for targets with ci = true: the GitHub side of the deploy workflow."""
    from .ci import ci_check, environment_for
    from .github import Gh, current_repo, gh_logged_in

    if "repo" not in github:
        gh = Gh(cwd=cfg.root)
        github["gh"] = gh
        github["repo"] = current_repo(gh) if gh_logged_in(gh) else False
    repo = github["repo"]
    if repo is False:
        rep.add(WARN, "ci", "can't check GitHub (gh not installed or not logged in)",
                "gh auth login")  # fmt: skip
        return
    if repo is None:
        rep.add(FAIL, "ci", "ci = true but this site has no GitHub repository", "ym repo create")
        return
    res = ci_check(cfg, spec, github["gh"], repo)
    env = environment_for(spec.name)
    if res.ok:
        rep.add(OK, "ci", f"{repo}: environment {env!r} has both secrets")
    else:
        rep.add(FAIL, "ci", f"{repo}: " + "; ".join(res.problems), f"ym ci setup {spec.name}")


def check_target(rep: Report, cfg: SiteConfig, spec: TargetSpec, github: dict | None = None
                 ) -> None:  # fmt: skip
    where = spec.host or "local"
    rep.section(f"target {spec.name} ({spec.type}, {where}{', ci' if spec.ci else ''})")
    rel = None
    if _login(rep, spec):
        try:
            rel = _sftp_checks(rep, cfg, spec) if spec.is_sftp else _rsync_checks(rep, cfg, spec)
        except YMError as exc:
            rep.add(FAIL, "server", str(exc).splitlines()[0])
    _url_checks(rep, spec, deployed=rel is not None)
    _deployed_check(rep, cfg, spec, rel)
    if spec.ci:
        _ci_checks(rep, cfg, spec, github if github is not None else {})


def doctor(start: Path | None, target: str | None = None) -> bool:
    """Print the report; returns True when nothing failed."""
    rep = Report()
    cfg = check_site(rep, start)
    check_tools(rep, cfg)
    if cfg is not None:
        if target is not None:
            specs = [cfg.target(target)]
        else:
            specs = list(cfg.targets.values())
            if not specs:
                rep.section("targets")
                rep.add(WARN, "targets", "none defined in site.toml")
        github: dict = {}
        for spec in specs:
            check_target(rep, cfg, spec, github)
    console.info(
        f"doctor: {rep.count(OK)} ok, {rep.count(WARN)} warning(s), {rep.count(FAIL)} failed"
    )
    return not rep.failed
