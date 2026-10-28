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
    p.add_argument("--dry-run", action="store_true", help="show what would change, then stop")
    p.add_argument("--no-build", action="store_true", help="deploy the existing build output")
    p.add_argument(
        "--skip-backup",
        action="store_true",
        help="do not dump the database first (sftp targets never do)",
    )
    p.add_argument("--skip-verify", action="store_true", help="do not run verify afterwards")
    p.add_argument(
        "--verify-remote",
        action="store_true",
        help="sftp targets: stat every deployed file and re-upload ones changed on the server",
    )

    p = sub.add_parser("verify", parents=[common], help="health check + redirect tests")
    p.add_argument("target", nargs="?")
    p.add_argument("--url", help="base URL to check instead of a target's url")

    p = sub.add_parser("plugins", parents=[common], help="apply the active plugin set on a target")
    p.add_argument("target")

    _onboarding_parsers(sub, common)
    return parser


def _onboarding_parsers(sub, common: argparse.ArgumentParser) -> None:
    """Design §9: new, template-update, server-config, adopt, setup."""
    fmt = argparse.RawDescriptionHelpFormatter
    p = sub.add_parser(
        "new",
        parents=[common],
        formatter_class=fmt,
        help="create a site repo from the template (copier), vendor core + plugins, git init",
        description=(
            "Create a new site repo from yourls-site-template with Copier (run as `uvx copier`),\n"
            "then vendor YOURLS core and plugins (ym core + ym sync), best-effort `uv lock`,\n"
            "and make the initial git commit. DEST must be empty or not exist."
        ),
        epilog=(
            "examples:\n"
            "  ym new go-example-com\n"
            "  ym new go-example-com --defaults --data site_name='Example Go Links' \\\n"
            "      --data site_url=https://go.example.com\n"
            "environment:\n"
            "  YM_TEMPLATE   template source used when --template is not given\n"
            "                (default: gh:RubenFixit/yourls-site-template)"
        ),
    )
    p.add_argument("dest", nargs="?", default=".", metavar="DEST", help="new site directory")
    p.add_argument("--template", metavar="SRC", help="template (path, git URL or gh:owner/repo)")
    p.add_argument(
        "--vcs-ref", metavar="REF", help="template git ref (default: its latest release tag)"
    )
    p.add_argument(
        "--data",
        action="append",
        default=[],
        metavar="K=V",
        help="answer a template question (repeatable), e.g. site_url=https://go.example.com",
    )
    p.add_argument("--defaults", action="store_true", help="use defaults for unanswered questions")
    p.add_argument("--no-vendor", action="store_true", help="skip `ym core` + `ym sync`")
    p.add_argument("--no-git", action="store_true", help="skip git init and the initial commit")

    p = sub.add_parser(
        "template-update",
        parents=[common],
        help="pull template changes into this site repo (copier update)",
        description=(
            "Run `copier update` in a clean site repo, then best-effort `uv lock`, and show"
            " what changed. Previously answered questions are not asked again."
        ),
    )
    p.add_argument("--vcs-ref", metavar="REF", help="template git ref (default: latest tag)")
    p.add_argument(
        "--manager-version",
        metavar="vX.Y.Z",
        help="pin this yourls-manager tag in pyproject.toml (manager_version answer)",
    )
    p.add_argument("--defaults", action="store_true", help="accept defaults for new questions")

    p = sub.add_parser(
        "server-config",
        parents=[common],
        formatter_class=fmt,
        help="create the server-local YOURLS config (secrets) on a target",
        description=(
            "Write the target's config_file (mode 0600) from server/config.example.php:\n"
            "DB settings, YOURLS_SITE (the target's url), a fresh cookie key and one admin\n"
            "user whose password is hashed by the target's PHP (sftp targets: locally with\n"
            "bcrypt). With --from-existing, copy the existing install's user/config.php\n"
            "instead (when adopting)."
        ),
        epilog=(
            "secrets (never pass them as arguments):\n"
            "  YM_DB_PASSWORD      database password (else prompted)\n"
            "  YM_ADMIN_PASSWORD   admin password (else prompted twice)"
        ),
    )
    p.add_argument("target")
    p.add_argument(
        "--force", action="store_true", help="replace an existing file (kept as .bak-<UTC>)"
    )
    p.add_argument(
        "--from-existing",
        action="store_true",
        help="copy <web root>/user/config.php (or includes/config.php) to config_file",
    )
    p.add_argument("--db-host", metavar="H", help="YOURLS_DB_HOST (default: example's value)")
    p.add_argument("--db-name", metavar="N", help="YOURLS_DB_NAME (prompted if omitted)")
    p.add_argument("--db-user", metavar="U", help="YOURLS_DB_USER (prompted if omitted)")
    p.add_argument("--db-prefix", metavar="P", help="YOURLS_DB_PREFIX (default: yourls_)")
    p.add_argument("--admin-user", metavar="U", help="admin username (prompted if omitted)")

    p = sub.add_parser(
        "adopt",
        parents=[common],
        formatter_class=fmt,
        help="bring an existing YOURLS install under management",
        description=(
            "Snapshot an existing install, vendor core at its version, copy site-specific\n"
            "files and plugins into overlay/ (collisions with template files go to\n"
            "adopt-review/), add [[plugins]] entries, and check build/install parity.\n"
            "Writes adopt-review/REPORT.md. Run inside a site repo created by `ym new`.\n"
            "sftp targets: the active plugins can't be read; give them with --active, or\n"
            "answer the per-plugin prompt (on a terminal)."
        ),
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-target", metavar="TARGET", help="pull the target's web root")
    src.add_argument("--from-dir", type=Path, metavar="PATH", help="a local copy of the install")
    p.add_argument(
        "--ignore",
        action="append",
        default=[],
        metavar="GLOB",
        help="extra path glob to ignore (repeatable; added to the defaults)",
    )
    p.add_argument(
        "--active",
        metavar="A,B,...",
        help="plugins active on the install (comma-separated; '' for none);"
        " instead of reading them from its database",
    )
    p.add_argument("--dry-run", action="store_true", help="analyze and print the plan only")

    p = sub.add_parser(
        "setup",
        parents=[common],
        help="interactive wizard: new site or adopt an existing install (resumable)",
        description=(
            "Guided onboarding (needs a terminal). Prints each equivalent command. Progress is"
            " saved, so running it again after an error resumes where it stopped."
        ),
    )
    p.add_argument("dir", nargs="?", type=Path, metavar="DIR", help="site repo to resume")

    p = sub.add_parser(
        "ssh-setup",
        parents=[common],
        help="check SSH key login to a target; create and install a key if needed",
        description=(
            "ym runs ssh non-interactively (BatchMode), so targets need key authentication."
            " This checks it and otherwise runs ssh-keygen and ssh-copy-id for you"
            " (asking for the SSH password once). For sftp targets the check is an SFTP"
            " login and the key is appended to .ssh/authorized_keys over SFTP."
        ),
    )
    p.add_argument("target")
    p.add_argument(
        "--key", type=Path, metavar="PATH", help="private key path (default: ~/.ssh/id_ed25519)"
    )


def _load(args: argparse.Namespace) -> SiteConfig:
    start = args.site_dir.resolve() if args.site_dir else None
    if start is not None and not (start / "site.toml").is_file():
        # allow pointing at a subdirectory too
        return load_site(find_site_dir(start))
    return load_site(start or find_site_dir())


def _dispatch(args: argparse.Namespace) -> int:
    cmd = args.command

    if cmd == "new":
        from .newsite import new_site

        new_site(
            Path(args.dest),
            template=args.template,
            vcs_ref=args.vcs_ref,
            data=args.data,
            defaults=args.defaults,
            vendor=not args.no_vendor,
            git_init=not args.no_git,
        )
        return EXIT_OK

    if cmd == "template-update":
        from .newsite import template_update

        start = args.site_dir.resolve() if args.site_dir else None
        template_update(
            start,
            vcs_ref=args.vcs_ref,
            manager_version=args.manager_version,
            defaults=args.defaults,
        )
        return EXIT_OK

    if cmd == "setup":
        return _setup(args.dir)

    if cmd == "ssh-setup":
        from .sshsetup import ssh_setup

        cfg = _load(args)
        spec = cfg.target(args.target)
        if not spec.is_local and not (sys.stdin.isatty() and sys.stdout.isatty()):
            from .sshsetup import check_ssh

            res = check_ssh(spec)
            if res.ok:
                console.info("key authentication works")
                return EXIT_OK
            raise UsageError(
                "`ym ssh-setup` needs a terminal to create or install a key"
                + (f"\n{res.detail}" if spec.is_sftp and res.detail else "")
            )
        ok = ssh_setup(spec, confirm=_confirm, key=args.key)
        return EXIT_OK if ok else EXIT_ERROR

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
        if args.verify_remote and not target.spec.is_sftp:
            raise UsageError("--verify-remote applies to sftp targets only")
        ok = target.deploy(
            DeployOptions(
                dry_run=args.dry_run,
                no_build=args.no_build,
                skip_backup=args.skip_backup,
                skip_verify=args.skip_verify,
                verify_remote=args.verify_remote,
            )
        )
        return EXIT_OK if ok else EXIT_ERROR

    if cmd == "plugins":
        from .deploy import get_target

        get_target(cfg, args.target).apply_plugins()
        return EXIT_OK

    if cmd == "server-config":
        from .serverconfig import ServerConfigOptions, server_config

        server_config(
            cfg,
            args.target,
            ServerConfigOptions(
                force=args.force,
                from_existing=args.from_existing,
                db_host=args.db_host,
                db_name=args.db_name,
                db_user=args.db_user,
                db_prefix=args.db_prefix,
                admin_user=args.admin_user,
            ),
        )
        return EXIT_OK

    if cmd == "adopt":
        from .adopt import adopt

        adopt(
            cfg,
            from_target=args.from_target,
            from_dir=args.from_dir,
            ignore=args.ignore,
            dry_run=args.dry_run,
            active=None if args.active is None else args.active.split(","),
        )
        return EXIT_OK

    raise YMError(f"unknown command {cmd}")  # pragma: no cover


def _confirm(question: str) -> bool:
    while True:
        answer = input(f"{question} [Y/n]: ").strip().lower()
        if answer in ("", "y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def _setup(start: Path | None = None) -> int:
    from .wizard import run_wizard

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise UsageError(
            "`ym setup` is interactive and needs a terminal; use `ym new`, `ym adopt`,"
            " `ym server-config` and `ym deploy` directly"
        )

    def ask(question: str, default: str | None) -> str:
        suffix = f" [{default}]" if default else ""
        answer = input(f"{question}{suffix}: ")
        return answer if answer.strip() else (default or "")

    return run_wizard(ask, invoke=main, start=start).exit_code


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    console.verbose = bool(getattr(args, "verbose", False))
    try:
        return _dispatch(args)
    except YMError as exc:
        console.error(str(exc))
        return exc.exit_code
    except (KeyboardInterrupt, EOFError):
        console.error("interrupted")
        return 130
    except OSError as exc:
        console.error(str(exc))
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

__all__ = ["EXIT_USAGE", "build_parser", "main"]
