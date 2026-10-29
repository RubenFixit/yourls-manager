"""`ym gh-setup`: configure GitHub environments and secrets for the deploy workflow (§13).

The site template's ``.github/workflows/deploy.yml`` deploys target ``prod`` in the
``production`` environment and every other target in ``staging``, and needs two secrets on
that environment: ``DEPLOY_SSH_KEY`` (a private key the server accepts) and
``DEPLOY_KNOWN_HOSTS`` (the server's host keys). This command, using the GitHub CLI:

1. checks that ``gh`` is installed and logged in (offers ``gh auth login`` on a terminal);
2. finds the site's GitHub repository, or creates it (private unless ``--public``) and pushes
   the current branch, after asking (``--create-repo`` on a non-terminal);
3. creates the environment if it is missing;
4. generates a dedicated CI key (ed25519, no passphrase) per environment, installs its
   public half on the server over the working ym login (ssh, or SFTP for sftp targets),
   checks that it logs in, and stores the private half only in the GitHub secret;
5. takes the host keys from ``~/.ssh/known_hosts`` (falling back to ``ssh-keyscan`` with a
   fingerprint confirmation).

An existing ``DEPLOY_SSH_KEY`` is kept unless ``--rotate``.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import SiteConfig, TargetSpec
from .util import UsageError, YMError, console, run, utc_now

KEY_SECRET = "DEPLOY_SSH_KEY"
HOSTS_SECRET = "DEPLOY_KNOWN_HOSTS"
WORKFLOW = ".github/workflows/deploy.yml"
GH_INSTALL = "https://cli.github.com (Debian/Ubuntu/WSL: `sudo apt install gh`)"

# Appends "$1" to ~/.ssh/authorized_keys unless the exact line is there (run via sh -c).
AUTHORIZED_KEYS_SCRIPT = (
    'umask 077; mkdir -p "$HOME/.ssh" && touch "$HOME/.ssh/authorized_keys" && '
    '{ grep -qxF "$1" "$HOME/.ssh/authorized_keys" || '
    'printf "%s\\n" "$1" >> "$HOME/.ssh/authorized_keys"; }'
)

Confirm = Callable[[str], bool]
Interactive = Callable[[Sequence[str]], int]


def environment_for(target: str) -> str:
    """The deploy workflow's mapping: prod -> production, anything else -> staging."""
    return "production" if target == "prod" else "staging"


# --------------------------------------------------------------------------- gh


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


def ensure_gh(gh: Gh, *, confirm: Confirm | None, interactive: Interactive) -> None:
    """gh installed and authenticated; offers `gh auth login` when a terminal is there."""
    if shutil.which(gh.exe) is None:
        raise UsageError(f"the GitHub CLI (gh) is not installed: {GH_INSTALL}\n"
                         "Then run `gh auth login` and `ym gh-setup` again.")  # fmt: skip
    if gh.run("auth", "status", check=False).returncode == 0:
        return
    if confirm is None:
        raise UsageError("gh is not logged in to GitHub; run `gh auth login` first")
    console.info("gh is installed but not logged in to GitHub.")
    if not confirm("Run `gh auth login` now?"):
        raise UsageError("gh is not logged in; run `gh auth login`, then `ym gh-setup` again")
    if interactive([gh.exe, "auth", "login"]) != 0:
        raise YMError("`gh auth login` failed")
    if gh.run("auth", "status", check=False).returncode != 0:
        raise YMError("gh is still not logged in (see `gh auth status`)")


_GITHUB_REMOTE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"(?P<repo>[^/\s]+/[^/\s]+?)(?:\.git)?/?$"
)


def github_repo_of(url: str) -> str | None:
    m = _GITHUB_REMOTE.match(url.strip())
    return m["repo"] if m else None


def _git_out(root: Path, *args: str) -> str | None:
    proc = run(["git", *args], cwd=root, check=False)
    return proc.stdout.decode(errors="replace").strip() if proc.returncode == 0 else None


def repo_exists(gh: Gh, repo: str) -> bool:
    return gh.run("repo", "view", repo, "--json", "name", check=False).returncode == 0


