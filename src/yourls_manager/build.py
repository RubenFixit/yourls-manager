"""`ym build`: assemble dist/ from yourls/ + overlay/."""

from __future__ import annotations

from pathlib import Path

from .config import SITE_FILE, SiteConfig
from .drift import compute_drift
from .lockfile import load_lock
from .util import UsageError, YMError, console
from .vendor import copy_tree, rmtree


def _check_out_dir(cfg: SiteConfig, out: Path) -> None:
    out = out.resolve()
    root = cfg.root.resolve()
    forbidden = {root, cfg.vendor_dir.resolve(), cfg.overlay_dir.resolve(), Path.home().resolve()}
    if out in forbidden or out == Path(out.anchor) or root.is_relative_to(out):
        raise UsageError(f"refusing to use {out} as build output directory")
    if (out / SITE_FILE).exists() or (out / ".git").exists():
        raise UsageError(f"refusing to clean {out}: it looks like a repository")


def build_site(cfg: SiteConfig, out: Path | None = None, *, allow_drift: bool = False) -> Path:
    out = (out or cfg.out_dir).resolve()
    _check_out_dir(cfg, out)
    lock = load_lock(cfg.lock_file)
    drift = compute_drift(cfg, lock)
    if not drift.clean:
        listing = "\n  ".join(drift.problems)
        if not allow_drift:
            raise YMError(
                f"vendored tree does not match the lock:\n  {listing}\n"
                "fix it (ym core / ym plugin sync) or pass --allow-drift"
            )
        console.warn(f"building despite drift:\n  {listing}")

    if out.exists():
        rmtree(out)
    out.mkdir(parents=True)
    n_core = copy_tree(cfg.vendor_dir, out)
    console.detail(f"copied {n_core} files from yourls/")

    overrides: list[str] = []

    def on_copy(rel: str, existed: bool) -> None:
        if existed:
            overrides.append(rel)
            console.detail(f"overlay overrides {rel}")

    n_overlay = 0
    if cfg.overlay_dir.is_dir():
        n_overlay = copy_tree(cfg.overlay_dir, out, on_copy=on_copy)
    else:
        console.warn("no overlay/ directory; the build has no site config shim")

    problems = []
    for p in cfg.plugins:
        if not (out / "user" / "plugins" / p.name / "plugin.php").is_file():
            msg = f"plugin {p.name}: user/plugins/{p.name}/plugin.php not in build"
            if p.active:
                problems.append(msg)
            else:
                console.warn(msg)
    if problems:
        raise YMError("\n".join(problems))

    console.info(
        f"build: {out} ({n_core} core/plugin files, {n_overlay} overlay files,"
        f" {len(overrides)} overridden)"
    )
    return out
