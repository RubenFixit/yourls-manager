"""`ym ci setup` / `ym ci status`: deploy from CI, opt-in per target (GitHub or GitLab).

The site template's CI file (``.github/workflows/deploy.yml``, or ``.gitlab-ci.yml`` on
sites made with ``ci_provider = gitlab``) deploys target ``prod`` in the ``production``
environment and every other target in ``staging``, only for targets with ``ci = true`` in
site.toml, and needs two secrets on that environment: ``DEPLOY_SSH_KEY`` (a private key the
server accepts) and ``DEPLOY_KNOWN_HOSTS`` (the server's host keys). On GitLab they are
File-type, protected CI/CD variables scoped to the environment, and the ``v*`` tags must be
protected so tag pipelines receive them.

`ym ci setup`, using the forge's CLI (``gh`` or ``glab``):

1. checks that the CLI is installed and logged in (offers its login on a terminal);
2. finds the site's repository / project (offers `ym repo create` when there is none);
3. creates the environment if it is missing;
4. generates a dedicated CI key (ed25519, no passphrase) per environment, installs its
   public half on the server over the working ym login (ssh, or SFTP for sftp targets),
   checks that it logs in, and stores the private half only in the CI secret;
5. takes the host keys from ``~/.ssh/known_hosts`` (falling back to ``ssh-keyscan`` with a
   fingerprint confirmation);
6. sets ``ci = true`` on the target in site.toml (commit it: the CI file reads it);
7. GitLab only: protects the ``v*`` tags, and (unless ``--skip-updates``) creates the
   weekly ``ym check-updates`` pipeline schedule and the ``YM_UPDATE_TOKEN`` variable the
   job pushes with (a project access token, or one you paste when GitLab refuses to make
   one). GitHub's update workflow needs neither (it runs on a cron and uses GITHUB_TOKEN).

An existing ``DEPLOY_SSH_KEY`` (and ``YM_UPDATE_TOKEN``) is kept unless ``--rotate``;
rotating offers to remove the environment's previous CI keys from the server.
"""

from __future__ import annotations

import datetime as _dt
import json
import posixpath
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import gitlab
from .config import SiteConfig, TargetSpec, ci_provider, load_site
from .github import Gh, current_repo, ensure_gh, gh_logged_in, repo_exists
from .util import UsageError, YMError, console, utc_now

KEY_SECRET = "DEPLOY_SSH_KEY"
HOSTS_SECRET = "DEPLOY_KNOWN_HOSTS"
TOKEN_VARIABLE = "YM_UPDATE_TOKEN"
WORKFLOW = ".github/workflows/deploy.yml"
GITLAB_CI = ".gitlab-ci.yml"

# Appends "$1" to ~/.ssh/authorized_keys unless the exact line is there (run via sh -c).
AUTHORIZED_KEYS_SCRIPT = (
    'umask 077; mkdir -p "$HOME/.ssh" && touch "$HOME/.ssh/authorized_keys" && '
    '{ grep -qxF "$1" "$HOME/.ssh/authorized_keys" || '
    'printf "%s\\n" "$1" >> "$HOME/.ssh/authorized_keys"; }'
)
# Removes the lines whose comment (last field) starts with "$1" but isn't "$2"; prints how
# many it removed. Rewrites the file in place (cat >), keeping its mode and owner.
REMOVE_KEYS_SCRIPT = (
    'umask 077; f="$HOME/.ssh/authorized_keys"; [ -f "$f" ] || { echo 0; exit 0; }; '
    'n=$(awk -v p="$1" -v k="$2" \'index($NF, p) == 1 && $NF != k\' "$f" | wc -l); '
    'awk -v p="$1" -v k="$2" \'!(index($NF, p) == 1 && $NF != k)\' "$f" > "$f.ym-tmp" '
    '&& cat "$f.ym-tmp" > "$f" && rm -f "$f.ym-tmp" && echo $n'
)

Confirm = Callable[[str], bool]
AskSecret = Callable[[str], str]


def environment_for(target: str) -> str:
    """The deploy job's mapping: prod -> production, anything else -> staging."""
    return "production" if target == "prod" else "staging"


# --------------------------------------------------------------------------- github


def env_exists(gh: Gh, repo: str, env: str) -> bool:
    return gh.run("api", f"repos/{repo}/environments/{env}", check=False).returncode == 0


def env_secrets(gh: Gh, repo: str, env: str) -> set[str]:
    proc = gh.run("secret", "list", "--repo", repo, "--env", env, "--json", "name", check=False)
    if proc.returncode != 0:
        return set()
    try:
        return {s["name"] for s in json.loads(proc.stdout.decode() or "[]")}
    except (ValueError, KeyError, TypeError):
        return set()


