"""Base class for deploy targets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .. import __version__
from ..config import SiteConfig, TargetSpec
from ..lockfile import load_lock, lock_sha256
from ..util import iso_utc, run


@dataclass
class DeployOptions:
    dry_run: bool = False
    no_build: bool = False
    skip_backup: bool = False
    skip_verify: bool = False
    verify_remote: bool = False  # sftp only: stat every deployed file


# Set by the deploy policy check (release.check_deploy) for the current command: the
# release tag at HEAD and whether the policy was overridden. Merged into release.json.
DEPLOY_CONTEXT: dict[str, Any] = {}


def release_info(cfg: SiteConfig, spec: TargetSpec) -> dict[str, Any]:
    """What was deployed: git commit + dirty flag, tool version, lock hash, core version."""
    lock = load_lock(cfg.lock_file)
    commit = run(["git", "rev-parse", "HEAD"], cwd=cfg.root, check=False)
    status = run(["git", "status", "--porcelain"], cwd=cfg.root, check=False)
    return {
        "site": cfg.site.slug,
        "target": spec.name,
        "git_commit": commit.stdout.decode().strip() if commit.returncode == 0 else None,
        "git_dirty": bool(status.stdout.strip()) if status.returncode == 0 else None,
        "tool_version": __version__,
        "lock_sha256": lock_sha256(cfg.lock_file),
        "core_version": lock.core.version if lock.core else None,
        "deployed_at": iso_utc(),
        **DEPLOY_CONTEXT,
    }


class Target:
    """A deploy target. Subclasses implement the flow for their ``type``."""

    def __init__(self, cfg: SiteConfig, spec: TargetSpec):
        self.cfg = cfg
        self.spec = spec

    def deploy(self, opts: DeployOptions) -> bool:
        """Run the deploy. Returns False when post-deploy verification failed."""
        raise NotImplementedError

    def apply_plugins(self) -> dict:
        """Make the active plugin set on the target exactly the site.toml set."""
        raise NotImplementedError
