"""`ym new` and `ym template-update` (design §9): create/update a site repo with Copier.

Copier is run as a subprocess (``uvx copier`` when uv is available, else ``copier``) so it
never becomes a runtime dependency of the tool.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Sequence
from pathlib import Path

from .config import SITE_FILE, find_site_dir, load_site
from .util import UsageError, YMError, console, run

DEFAULT_TEMPLATE = "gh:RubenFixit/yourls-site-template"
TEMPLATE_ENV = "YM_TEMPLATE"
ANSWERS_FILE = ".copier-answers.yml"
INITIAL_COMMIT_MESSAGE = "Create site from yourls-site-template"
MANAGER_VERSION_RE = re.compile(r"^v\d+\.\d+\.\d+$")
DATA_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Never let git (run by copier or uv) block on a credential prompt.
_NO_PROMPT_ENV = {"GIT_TERMINAL_PROMPT": "0"}


def copier_command() -> list[str]:
    """argv prefix that runs Copier: ``uvx copier``, else ``copier``, else a UsageError."""
    uv = shutil.which("uv")
    if uv:
        uvx = shutil.which("uvx")
        return [uvx, "copier"] if uvx else [uv, "tool", "run", "copier"]
    copier = shutil.which("copier")
    if copier:
        return [copier]
    raise UsageError(
        "Copier is needed to create or update a site repo, but neither `uv` nor `copier` is on"
        " PATH.\nInstall uv (https://docs.astral.sh/uv/getting-started/installation/), e.g.\n"
        "  curl -LsSf https://astral.sh/uv/install.sh | sh\n"
        "then re-run this command (it uses `uvx copier`)."
    )


def _data_args(data: Sequence[str]) -> list[str]:
    args: list[str] = []
    for item in data:
        key, sep, _value = item.partition("=")
        if not sep or not DATA_KEY_RE.match(key):
            raise UsageError(f"--data expects KEY=VALUE, got {item!r}")
        args += ["--data", item]
    return args


def uv_lock(site: Path) -> bool:
    """Best-effort ``uv lock`` in the site repo. Returns True on success."""
    uv = shutil.which("uv")
    if not uv:
        console.warn("uv not found; skipped `uv lock` (run it later to write uv.lock)")
        return False
    console.step("uv lock")
    proc = run([uv, "lock"], cwd=site, env=_NO_PROMPT_ENV, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode(errors="replace").strip().splitlines()
        tail = "\n  ".join(detail[-6:])
        console.warn(
            "`uv lock` failed (e.g. the pinned yourls-manager tag is not published yet);"
            " the site repo is still usable with a globally installed `ym`. Run `uv lock`"
            " later." + (f"\n  {tail}" if tail else "")
        )
        return False
    console.info("  wrote uv.lock")
    return True


def _git_identity_missing(stderr: str) -> bool:
    s = stderr.lower()
    return "tell me who you are" in s or "identity unknown" in s or "empty ident" in s


def _git_init_commit(dest: Path) -> bool:
    console.step("git init")
    run(["git", "init", "-q", "-b", "main"], cwd=dest)
    run(["git", "add", "-A"], cwd=dest)
    proc = run(["git", "commit", "-q", "-m", INITIAL_COMMIT_MESSAGE], cwd=dest, check=False)
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode(errors="replace")
        if _git_identity_missing(err):
            console.warn(
                "git user.name/user.email are not configured; files are staged but not"
                " committed. Configure git, then run:\n"
                f'  git -C {dest} commit -m "{INITIAL_COMMIT_MESSAGE}"'
            )
            return False
        raise YMError(f"git commit failed:\n{err.strip()}")
    console.info(f'  initial commit "{INITIAL_COMMIT_MESSAGE}"')
    return True


def new_site(
    dest: Path,
    *,
    template: str | None = None,
    vcs_ref: str | None = None,
    data: Sequence[str] = (),
    defaults: bool = False,
    vendor: bool = True,
    git_init: bool = True,
) -> Path:
    """Render the template into ``dest`` and vendor core + plugins. Returns the site dir."""
    dest = dest.expanduser().resolve()
    if dest.exists():
        if not dest.is_dir():
            raise UsageError(f"{dest} exists and is not a directory")
        if any(dest.iterdir()):
            raise UsageError(f"refusing to create a site in non-empty directory {dest}")
    src = template or os.environ.get(TEMPLATE_ENV) or DEFAULT_TEMPLATE
    argv = [*copier_command(), "copy", "--quiet"]
    if vcs_ref:
        argv += ["--vcs-ref", vcs_ref]
    if defaults:
        argv.append("--defaults")
    argv += _data_args(data)
    argv += ["--", src, str(dest)]

    console.step(f"copier copy {src} -> {dest}")
    proc = run(argv, env=_NO_PROMPT_ENV, check=False, capture=False)
    if proc.returncode != 0:
        raise YMError(f"copier failed (exit {proc.returncode}); see its output above")
    if not (dest / SITE_FILE).is_file():
        raise YMError(f"the template did not produce {SITE_FILE} in {dest}")

    cfg = load_site(dest)
    if vendor:
        from .core import update_core
        from .plugins import sync_plugins

        console.step(f"ym core (YOURLS {cfg.core.version})")
        update_core(cfg)
        cfg = load_site(dest)
        console.step("ym plugin sync")
        sync_plugins(cfg, check_dirty=False)  # brand-new repo: nothing to lose
    else:
        console.info("skipped vendoring (--no-vendor); run `ym core` and `ym plugin sync` later")

    uv_lock(dest)
    if git_init:
        _git_init_commit(dest)

    prod = "prod" in cfg.targets
    console.info("")
    console.info(f"Site repo ready: {dest}")
    console.info("Next steps:")
    console.info(f"  cd {dest}")
    if not prod:
        console.info("  # edit site.toml: uncomment and fill in [deploy.targets.prod]")
    console.info("  ym target config prod          # create the server-local config (secrets)")
    console.info("  ym deploy prod --dry-run       # preview the first deploy")
    console.info("  ym deploy prod")
    if prod and cfg.targets["prod"].is_sftp:
        console.info("  # sftp target: ym prints the browser steps (install/upgrade page, plugins)")
    console.info("Adopting an existing YOURLS install instead? `ym adopt --from-target prod`")
    return dest


# --------------------------------------------------------------------------- update


def _git_dirty(root: Path) -> list[str]:
    probe = run(["git", "rev-parse", "--is-inside-work-tree"], cwd=root, check=False)
    if probe.returncode != 0:
        raise UsageError(f"{root} is not a git repository; `copier update` needs one")
    proc = run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=root)
    return [ln for ln in proc.stdout.decode(errors="replace").splitlines() if ln.strip()]


def template_update(
    site_dir: Path | None = None,
    *,
    vcs_ref: str | None = None,
    manager_version: str | None = None,
    defaults: bool = False,
) -> None:
    root = find_site_dir(site_dir)
    if not (root / ANSWERS_FILE).is_file():
        raise UsageError(
            f"{root} has no {ANSWERS_FILE}; it was not created from the template"
            " (`ym new`), so `copier update` cannot run"
        )
    if manager_version is not None and not MANAGER_VERSION_RE.match(manager_version):
        raise UsageError(f"--manager-version must look like v0.2.0, got {manager_version!r}")
    dirty = _git_dirty(root)
    if dirty:
        shown = "\n  ".join(dirty[:20]) + ("\n  ..." if len(dirty) > 20 else "")
        raise UsageError(
            f"the site repo has uncommitted changes; commit or stash them first:\n  {shown}"
        )

    argv = [*copier_command(), "update", "--skip-answered"]
    if vcs_ref:
        argv += ["--vcs-ref", vcs_ref]
    if defaults:
        argv.append("--defaults")
    if manager_version:
        argv += ["--data", f"manager_version={manager_version}"]
    argv.append(str(root))
    console.step("copier update")
    proc = run(argv, cwd=root, env=_NO_PROMPT_ENV, check=False, capture=False)
    if proc.returncode != 0:
        raise YMError(f"copier update failed (exit {proc.returncode}); see its output above")

    uv_lock(root)
    stat = run(["git", "diff", "--stat"], cwd=root, check=False).stdout.decode(errors="replace")
    untracked = run(
        ["git", "ls-files", "--others", "--exclude-standard"], cwd=root, check=False
    ).stdout.decode(errors="replace")
    console.info("")
    if stat.strip() or untracked.strip():
        console.info("Changes from the template update:")
        if stat.strip():
            console.info(stat.rstrip())
        for line in untracked.splitlines():
            console.info(f" {line} (new)")
        conflicts = run(
            ["git", "grep", "-l", "-E", "^<<<<<<< (before updating|before update)"],
            cwd=root,
            check=False,
        ).stdout.decode(errors="replace")
        if conflicts.strip():
            console.warn("merge conflicts left by copier in:\n  " + "\n  ".join(conflicts.split()))
        console.info("Review with `git diff`, then commit, e.g.:")
        console.info('  git add -A && git commit -m "Update from yourls-site-template"')
    else:
        console.info("Template update made no changes.")