def set_secret(gh: Gh, repo: str, env: str, name: str, value: str) -> None:
    gh.run("secret", "set", name, "--repo", repo, "--env", env, input=value.encode())


# --------------------------------------------------------------------------- forges


class Forge:
    """What setup and status need from the site's CI host."""

    label = ""  # "GitHub Actions" / "GitLab CI"
    ci_file = ""
    where = ""  # repository / project, for messages

    def env_exists(self, env: str) -> bool:
        raise NotImplementedError

    def create_env(self, env: str) -> None:
        raise NotImplementedError

    def secret_names(self, env: str) -> set[str]:
        raise NotImplementedError

    def set_secret(self, env: str, name: str, value: str, *, exists: bool) -> None:
        raise NotImplementedError

    def target_problems(self) -> list[str]:
        """Project-wide problems that stop CI deploys (checked once per status run)."""
        return []


class GitHubForge(Forge):
    label = "GitHub Actions"
    ci_file = WORKFLOW

    def __init__(self, gh: Gh, repo: str):
        self.gh = gh
        self.repo = self.where = repo

    def env_exists(self, env: str) -> bool:
        return env_exists(self.gh, self.repo, env)

    def create_env(self, env: str) -> None:
        self.gh.run("api", "--method", "PUT", f"repos/{self.repo}/environments/{env}")

    def secret_names(self, env: str) -> set[str]:
        return env_secrets(self.gh, self.repo, env)

    def set_secret(self, env: str, name: str, value: str, *, exists: bool) -> None:
        set_secret(self.gh, self.repo, env, name, value)


class GitLabForge(Forge):
    label = "GitLab CI"
    ci_file = GITLAB_CI

    def __init__(self, glab: gitlab.Glab, proj: gitlab.Project):
        self.glab = glab
        self.proj = proj
        self.where = str(proj)
        self._vars: list[dict[str, Any]] | None = None

    def variables(self) -> list[dict[str, Any]]:
        if self._vars is None:
            self._vars = gitlab.variables(self.glab, self.proj)
        return self._vars

    def env_exists(self, env: str) -> bool:
        return gitlab.env_exists(self.glab, self.proj, env)

    def create_env(self, env: str) -> None:
        gitlab.create_env(self.glab, self.proj, env)

    def secret_names(self, env: str) -> set[str]:
        return {
            v["key"]
            for v in self.variables()
            if v.get("environment_scope") == env and v.get("variable_type") == "file"
        }

    def set_secret(self, env: str, name: str, value: str, *, exists: bool) -> None:
        # exists: a variable with this key and scope is there (maybe not File-type)
        exists = exists or gitlab.has_variable(self.variables(), name, env)
        gitlab.set_variable(self.glab, self.proj, name, value, scope=env, file=True,
                            protected=True, exists=exists)  # fmt: skip
        self._vars = None

    def target_problems(self) -> list[str]:
        if gitlab.tag_protected(self.glab, self.proj):
            return []
        return [f"tags {gitlab.TAG_PATTERN} are not protected (protected variables don't reach"
                " release-tag pipelines)"]  # fmt: skip


# --------------------------------------------------------------------------- host keys


def known_hosts_name(spec: TargetSpec) -> str:
    host = spec.host or ""
    return host if spec.port == 22 else f"[{host}]:{spec.port}"


def local_host_keys(spec: TargetSpec, known_hosts: Path | None = None) -> list[str]:
    """Lines for the host from ~/.ssh/known_hosts (hashed entries work as they are)."""
    path = known_hosts or Path.home() / ".ssh" / "known_hosts"
    if not path.is_file():
        return []
    proc = subprocess.run(["ssh-keygen", "-F", known_hosts_name(spec), "-f", str(path)],
                          capture_output=True, check=False)  # fmt: skip
    lines = proc.stdout.decode(errors="replace").splitlines()
    return [ln for ln in lines if ln.strip() and not ln.startswith("#")]


def fingerprints(lines: list[str]) -> list[str]:
    proc = subprocess.run(["ssh-keygen", "-lf", "-"], input="\n".join(lines).encode() + b"\n",
                          capture_output=True, check=False)  # fmt: skip
    return proc.stdout.decode(errors="replace").splitlines()


