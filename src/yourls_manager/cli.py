"""Command line interface: ``yourls-manager`` / ``ym`` (design §4)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
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


# Old command spellings kept for one release (design §12): hidden from `ym --help`, they warn
# and then run the new command. Remove them in 0.8.
REMOVED_IN = "0.8"


def _deprecated(
    sub,
    common: argparse.ArgumentParser,
    old: str,
    new: str,
    add_args: Callable[[argparse.ArgumentParser], None] | None = None,
    **dest: str,
) -> argparse.ArgumentParser:
    """Register ``ym OLD`` as a deprecated spelling of ``ym NEW``.

    ``dest`` sets the namespace keys of the new command (e.g. ``command="plugin",
    plugin_cmd="apply"``); they override the subparser's own ``command=OLD``. No ``help=`` is
    given, so argparse leaves the alias out of the command list."""
    p = sub.add_parser(
        old,
        parents=[common],
        description=f"Deprecated (removed in {REMOVED_IN}): use `ym {new}`.",
    )
    if add_args is not None:
        add_args(p)
    p.set_defaults(deprecated=(old, new), **dest)
    return p


def _warn_deprecated(args: argparse.Namespace) -> None:
    dep = getattr(args, "deprecated", None)
    if dep:
        console.warn(
            f"`ym {dep[0]}` is deprecated and will be removed in {REMOVED_IN}; use `ym {dep[1]}`"
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
        "--allow-dirty", action="store_true", help="deploy even with uncommitted changes"
    )
    p.add_argument(
        "--force",
        action="store_true",
        help='override the target\'s accept = "release" policy (recorded in release.json)',
    )
    p.add_argument(
        "--verify-remote",
        action="store_true",
        help="sftp targets: stat every deployed file and re-upload ones changed on the server",
    )

    p = sub.add_parser("verify", parents=[common], help="health check + redirect tests")
    p.add_argument("target", nargs="?")
    p.add_argument("--url", help="base URL to check instead of a target's url")

    p = sub.add_parser(
        "plugin", parents=[common], help="search, add, remove, list, enable or disable plugins"
    )
    psub = p.add_subparsers(dest="plugin_cmd", metavar="ACTION", required=True)
    q = psub.add_parser(
        "list", parents=[common], help="plugins in site.toml: version, source, lock, updates"
    )
    q.add_argument("--json", action="store_true", help="machine-readable output")
    q.add_argument(
        "--check", action="store_true", help="ask upstream for newer commits (exit 10 if any)"
    )
    q = psub.add_parser(
        "apply", parents=[common], help="make the target's active plugins exactly site.toml's"
    )
    q.add_argument("target")
    q = psub.add_parser(
        "search",
        parents=[common],
        help="search the awesome-yourls plugin catalog",
        description=(
            "Search the awesome-yourls catalog (cached for a day) by name, tags and"
            " description. Plugins already in site.toml are marked."
        ),
    )
    q.add_argument("query", nargs="*", help="search words (all must match)")
    q.add_argument("--category", metavar="SLUG", help="limit to a category (lists it alone)")
    q.add_argument("--refresh", action="store_true", help="download the catalog again")
    q.add_argument("--json", action="store_true", help="machine-readable output")
    q = psub.add_parser(
        "add",
        parents=[common],
        help="add a plugin by catalog name or git/project URL",
        description=(
            "Add a plugin by its awesome-yourls catalog name (exact, case-insensitive or"
            " unique prefix) or by a clone URL / project page (GitHub, GitLab, Codeberg,"
            " Bitbucket; /tree/<ref>/<dir> sets ref and path). plugin.php is looked for at"
            " the top of the repo, else one directory down."
        ),
    )
    q.add_argument("source", metavar="NAME|URL", help="catalog name or git/project URL")
    q.add_argument("--name", help="plugin directory name (default: from plugin.php's folder)")
    q.add_argument("--ref", help="branch, tag or commit (default: from the URL, else HEAD)")
    q.add_argument("--path", help="subdirectory holding plugin.php")
    q.add_argument("--inactive", action="store_true", help="add it with active = false")
    q.add_argument(
        "--no-sync", action="store_true", help="only edit site.toml; vendor later with ym sync"
    )
    q.add_argument("--refresh", action="store_true", help="download the catalog again")
    q = psub.add_parser("remove", parents=[common], help="remove a plugin from the site")
    q.add_argument("name")
    q.add_argument("--keep-files", action="store_true", help="keep the vendored copy")
    for act in ("enable", "disable"):
        q = psub.add_parser(act, parents=[common], help=f"{act} a plugin in site.toml")
        q.add_argument("name")

    p = sub.add_parser(
        "update",
        parents=[common],
        help="update core to the latest release and plugins to their newest commits, then build",
    )
    p.add_argument("--commit", action="store_true", help="commit the update with a summary")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--core-only", action="store_true", help="only update YOURLS core")
    g.add_argument("--plugins-only", action="store_true", help="only update plugins")

    p = sub.add_parser(
        "release",
        parents=[common],
        help="tag a release: v<yourls>-<rev>[-rc.<n>] with notes from the lock diff",
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--rc", action="store_true", help="tag a release candidate")
    g.add_argument("--promote", action="store_true", help="tag the newest rc as its final")
    g.add_argument("--list", action="store_true", help="list releases and where they're deployed")
    p.add_argument("--push", action="store_true", help="push the new tag to origin")
    p.add_argument("--dry-run", action="store_true", help="show the tag and notes only")
    p.add_argument("-m", "--message", help="extra text for the release notes")

    _deprecated(
        sub,
        common,
        "plugins",
        "plugin apply",
        lambda p: p.add_argument("target"),
        command="plugin",
        plugin_cmd="apply",
    )

    p = sub.add_parser(
        "doctor",
        parents=[common],
        help="read-only health report: tools, site repo, targets (exit 1 on failures)",
        description=(
            "Check local tools, site.toml, the lock and drift, uncommitted and unpushed"
            " changes, and per target: login, PHP CLI (rsync), server config, https url,"
            " install/upgrade state, and the deployed commit vs HEAD. Changes nothing."
        ),
    )
    p.add_argument("target", nargs="?", help="check only this target (default: all)")

    p = sub.add_parser(
        "repo",
        parents=[common],
        help="store the site on GitHub (create the repo, push)",
    )
    rsub = p.add_subparsers(dest="repo_cmd", metavar="ACTION", required=True)
    q = rsub.add_parser(
        "create",
        parents=[common],
        help="create the site's GitHub repo as origin and push the current branch (needs gh)",
        description=(
            "Create a GitHub repository for this site with the GitHub CLI (private unless"
            " --public), add it as origin (or create the one origin already names), and push"
            " the current branch. Tags are not pushed. Nothing about deploying changes: that"
            " stays manual unless you run `ym ci setup`."
        ),
    )
    q.add_argument("--name", help="repository name (default: the site directory name)")
    q.add_argument("--owner", help="user or organization (default: your gh login)")
    q.add_argument("--public", action="store_true", help="create it public (default: private)")
    q.add_argument("--dry-run", action="store_true", help="show what would be created")

    p = sub.add_parser(
        "ci",
        parents=[common],
        help="deploy from GitHub Actions (opt-in per target): setup, status",
    )
    csub = p.add_subparsers(dest="ci_cmd", metavar="ACTION", required=True)
    q = csub.add_parser(
        "setup",
        parents=[common],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        help="enable GitHub Actions deploys: environment, CI key, host keys, ci = true",
        description=(
            "Enable deploys from the site's .github/workflows/deploy.yml with the GitHub CLI:\n"
            "create the environment (prod -> production, other targets -> staging), generate\n"
            "a dedicated CI key, install it on the server over your working login, check it\n"
            "logs in, store it as DEPLOY_SSH_KEY, store the host keys from ~/.ssh/known_hosts\n"
            "as DEPLOY_KNOWN_HOSTS, and set `ci = true` on the target in site.toml. Offers\n"
            "`gh auth login`, and `ym repo create` when the site has no GitHub repo yet."
        ),
        epilog=(
            "examples:\n"
            "  ym ci setup                 # prod, staging and targets with ci = true\n"
            "  ym ci setup prod --dry-run\n"
            "  ym ci setup prod --rotate   # replace the CI key"
        ),
    )
    q.add_argument("targets", nargs="*", metavar="TARGET", help="default: prod and staging")
    q.add_argument("--repo", metavar="OWNER/NAME", help="GitHub repo (default: from the remote)")
    q.add_argument("--rotate", action="store_true", help="replace an existing DEPLOY_SSH_KEY")
    q.add_argument("--dry-run", action="store_true", help="show what would be set, change nothing")
    q = csub.add_parser(
        "status",
        parents=[common],
        help="check the GitHub environments and secrets of ci = true targets (exit 1 if not ready)",
    )
    q.add_argument("targets", nargs="*", metavar="TARGET", help="default: targets with ci = true")

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

    if cmd == "doctor":
        from .doctor import doctor

        start = args.site_dir.resolve() if args.site_dir else None
        return EXIT_OK if doctor(start, args.target) else EXIT_ERROR

    if cmd == "plugin" and args.plugin_cmd == "search":
        from .catalog import plugin_search

        try:
            site_cfg = _load(args)
        except UsageError:
            site_cfg = None  # searching works outside a site repo too
        plugin_search(site_cfg, " ".join(args.query), category=args.category,
                      refresh=args.refresh, as_json=args.json)  # fmt: skip
        return EXIT_OK

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
        from .deploy.base import DEPLOY_CONTEXT
        from .release import check_deploy, move_marker

        target = get_target(cfg, args.target)
        if args.verify_remote and not target.spec.is_sftp:
            raise UsageError("--verify-remote applies to sftp targets only")
        DEPLOY_CONTEXT.update(
            check_deploy(cfg, target.spec, allow_dirty=args.allow_dirty, force=args.force)
        )
        ok = target.deploy(
            DeployOptions(
                dry_run=args.dry_run,
                no_build=args.no_build,
                skip_backup=args.skip_backup,
                skip_verify=args.skip_verify,
                verify_remote=args.verify_remote,
            )
        )
        if not args.dry_run:
            move_marker(cfg, target.spec)  # files are deployed even if verify failed
        return EXIT_OK if ok else EXIT_ERROR

    if cmd == "plugin":
        from .plugincmd import list_plugins, set_active

        if args.plugin_cmd == "list":
            updates = list_plugins(cfg, check=args.check, as_json=args.json)
            return EXIT_UPDATES if updates else EXIT_OK
        if args.plugin_cmd == "apply":
            from .deploy import get_target

            get_target(cfg, args.target).apply_plugins()
            return EXIT_OK
        if args.plugin_cmd == "add":
            from .plugincmd import add_plugin

            add_plugin(
                cfg,
                args.source,
                name=args.name,
                ref=args.ref,
                path=args.path,
                active=not args.inactive,
                sync=not args.no_sync,
                choose=_choose if sys.stdin.isatty() and sys.stdout.isatty() else None,
                refresh=args.refresh,
            )
            return EXIT_OK
        if args.plugin_cmd == "remove":
            from .plugincmd import remove_plugin

            remove_plugin(cfg, args.name, keep_files=args.keep_files)
            return EXIT_OK
        on = args.plugin_cmd == "enable"
        if set_active(cfg, args.name, on):
            console.info(f"{args.name}: active = {str(on).lower()} (applied on the next deploy)")
        else:
            console.info(f"{args.name}: already {'active' if on else 'inactive'}")
        return EXIT_OK

    if cmd == "repo":
        from .ci import is_tty
        from .github import repo_create

        repo_create(cfg, name=args.name, owner=args.owner, public=args.public,
                    dry_run=args.dry_run, confirm=_confirm if is_tty() else None)  # fmt: skip
        return EXIT_OK

    if cmd == "ci":
        from .ci import ci_setup, ci_status, is_tty

        if args.ci_cmd == "status":
            return EXIT_OK if ci_status(cfg, args.targets) else EXIT_ERROR
        ci_setup(cfg, args.targets, repo=args.repo, rotate=args.rotate, dry_run=args.dry_run,
                 confirm=_confirm if is_tty() else None)  # fmt: skip
        return EXIT_OK

    if cmd == "update":
        from .update import update

        update(cfg, commit=args.commit, core=not args.plugins_only, plugins=not args.core_only)
        return EXIT_OK

    if cmd == "release":
        from .release import list_releases, release

        if args.list:
            list_releases(cfg)
            return EXIT_OK
        release(
            cfg,
            rc=args.rc,
            promote=args.promote,
            push=args.push,
            dry_run=args.dry_run,
            message=args.message,
        )
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


def _choose(question: str, options: list[str]) -> str:
    console.info(question)
    for i, opt in enumerate(options, 1):
        console.info(f"  {i}) {opt}")
    while True:
        answer = input(f"number [1-{len(options)}]: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
        if answer in options:
            return answer


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
    _warn_deprecated(args)
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
