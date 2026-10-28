"""Deploy targets, keyed by ``type`` in ``[deploy.targets.<name>]``.

Future target types (e.g. ``docker``, ``windows-release``) register here.
"""

from __future__ import annotations

from .base import DeployOptions, Target
from .rsync import RsyncTarget

TARGET_TYPES: dict[str, type[Target]] = {
    "rsync": RsyncTarget,
}

__all__ = ["TARGET_TYPES", "DeployOptions", "Target", "get_target"]


def get_target(cfg, name: str) -> Target:
    spec = cfg.target(name)
    return TARGET_TYPES[spec.type](cfg, spec)