def scan_host_keys(spec: TargetSpec) -> list[str]:
    try:
        proc = subprocess.run(["ssh-keyscan", "-T", "15", "-p", str(spec.port), spec.host or ""],
                              capture_output=True, check=False)  # fmt: skip
    except FileNotFoundError:
        raise UsageError("ssh-keyscan not found (install OpenSSH)") from None
    lines = proc.stdout.decode(errors="replace").splitlines()
    return [ln for ln in lines if ln.strip() and not ln.startswith("#")]


def host_keys(spec: TargetSpec, confirm: Confirm | None) -> list[str]:
    lines = local_host_keys(spec)
    if lines:
        console.info(f"  host keys: {len(lines)} from ~/.ssh/known_hosts")
        return lines
    console.info(f"  {known_hosts_name(spec)} is not in ~/.ssh/known_hosts; scanning it")
    lines = scan_host_keys(spec)
    if not lines:
        raise YMError(f"ssh-keyscan got no host keys from {spec.host} (port {spec.port})")
    for fp in fingerprints(lines):
        console.info(f"    {fp}")
    if confirm is None:
        raise UsageError(
            f"can't confirm the host key fingerprints without a terminal; connect once with"
            f" `ym target login {spec.name}` (it records the host key), then run this again"
        )
    if not confirm("Do these fingerprints match the server's (ask your host if unsure)?"):
        raise UsageError("host key not confirmed; nothing stored")
    return lines


# --------------------------------------------------------------------------- CI key


@dataclass
class CiKey:
    private: Path
    public: str


def generate_key(directory: Path, comment: str) -> CiKey:
    key = directory / "id_ed25519"
    proc = subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(key)],
        capture_output=True, check=False,
    )  # fmt: skip
    if proc.returncode != 0:
        raise YMError(f"ssh-keygen failed: {proc.stderr.decode(errors='replace').strip()}")
    return CiKey(key, key.with_name("id_ed25519.pub").read_text().strip())


def install_public_key(spec: TargetSpec, public_key: str) -> None:
    """Append the key to authorized_keys over the target's normal ym login."""
    if spec.is_sftp:
        from .login import install_key_sftp
        from .transport.sftp import SftpTransport

        with SftpTransport.for_target(spec, interactive=False) as t:
            install_key_sftp(t, public_key)
        return
    from .remote import SshRunner

    runner = SshRunner(spec.host or "", spec.user, spec.port)
    runner.run(["sh", "-c", AUTHORIZED_KEYS_SCRIPT, "sh", public_key])
    console.info(f"  added the key to ~/.ssh/authorized_keys on {runner.destination}")


def check_key(spec: TargetSpec, key: Path) -> None:
    """Log in with only this key, as CI will (no agent, no ~/.ssh/config)."""
    if spec.is_sftp:
        from .transport.sftp import SftpError, SftpTransport

        t = SftpTransport.for_target(
            spec, interactive=False, allow_agent=False, look_for_keys=False,
            identity_files=(str(key),), ssh_config=Path("/dev/null"), env={},
            allow_password_prompt=False,
        )  # fmt: skip
        try:
            with t:
                t.home()
        except SftpError as exc:
            raise YMError(f"the new CI key was installed but doesn't log in: {exc}") from None
    else:
        from .login import classify
        from .remote import SshRunner

        runner = SshRunner(spec.host or "", spec.user, spec.port)
        argv = [*runner.ssh_base(), "-F", "/dev/null", "-o", "IdentitiesOnly=yes",
                "-o", "IdentityAgent=none", "-i", str(key), runner.destination,
                "--", "true"]  # fmt: skip
        proc = subprocess.run(argv, input=b"", capture_output=True, check=False)
        res = classify(proc.returncode, proc.stderr.decode(errors="replace"))
        if not res.ok:
            raise YMError(f"the new CI key was installed but doesn't log in: {res.reason}"
                          + (f"\n{res.detail}" if res.detail else ""))  # fmt: skip
    console.info("  the new key logs in (CI will use it the same way)")


def remove_old_keys(spec: TargetSpec, prefix: str, keep: str) -> int:
    """Remove authorized_keys lines whose comment starts with ``prefix`` (except ``keep``)."""
    if spec.is_sftp:
        from .transport.sftp import SftpTransport

        with SftpTransport.for_target(spec, interactive=False) as t:
            path = posixpath.join(t.abspath(".ssh"), "authorized_keys")
            if not t.is_file(path):
                return 0
            lines = t.read_bytes(path).decode("utf-8", "replace").splitlines(keepends=True)
            kept = [ln for ln in lines if not _old_key(ln, prefix, keep)]
            if len(kept) != len(lines):
                t.write_bytes(path, "".join(kept).encode("utf-8"), 0o600)
            return len(lines) - len(kept)
    from .remote import SshRunner

    runner = SshRunner(spec.host or "", spec.user, spec.port)
    out = runner.run(["sh", "-c", REMOVE_KEYS_SCRIPT, "sh", prefix, keep]).stdout
    try:
        return int(out.decode(errors="replace").split()[-1])
    except (IndexError, ValueError):
        return 0