def resolve_repo(
    gh: Gh,
    root: Path,
    repo: str | None,
    *,
    create: bool = False,
    public: bool = False,
    confirm: Confirm | None = None,
    dry_run: bool = False,
) -> str:
    """The site's GitHub repo; offers to create it (and push) when there is none."""
    if repo and repo.count("/") != 1:
        raise UsageError(f"--repo must look like OWNER/NAME, got {repo!r}")
    origin = _git_out(root, "config", "--get", "remote.origin.url")  # as written
    if repo is None:
        proc = gh.run("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner",
                      check=False)  # fmt: skip
        name = proc.stdout.decode(errors="replace").strip()
        if proc.returncode == 0 and "/" in name:
            return name
        repo = github_repo_of(origin) if origin else None
        if repo is None:
            if origin:
                raise UsageError(
                    f"origin is {origin}, not a GitHub repository; pass --repo OWNER/NAME"
                    " (it is added as remote `github`)"
                )
            login = gh.out("api", "user", "-q", ".login")
            repo = f"{login}/{root.name}"
    elif repo_exists(gh, repo):
        return repo

    # The repository doesn't exist yet.
    if _git_out(root, "rev-parse", "--verify", "-q", "HEAD") is None:
        raise UsageError("this site repo has no commits yet; commit it first")
    visibility = "public" if public else "private"
    console.info(f"GitHub repository {repo} does not exist yet")
    if dry_run:
        console.info(f"  would create it ({visibility}) and push the current branch")
        return repo
    if not create:
        if confirm is None:
            raise UsageError(
                f"no GitHub repository for this site; pass --create-repo to create {repo}"
                f" ({visibility}) and push, or --repo OWNER/NAME"
            )
        if not confirm(f"Create {repo} ({visibility}) on GitHub and push the current branch?"):
            raise UsageError("no GitHub repository; nothing changed")
    origin_repo = github_repo_of(origin) if origin else None
    if origin_repo is not None and origin_repo.lower() == repo.lower():
        gh.run("repo", "create", repo, f"--{visibility}")
        remote = "origin"
    else:
        remote = "origin" if origin is None else "github"
        if _git_out(root, "config", "--get", f"remote.{remote}.url") is not None:
            raise UsageError(f"remote {remote!r} already exists; pass --repo matching it")
        gh.run("repo", "create", repo, f"--{visibility}", "--source", str(root),
               "--remote", remote)  # fmt: skip
    console.info(f"  created {repo} ({visibility}); remote {remote}")
    branch = _git_out(root, "rev-parse", "--abbrev-ref", "HEAD") or "HEAD"
    push = run(["git", "push", "-u", remote, branch], cwd=root, check=False, capture=False)
    if push.returncode != 0:
        raise YMError(f"`git push -u {remote} {branch}` failed; push by hand, then run"
                      " `ym gh-setup` again")  # fmt: skip
    console.info(f"  pushed {branch} (tags not pushed: a release tag starts a deploy, so push"
                 " them once the secrets are set)")  # fmt: skip
    return repo


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
            f" `ym ssh-setup {spec.name}` (it records the host key), then run this again"
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
        from .sshsetup import install_key_sftp
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
        from .remote import SshRunner
        from .sshsetup import classify

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


# --------------------------------------------------------------------------- flow


@dataclass
class Plan:
    env: str
    spec: TargetSpec


def pick_targets(cfg: SiteConfig, names: list[str]) -> list[Plan]:
    if names:
        specs = [cfg.target(n) for n in names]
    else:
        specs = [cfg.targets[n] for n in ("prod", "staging") if n in cfg.targets]
        others = sorted(set(cfg.targets) - {"prod", "staging"})
        if others:
            console.info(f"skipping target(s) {', '.join(others)}: name them to set them up"
                         " (they deploy in the staging environment)")  # fmt: skip
        if not specs:
            raise UsageError("no prod or staging target in site.toml; name the target(s)")
    plans = []
    for spec in specs:
        if spec.is_local:
            raise UsageError(f"target {spec.name} has no host; GitHub Actions can't deploy it")
        plans.append(Plan(environment_for(spec.name), spec))
    by_env: dict[str, list[str]] = {}
    for p in plans:
        by_env.setdefault(p.env, []).append(p.spec.name)
    for env, names_ in by_env.items():
        if len(names_) > 1:
            raise UsageError(f"targets {', '.join(names_)} all deploy in the {env!r} environment,"
                             " which holds one set of secrets; set up one of them")  # fmt: skip
    return plans


