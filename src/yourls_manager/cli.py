"""Command line interface: ``yourls-manager`` / ``ym`` (design §4)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .config import REDIRECTS_FILE, SiteConfig, find_site_dir, load_site
from .util import EXIT_ERROR, EXIT_OK, EXIT_UPDATES, EXIT_USAGE, UsageError, YMError, console


def _global_options(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    default = argparse.SUPPRESS if suppress else None
    parser.add_argument(
        "--site-dir",
        type=Path,
        default=default,
        metavar="PATH",
        help="site repo directory (default: search upward from cwd for site.toml)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS if suppress else False,
        help="verbose output",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ym",
        description="Manage a Git-tracked YOURLS site: vendor core and plugins, build, deploy.",
    )
    parser.add_argument("--version", action="version", version=f"yourls-manager {__version__}")
    _global_options(parser, suppress=False)
    common = argparse.ArgumentParser(add_help=False)
    _global_options(common, suppress=True)

    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    sub.add_parser("status", parents=[common], help="show locked versions and drift")

    p = sub.add_parser("check", parents=[common], help="check upstreams for updates (exit 10)")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    p = sub.add_parser("core", parents=[common], help="vendor YOURLS core at a tag")
    p.add_argument(
        "--version",
        dest="core_version",
        metavar="V",
        help="tag to vendor, or 'latest' (default: site.toml [core].version)",
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="ignore uncommitted changes in yourls/")

    p = sub.add_parser("sync", parents=[common], help="vendor plugins into yourls/user/plugins")
    p.add_argument(
        "--plugin",
        action="append",
        metavar="NAME",
        default=[],
        help="limit to this plugin (repeatable)",
    )
    p.add_argument("--update", action="store_true", help="re-resolve refs to latest commits")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="ignore uncommitted changes in yourls/")

    p = sub.add_parser("build", parents=[common], help="assemble dist/ from yourls/ + overlay/")
    p.add_argument("--out", type=Path, metavar="DIR", help="output dir (default: [build].out)")
    p.add_argument(
        "--allow-drift", action="store_true", help="build even if yourls/ differs from the lock"
    )

    p = sub.add_parser("deploy", parents=[common], help="build and deploy to a target")
    p.add_argument("target")
    p.add_argument("--dry-run", action="store_true", help="show what rsync would change, then stop")
    p.add_argument("--no-build", action="store_true", help="deploy the existing build output")
    p.add_argument("--skip-backup", action="store_true", help="do not dump the database first")
    p.add_argument("--skip-verify", action="store_true", help="do not run verify afterwards")

    p = sub.add_parser("verify", parents=[common], help="health check + redirect tests")
    p.add_argument("target", nargs="?")
    p.add_argument("--url", help="base URL to check instead of a target's url")

    p = sub.add_parser("plugins", parents=[common], help="apply the active plugin set on a target")
    p.add_argument("target")
    return parser


def _load(args: argparse.Namespace) -> SiteConfig:
    start = args.site_dir.resolve() if args.site_dir else None
    if start is not None and not (start / "site.toml").is_file():
        # allow pointing at a subdirectory too
        return load_site(find_site_dir(start))
    return load_site(start or find_site_dir())


def _dispatch(args: argparse.Namespace) -> int:
    cmd = args.command

    if cmd == "verify":
        from .verify import verify

        if args.url and args.target:
            raise UsageError("give either TARGET or --url, not both")
        if not args.url and not args.target:
            raise UsageError("verify needs TARGET or --url")
        try:
            cfg = _load(args)
            cases = cfg.root / REDIRECTS_FILE
            url = args.url or cfg.target(args.target).url
        except YMError:
            if not args.url:
                raise
            cases = Path.cwd() / REDIRECTS_FILE
            url = args.url
        return EXIT_OK if verify(url, cases) else EXIT_ERROR

    cfg = _load(args)

    if cmd == "status":
        from .report import status

        status(cfg)
        return EXIT_OK

    if cmd == "check":
        from .report import check_updates, print_check

        report = check_updates(cfg)
        print_check(report, args.json)
        if report["updates_available"]:
            return EXIT_UPDATES
        if any(p.get("error") for p in report["plugins"].values()):
            return EXIT_ERROR
        return EXIT_OK

    if cmd == "core":
        from .core import update_core

        update_core(cfg, args.core_version, dry_run=args.dry_run, force=args.force)
        return EXIT_OK

    if cmd == "sync":
        from .plugins import sync_plugins

        sync_plugins(
            cfg, args.plugin or None, update=args.update, dry_run=args.dry_run, force=args.force
        )
        return EXIT_OK

    if cmd == "build":
        from .build import build_site

        out = args.out.resolve() if args.out else None
        build_site(cfg, out, allow_drift=args.allow_drift)
        return EXIT_OK

    if cmd == "deploy":
        from .deploy import DeployOptions, get_target

        target = get_target(cfg, args.target)
        ok = target.deploy(
            DeployOptions(
                dry_run=args.dry_run,
                no_build=args.no_build,
                skip_backup=args.skip_backup,
                skip_verify=args.skip_verify,
            )
        )
        return EXIT_OK if ok else EXIT_ERROR

    if cmd == "plugins":
        from .deploy import get_target

        get_target(cfg, args.target).apply_plugins()
        return EXIT_OK

    raise YMError(f"unknown command {cmd}")  # pragma: no cover


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    console.verbose = bool(getattr(args, "verbose", False))
    try:
        return _dispatch(args)
    except YMError as exc:
        console.error(str(exc))
        return exc.exit_code
    except KeyboardInterrupt:
        console.error("interrupted")
        return 130
    except OSError as exc:
        console.error(str(exc))
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

__all__ = ["EXIT_USAGE", "build_parser", "main"]