def _old_key(line: str, prefix: str, keep: str) -> bool:
    parts = line.split()
    return bool(parts) and parts[-1].startswith(prefix) and parts[-1] != keep


# --------------------------------------------------------------------------- flow


@dataclass
class Plan:
    env: str
    spec: TargetSpec


def pick_targets(cfg: SiteConfig, names: list[str], *, allow_none: bool = False) -> list[Plan]:
    if names:
        specs = [cfg.target(n) for n in names]
    else:
        specs = [
            t
            for n, t in cfg.targets.items()
            if (n in ("prod", "staging") or t.ci) and not t.is_local
        ]
        others = sorted(set(cfg.targets) - {s.name for s in specs})
        if others:
            console.info(f"skipping target(s) {', '.join(others)}: name them to set them up"
                         " (they deploy in the staging environment)")  # fmt: skip
        if not specs and not allow_none:
            raise UsageError("no prod or staging target in site.toml; name the target(s)")
    plans = []
    for spec in specs:
        if spec.is_local:
            raise UsageError(f"target {spec.name} has no host; CI can't deploy it")
        plans.append(Plan(environment_for(spec.name), spec))
    by_env: dict[str, list[str]] = {}
    for p in plans:
        by_env.setdefault(p.env, []).append(p.spec.name)
    for env, names_ in by_env.items():
        if len(names_) > 1:
            raise UsageError(f"targets {', '.join(names_)} all deploy in the {env!r} environment,"
                             " which holds one set of secrets; set up one of them")  # fmt: skip
    return plans


def find_repo(cfg: SiteConfig, gh: Gh, repo: str | None, *, confirm: Confirm | None,
              dry_run: bool) -> str:  # fmt: skip
    if repo:
        if repo.count("/") != 1:
            raise UsageError(f"--repo must look like OWNER/NAME, got {repo!r}")
        if not repo_exists(gh, repo):
            raise UsageError(f"{repo} does not exist on GitHub (or gh can't see it)")
        return repo
    found = current_repo(gh)
    if found:
        return found
    from .github import create_repo, planned_repo

    planned = planned_repo(gh, cfg.root, None, None)
    if dry_run:
        console.info(f"no GitHub repository yet: would offer to create {planned}")
        return planned
    if confirm is None:
        raise UsageError("this site has no GitHub repository yet; create it with"
                         " `ym repo create`, then run this again")  # fmt: skip
    if not confirm(f"This site has no GitHub repository yet. Create {planned} (private)"
                   " and push the current branch?"):  # fmt: skip
        raise UsageError("no GitHub repository; nothing changed (see `ym repo create`)")
    create_repo(cfg, gh, planned)
    return planned


def find_project(cfg: SiteConfig, glab: gitlab.Glab, repo: str | None, *,
                 confirm: Confirm | None, dry_run: bool) -> gitlab.Project:  # fmt: skip
    """The site's GitLab project (--repo, origin, repo_url); offers to create it."""
    if repo:
        proj = gitlab.project_of(repo) or gitlab.project_of(f"https://{glab.host}/{repo}")
        if proj is None:
            raise UsageError(f"--repo must look like GROUP/NAME or a project URL, got {repo!r}")
        glab.host = proj.host
        if gitlab.get_project(glab, proj) is None:
            raise UsageError(f"{proj} does not exist on {proj.host} (or glab can't see it)")
        return proj
    proj = gitlab.site_project(cfg.root)
    if proj is not None and gitlab.get_project(glab, proj) is not None:
        return proj
    planned = gitlab.planned_project(glab, cfg.root, None, None, None)
    glab.host = planned.host
    if dry_run:
        console.info(f"no GitLab project yet: would offer to create {planned}")
        return planned
    if confirm is None:
        raise UsageError("this site has no GitLab project yet; create it with"
                         " `ym repo create`, then run this again")  # fmt: skip
    if not confirm(f"This site has no GitLab project yet. Create {planned} (private)"
                   " and push the current branch?"):  # fmt: skip
        raise UsageError("no GitLab project; nothing changed (see `ym repo create`)")
    gitlab.create_project(cfg, glab, planned)
    return planned