def gh_setup(
    cfg: SiteConfig,
    targets: list[str],
    *,
    repo: str | None = None,
    create_repo: bool = False,
    public: bool = False,
    rotate: bool = False,
    dry_run: bool = False,
    confirm: Confirm | None = None,
    interactive: Interactive | None = None,
    gh: Gh | None = None,
) -> None:
    from .sshsetup import _terminal, check_ssh

    gh = gh or Gh(cwd=cfg.root)
    plans = pick_targets(cfg, targets)
    ensure_gh(gh, confirm=confirm, interactive=interactive or _terminal)
    repo = resolve_repo(gh, cfg.root, repo, create=create_repo, public=public, confirm=confirm,
                        dry_run=dry_run)  # fmt: skip
    console.info(f"repository: {repo}")
    if not (cfg.root / WORKFLOW).is_file():
        console.warn(f"{WORKFLOW} is missing; these secrets are for the site template's deploy"
                     " workflow (`ym template-update`)")  # fmt: skip

    # Look everything up (and confirm host keys) before changing anything.
    prepared = []
    for plan in plans:
        spec, env = plan.spec, plan.env
        console.step(f"{spec.name} -> environment {env!r} ({spec.user + '@' if spec.user else ''}"
                     f"{spec.host}, {spec.type})")  # fmt: skip
        exists = env_exists(gh, repo, env)
        have = env_secrets(gh, repo, env) if exists else set()
        new_key = rotate or KEY_SECRET not in have
        hosts = host_keys(spec, confirm)
        if new_key and not dry_run:
            login = check_ssh(spec, password=True)
            if not login.ok:
                raise YMError(f"can't log in to {spec.host} to install the CI key ({login.reason});"
                              f" run `ym ssh-setup {spec.name}` first")  # fmt: skip
        if dry_run:
            console.info(f"  would {'create' if not exists else 'use'} environment {env}")
            console.info(f"  would set {HOSTS_SECRET} ({len(hosts)} line(s))")
            what = (
                f"generate and install a new CI key, set {KEY_SECRET}"
                if new_key
                else f"keep the existing {KEY_SECRET}"
            )
            console.info(f"  would {what}")
        prepared.append((spec, env, exists, KEY_SECRET in have, new_key, hosts))
    if dry_run:
        console.info("dry run: nothing changed")
        return

    for spec, env, exists, had_key, new_key, hosts in prepared:
        console.step(f"{spec.name}: configure environment {env!r}")
        if not exists:
            gh.run("api", "--method", "PUT", f"repos/{repo}/environments/{env}")
            console.info(f"  created environment {env}")
        if new_key:
            comment = f"ym-ci-{cfg.site.slug}-{env}-{utc_now().strftime('%Y%m%d')}"
            with tempfile.TemporaryDirectory(prefix="ym-ci-key-") as tmp:
                key = generate_key(Path(tmp), comment)
                install_public_key(spec, key.public)
                check_key(spec, key.private)
                set_secret(gh, repo, env, KEY_SECRET, key.private.read_text())
            console.info(f"  set {KEY_SECRET} (key {comment}; the private key exists only there)")
            if had_key:
                console.info("  the previous CI key is still authorized on the server: remove its"
                             " `ym-ci-` line from ~/.ssh/authorized_keys")  # fmt: skip
        else:
            console.info(f"  {KEY_SECRET} already set; kept (--rotate replaces it)")
        set_secret(gh, repo, env, HOSTS_SECRET, "\n".join(hosts) + "\n")
        console.info(f"  set {HOSTS_SECRET} ({len(hosts)} line(s))")
    console.info("done: push a release tag (`ym release --push`) or run the deploy workflow"
                 " by hand (`gh workflow run deploy.yml -f target=prod`)")  # fmt: skip


def is_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()
