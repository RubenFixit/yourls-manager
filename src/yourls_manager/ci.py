"""`ym ci setup` / `ym ci status`: deploy from GitHub Actions, opt-in per target (§13.2).

The site template's ``.github/workflows/deploy.yml`` deploys target ``prod`` in the
``production`` environment and every other target in ``staging``, only for targets with
``ci = true`` in site.toml, and needs two secrets on that environment: ``DEPLOY_SSH_KEY``
(a private key the server accepts) and ``DEPLOY_KNOWN_HOSTS`` (the server's host keys).

`ym ci setup`, using the GitHub CLI:

1. checks that ``gh`` is installed and logged in (offers ``gh auth login`` on a terminal);
2. finds the site's GitHub repository (offers `ym repo create` when there is none);
3. creates the environment if it is missing;
4. generates a dedicated CI key (ed25519, no passphrase) per environment, installs its
   public half on the server over the working ym login (ssh, or SFTP for sftp targets),
   checks that it logs in, and stores the private half only in the GitHub secret;
5. takes the host keys from ``~/.ssh/known_hosts`` (falling back to ``ssh-keyscan`` with a
   fingerprint confirmation);
6. sets ``ci = true`` on the target in site.toml (commit it: the workflow reads it).

An existing ``DEPLOY_SSH_KEY`` is kept unless ``--rotate``.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import SiteConfig, TargetSpec, load_site
from .github import Gh, current_repo, ensure_gh, gh_logged_in, repo_exists
from .util import UsageError, YMError, console, utc_now

KEY_SECRET = "DEPLOY_SSH_KEY"
HOSTS_SECRET = "DEPLOY_KNOWN_HOSTS"
WORKFLOW = ".github/workflows/deploy.yml"

# Appends "$1" to ~/.ssh/authorized_keys unless the exact line is there (run via sh -c).
AUTHORIZED_KEYS_SCRIPT = (
    'umask 077; mkdir -p "$HOME/.ssh" && touch "$HOME/.ssh/authorized_keys" && '
    '{ grep -qxF "$1" "$HOME/.ssh/authorized_keys" || '
    'printf "%s\\n" "$1" >> "$HOME/.ssh/authorized_keys"; }'
)

Confirm = Callable[[str], bool]


def environment_for(target: str) -> str:
    """The deploy workflow's mapping: prod -> production, anything else -> staging."""
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


# --------------------------------------------------------------------------- flow


@dataclass
class Plan:
    env: str
    spec: TargetSpec


def pick_targets(cfg: SiteConfig, names: list[str]) -> list[Plan]:
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


# --------------------------------------------------------------------------- site.toml

_TABLE = re.compile(r"^\s*\[")
_CI = re.compile(r"^(\s*ci\s*=\s*)(true|false)(.*)$")
_CI_HINT = re.compile(r"^(\s*)#\s*ci(\s*=\s*)(?:true|false)(.*\n?)$", re.S)


def set_ci(cfg: SiteConfig, target: str) -> bool:
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
            lines.insert(last, "ci          = true             # deploy from GitHub Actions\n")
    path.write_text("".join(lines), encoding="utf-8")
    try:
        if not load_site(cfg.root).target(target).ci:
            raise UsageError("edit did not take effect")
    except Exception:
        path.write_text(original, encoding="utf-8")
        raise
    return True


# --------------------------------------------------------------------------- setup


def ci_setup(
    cfg: SiteConfig,
    targets: list[str],
    *,
    repo: str | None = None,
    rotate: bool = False,
    dry_run: bool = False,
    confirm: Confirm | None = None,
    gh: Gh | None = None,
) -> None:
    from .login import check_login

    gh = gh or Gh(cwd=cfg.root)
    plans = pick_targets(cfg, targets)
    ensure_gh(gh, confirm=confirm)
    repo = find_repo(cfg, gh, repo, confirm=confirm, dry_run=dry_run)
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
    if dry_run:
        console.info("dry run: nothing changed")
        return

    changed_toml = False
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
        if set_ci(cfg, spec.name):
            console.info("  site.toml: ci = true")
            changed_toml = True
    if changed_toml:
        console.info(
            "commit site.toml (the workflow reads `ci` from the commit it deploys):"
            ' git commit -m "Deploy from GitHub Actions" site.toml && git push'
        )
    console.info(
        "done: release tags (`ym release --push`) now deploy from GitHub Actions; or"
        " run the workflow by hand (`gh workflow run deploy.yml -f target=prod`)"
    )


# --------------------------------------------------------------------------- status


@dataclass
class CiCheck:
    target: str
    ok: bool
    problems: list[str]


def ci_check(cfg: SiteConfig, spec: TargetSpec, gh: Gh, repo: str) -> CiCheck:
    env = environment_for(spec.name)
    problems = []
    if not env_exists(gh, repo, env):
        problems.append(f"environment {env!r} does not exist")
    else:
        missing = {KEY_SECRET, HOSTS_SECRET} - env_secrets(gh, repo, env)
        if missing:
            problems.append(f"environment {env!r} lacks {', '.join(sorted(missing))}")
    if not (cfg.root / WORKFLOW).is_file():
        problems.append(f"{WORKFLOW} is missing")
    return CiCheck(spec.name, not problems, problems)


def ci_status(cfg: SiteConfig, targets: list[str], *, gh: Gh | None = None) -> bool:
    """Print the CI state of the targets (default: all with ci = true). True when ready."""
    gh = gh or Gh(cwd=cfg.root)
    specs = [cfg.target(n) for n in targets] if targets else [
        t for t in cfg.targets.values() if t.ci
    ]  # fmt: skip
    if not specs:
        console.info("no target has ci = true: every deploy runs from this machine"
                     " (`ym ci setup` enables GitHub Actions deploys)")  # fmt: skip
        return True
    if not gh_logged_in(gh):
        raise UsageError("checking needs the GitHub CLI, logged in (`gh auth login`)")
    repo = current_repo(gh)
    if repo is None:
        console.info("FAIL  no GitHub repository for this site (`ym repo create`)")
        return False
    console.info(f"repository: {repo}")
    ok = True
    for spec in specs:
        res = ci_check(cfg, spec, gh, repo)
        flag = "" if spec.ci else " (ci = false: the workflow skips it)"
        env = environment_for(spec.name)
        if res.ok:
            console.info(f"ok    {spec.name}: environment {env!r} has both secrets{flag}")
        else:
            console.info(f"FAIL  {spec.name}: {'; '.join(res.problems)}{flag}"
                         f"  -> ym ci setup {spec.name}")  # fmt: skip
        ok &= res.ok
    return ok


def is_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()