def require_ci_provider(cfg: SiteConfig, command: str) -> str:
    provider = ci_provider(cfg.root)
    if provider not in ("github", "gitlab"):
        raise UsageError(
            f"`ym {command}` sets up the template's CI jobs, but this site has none"
            f" (ci_provider = {provider}). Add them with"
            " `ym template update --data ci_provider=github` (or gitlab)"
        )
    return provider


def gitlab_cli(cfg: SiteConfig, glab: gitlab.Glab | None = None) -> gitlab.Glab:
    if glab is not None:
        return glab
    if not gitlab.glab_available(gitlab.Glab()):
        raise UsageError(f"the GitLab CLI (glab) is not installed: {gitlab.GLAB_INSTALL}")
    proj = gitlab.site_project(cfg.root)
    return gitlab.Glab(cwd=cfg.root, host=proj.host if proj else gitlab.DEFAULT_HOST)


# --------------------------------------------------------------------------- site.toml

_TABLE = re.compile(r"^\s*\[")
_CI = re.compile(r"^(\s*ci\s*=\s*)(true|false)(.*)$")
_CI_HINT = re.compile(r"^(\s*)#\s*ci(\s*=\s*)(?:true|false)(.*\n?)$", re.S)


def set_ci(cfg: SiteConfig, target: str, label: str = "GitHub Actions") -> bool:
    """Set ``ci = true`` in [deploy.targets.<target>] (text edit). False if already set."""
    if cfg.target(target).ci:
        return False
    path = cfg.site_file
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    head = re.compile(rf"^\s*\[\s*deploy\.targets\.{re.escape(target)}\s*\]")
    start = next((i for i, ln in enumerate(lines) if head.match(ln)), None)
    if start is None:
        raise UsageError(f"could not find [deploy.targets.{target}] in site.toml; add"
                         " `ci = true` there by hand")  # fmt: skip
    end = start + 1
    while end < len(lines) and not _TABLE.match(lines[end]):
        end += 1
    commented = None  # the template's `# ci = true ...` hint, within the table's own lines
    for k in range(start + 1, end):
        if lines[k].strip() in ("", "#"):
            break
        if _CI_HINT.match(lines[k]):
            commented = k
            break
    for k in range(start + 1, end):
        m = _CI.match(lines[k].rstrip("\n"))
        if m:
            lines[k] = f"{m[1]}true{m[3]}\n"
            break
    else:
        if commented is not None:
            m = _CI_HINT.match(lines[commented])
            lines[commented] = f"{m[1]}ci{m[2]}true{m[3]}"
        else:
            last = end
            while last > start + 1 and not lines[last - 1].strip():
                last -= 1  # keep blank lines (and comments of the next table) after the key
            while last > start + 1 and lines[last - 1].lstrip().startswith("#"):
                last -= 1
            if last > start + 1 and not lines[last - 1].endswith("\n"):
                lines[last - 1] += "\n"
            lines.insert(last, f"ci          = true             # deploy from {label}\n")
    path.write_text("".join(lines), encoding="utf-8")
    try:
        if not load_site(cfg.root).target(target).ci:
            raise UsageError("edit did not take effect")
    except Exception:
        path.write_text(original, encoding="utf-8")
        raise
    return True


# --------------------------------------------------------------------------- setup


def _forge(cfg: SiteConfig, provider: str, repo: str | None, *, confirm: Confirm | None,
           dry_run: bool, gh: Gh | None, glab: gitlab.Glab | None) -> Forge:  # fmt: skip
    if provider == "github":
        gh = gh or Gh(cwd=cfg.root)
        ensure_gh(gh, confirm=confirm)
        return GitHubForge(gh, find_repo(cfg, gh, repo, confirm=confirm, dry_run=dry_run))
    glab = gitlab_cli(cfg, glab)
    gitlab.ensure_glab(glab, confirm=confirm)
    proj = find_project(cfg, glab, repo, confirm=confirm, dry_run=dry_run)
    return GitLabForge(glab, proj)


