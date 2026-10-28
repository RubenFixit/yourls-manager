"""Base class for deploy targets."""

from __future__ import annotations

from dataclasses import dataclass

from ..config import SiteConfig, TargetSpec


@dataclass
class DeployOptions:
    dry_run: bool = False
    no_build: bool = False
    skip_backup: bool = False
    skip_verify: bool = False


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