def ci_setup(
    cfg: SiteConfig,
    targets: list[str],
    *,
    repo: str | None = None,
    rotate: bool = False,
    dry_run: bool = False,
    confirm: Confirm | None = None,
    ask_secret: AskSecret | None = None,
    skip_updates: bool = False,
    gh: Gh | None = None,
    glab: gitlab.Glab | None = None,
) -> None:
    from .login import check_login

    provider = require_ci_provider(cfg, "ci setup")
    updates = provider == "gitlab" and not skip_updates
    plans = pick_targets(cfg, targets, allow_none=updates)
    forge = _forge(cfg, provider, repo, confirm=confirm, dry_run=dry_run, gh=gh, glab=glab)
    console.info(f"{'repository' if provider == 'github' else 'project'}: {forge.where}")
    if not (cfg.root / forge.ci_file).is_file():
        console.warn(f"{forge.ci_file} is missing; these settings are for the site template's"
                     " CI jobs (`ym template update`)")  # fmt: skip
    if not plans:
        console.info("no deploy targets to set up (deploys stay manual; name a target to"
                     " deploy it from CI)")  # fmt: skip

    # Look everything up (and confirm host keys) before changing anything.
    known = forge_known(forge)
    prepared = []
    for plan in plans:
        spec, env = plan.spec, plan.env
        console.step(f"{spec.name} -> environment {env!r} ({spec.user + '@' if spec.user else ''}"
                     f"{spec.host}, {spec.type})")  # fmt: skip
        exists = forge.env_exists(env) if known else False
        have = forge.secret_names(env) if exists else set()
        new_key = rotate or KEY_SECRET not in have
        hosts = host_keys(spec, confirm)
        if new_key and not dry_run:
            login = check_login(spec, password=True)
            if not login.ok:
                raise YMError(f"can't log in to {spec.host} to install the CI key ({login.reason});"
                              f" run `ym target login {spec.name}` first")  # fmt: skip
        if dry_run:
            console.info(f"  would {'create' if not exists else 'use'} environment {env}")
            console.info(f"  would set {HOSTS_SECRET} ({len(hosts)} line(s))")
            what = (
                f"generate and install a new CI key, set {KEY_SECRET}"
                if new_key
                else f"keep the existing {KEY_SECRET}"
            )
            console.info(f"  would {what}")
            if not spec.ci:
                console.info("  would set ci = true in site.toml")
        prepared.append((spec, env, exists, KEY_SECRET in have, new_key, hosts))
    gl = forge if isinstance(forge, GitLabForge) else None
    protect = (bool(plans) and gl is not None
               and not (known and gitlab.tag_protected(gl.glab, gl.proj)))  # fmt: skip
    if dry_run:
        if protect:
            console.info(f"would protect the tags {gitlab.TAG_PATTERN}")
        if updates and gl is not None:
            _setup_updates(cfg, gl, rotate=rotate, dry_run=True, ask_secret=ask_secret)
        console.info("dry run: nothing changed")
        return

    if protect and gl is not None:
        gitlab.protect_tags(gl.glab, gl.proj)
        console.info(
            f"protected the tags {gitlab.TAG_PATTERN} (release tags; only maintainers"
            " can create them, and their pipelines get the protected variables)"
        )
    changed_toml = False
    for spec, env, exists, had_key, new_key, hosts in prepared:
        console.step(f"{spec.name}: configure environment {env!r}")
        if not exists:
            forge.create_env(env)
            console.info(f"  created environment {env}")
        if new_key:
            comment = f"ym-ci-{cfg.site.slug}-{env}-{utc_now().strftime('%Y%m%d')}"
            with tempfile.TemporaryDirectory(prefix="ym-ci-key-") as tmp:
                key = generate_key(Path(tmp), comment)
                install_public_key(spec, key.public)
                check_key(spec, key.private)
                forge.set_secret(env, KEY_SECRET, key.private.read_text(), exists=had_key)
            console.info(f"  set {KEY_SECRET} (key {comment}; the private key exists only there)")
            if had_key:
                _old_keys(spec, cfg.site.slug, env, comment, confirm)
        else:
            console.info(f"  {KEY_SECRET} already set; kept (--rotate replaces it)")
        forge.set_secret(env, HOSTS_SECRET, "\n".join(hosts) + "\n",
                         exists=HOSTS_SECRET in forge.secret_names(env))  # fmt: skip
        console.info(f"  set {HOSTS_SECRET} ({len(hosts)} line(s))")
        if set_ci(cfg, spec.name, forge.label):
            console.info("  site.toml: ci = true")
            changed_toml = True
    if updates and gl is not None:
        _setup_updates(cfg, gl, rotate=rotate, dry_run=False, ask_secret=ask_secret)
    if changed_toml:
        console.info(
            "commit site.toml (the CI file reads `ci` from the commit it deploys):"
            f' git commit -m "Deploy from {forge.label}" site.toml && git push'
        )
    if not plans:
        return
    if provider == "github":
        console.info(
            "done: release tags (`ym release --push`) now deploy from GitHub Actions; or"
            " run the workflow by hand (`gh workflow run deploy.yml -f target=prod`)"
        )
    else:
        console.info(
            "done: release tags (`ym release --push`) now deploy from GitLab CI; or run a"
            " pipeline by hand with the variable TARGET=<name> (Build -> Pipelines -> Run"
            " pipeline)"
        )


def forge_known(forge: Forge) -> bool:
    """False for a project that a dry run only planned to create."""
    if isinstance(forge, GitLabForge):
        return gitlab.get_project(forge.glab, forge.proj) is not None
    return True


def _old_keys(spec: TargetSpec, slug: str, env: str, keep: str, confirm: Confirm | None) -> None:
    prefix = f"ym-ci-{slug}-{env}-"
    question = (f"Remove the previous CI key(s) ({prefix}*) from ~/.ssh/authorized_keys on"
                f" {spec.host}?")  # fmt: skip
    if confirm is None or not confirm(question):
        console.info(f"  the previous CI key is still authorized on the server: remove its"
                     f" `{prefix}` line(s) from ~/.ssh/authorized_keys")  # fmt: skip
        return
    n = remove_old_keys(spec, prefix, keep)
    console.info(f"  removed {n} previous CI key(s) from ~/.ssh/authorized_keys")


def _setup_updates(cfg: SiteConfig, forge: GitLabForge, *, rotate: bool, dry_run: bool,
                   ask_secret: AskSecret | None) -> None:  # fmt: skip
    """GitLab: the weekly check-updates schedule and the token its job pushes with."""
    glab, proj = forge.glab, forge.proj
    console.step("check-updates (scheduled merge requests for core and plugin updates)")
    known = forge_known(forge)
    schedule = gitlab.find_schedule(glab, proj) if known else None
    has_token = known and any(v.get("key") == TOKEN_VARIABLE for v in forge.variables())
    if dry_run:
        if schedule is None:
            console.info(f"  would create the pipeline schedule {gitlab.SCHEDULE!r}"
                         f" ({gitlab.SCHEDULE_CRON} UTC)")  # fmt: skip
        if rotate or not has_token:
            console.info(f"  would create a project access token and set {TOKEN_VARIABLE}")
        else:
            console.info(f"  would keep {TOKEN_VARIABLE}")
        return
    if schedule is None:
        ref = (gitlab.get_project(glab, proj) or {}).get("default_branch") or "main"
        gitlab.create_schedule(glab, proj, ref)
        schedule = gitlab.find_schedule(glab, proj)
        console.info(f"  created the pipeline schedule {gitlab.SCHEDULE!r} on {ref}"
                     f" ({gitlab.SCHEDULE_CRON} UTC; change it under Build -> Pipeline"
                     " schedules)")  # fmt: skip
    else:
        console.info(f"  pipeline schedule {gitlab.SCHEDULE!r} exists")
    if has_token and not rotate:
        console.info(f"  {TOKEN_VARIABLE} already set; kept (--rotate replaces it)")
    else:
        token = _update_token(glab, proj, ask_secret)
        if token:
            gitlab.set_variable(glab, proj, TOKEN_VARIABLE, token, masked=True,
                                exists=has_token)  # fmt: skip
            console.info(f"  set {TOKEN_VARIABLE} (masked)")
    if schedule is not None:
        console.info(f"  run it now: glab schedule run {schedule.get('id')}")


def _update_token(glab: gitlab.Glab, proj: gitlab.Project, ask_secret: AskSecret | None
                  ) -> str | None:  # fmt: skip
    expires = (utc_now() + _dt.timedelta(days=364)).date().isoformat()
    try:
        token = gitlab.create_access_token(glab, proj, expires)
        console.info(f"  created a project access token {gitlab.SCHEDULE!r} (Developer,"
                     f" write_repository, expires {expires}; run this again with --rotate"
                     " before then)")  # fmt: skip
        return token
    except gitlab.ApiError as exc:
        why = f"HTTP {exc.status}" if exc.status else str(exc)
        console.info(f"  GitLab didn't create a project access token ({why}; on gitlab.com"
                     " they may need a paid tier)")  # fmt: skip
    hint = (
        "create a personal (or group) access token with the write_repository scope and"
        f" store it as the masked CI/CD variable {TOKEN_VARIABLE}, or paste it here"
    )
    if ask_secret is None:
        console.warn(f"{TOKEN_VARIABLE} not set: {hint} (run this again on a terminal)")
        return None
    token = ask_secret(f"  Paste a token with write_repository for {proj} (empty: skip): ").strip()
    if not token:
        console.warn(f"{TOKEN_VARIABLE} not set; check-updates can't open merge requests until"
                     " it is")  # fmt: skip
        return None
    return token


# --------------------------------------------------------------------------- status


@dataclass
class CiCheck:
    target: str
    ok: bool
    problems: list[str] = field(default_factory=list)


def ci_check(cfg: SiteConfig, spec: TargetSpec, forge: Forge) -> CiCheck:
    env = environment_for(spec.name)
    problems = []
    if not forge.env_exists(env):
        problems.append(f"environment {env!r} does not exist")
    else:
        missing = {KEY_SECRET, HOSTS_SECRET} - forge.secret_names(env)
        if missing:
            problems.append(f"environment {env!r} lacks {', '.join(sorted(missing))}")
    if not (cfg.root / forge.ci_file).is_file():
        problems.append(f"{forge.ci_file} is missing")
    return CiCheck(spec.name, not problems, problems)


def status_forge(cfg: SiteConfig, *, gh: Gh | None = None, glab: gitlab.Glab | None = None
                 ) -> Forge | str:  # fmt: skip
    """The site's forge for read-only checks, or a reason it can't be checked (str)."""
    if ci_provider(cfg.root) == "github":
        gh = gh or Gh(cwd=cfg.root)
        if not gh_logged_in(gh):
            return "can't check GitHub (gh not installed or not logged in): gh auth login"
        repo = current_repo(gh)
        if repo is None:
            return "no GitHub repository for this site: ym repo create"
        return GitHubForge(gh, repo)
    glab = gitlab_cli(cfg, glab)
    if not gitlab.glab_logged_in(glab):
        return (f"can't check GitLab (glab not installed or not logged in):"
                f" glab auth login --hostname {glab.host}")  # fmt: skip
    proj = gitlab.site_project(cfg.root)
    if proj is None or gitlab.get_project(glab, proj) is None:
        return "no GitLab project for this site: ym repo create"
    return GitLabForge(glab, proj)


def ci_status(cfg: SiteConfig, targets: list[str], *, gh: Gh | None = None,
              glab: gitlab.Glab | None = None) -> bool:  # fmt: skip
    """Print the CI state of the targets (default: all with ci = true). True when ready."""
    provider = require_ci_provider(cfg, "ci status")
    specs = [cfg.target(n) for n in targets] if targets else [
        t for t in cfg.targets.values() if t.ci
    ]  # fmt: skip
    if not specs and provider == "github":
        console.info("no target has ci = true: every deploy runs from this machine"
                     " (`ym ci setup` enables GitHub Actions deploys)")  # fmt: skip
        return True
    forge = status_forge(cfg, gh=gh, glab=glab)
    if isinstance(forge, str):
        reason, _, hint = forge.partition(": ")
        if reason.startswith("can't check"):
            raise UsageError(f"checking needs the {provider} CLI, logged in ({hint})")
        console.info(f"FAIL  {reason} (`{hint}`)")
        return False
    console.info(f"{'repository' if provider == 'github' else 'project'}: {forge.where}")
    ok = True
    if not specs:
        console.info("no target has ci = true: every deploy runs from this machine"
                     " (`ym ci setup TARGET` enables CI deploys)")  # fmt: skip
    shared = forge.target_problems() if specs else []
    for spec in specs:
        res = ci_check(cfg, spec, forge)
        res.problems += shared
        res.ok = not res.problems
        flag = "" if spec.ci else " (ci = false: the CI job skips it)"
        env = environment_for(spec.name)
        if res.ok:
            console.info(f"ok    {spec.name}: environment {env!r} has both secrets{flag}")
        else:
            console.info(f"FAIL  {spec.name}: {'; '.join(res.problems)}{flag}"
                         f"  -> ym ci setup {spec.name}")  # fmt: skip
        ok &= res.ok
    if isinstance(forge, GitLabForge):
        schedule = gitlab.find_schedule(forge.glab, forge.proj)
        token = any(v.get("key") == TOKEN_VARIABLE for v in forge.variables())
        if schedule is not None and token:
            console.info(f"ok    check-updates: schedule {gitlab.SCHEDULE!r} and {TOKEN_VARIABLE}")
        else:
            missing = ([] if schedule else [f"no schedule {gitlab.SCHEDULE!r}"]) + (
                [] if token else [f"no {TOKEN_VARIABLE}"])  # fmt: skip
            console.info(f"warn  check-updates: {'; '.join(missing)}  -> ym ci setup")
    return ok


def is_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()
