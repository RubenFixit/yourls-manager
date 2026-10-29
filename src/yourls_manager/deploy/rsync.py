"""``type = "rsync"`` deploy target (design §5)."""

from __future__ import annotations

import importlib.resources
import json
import re
import shutil
from pathlib import Path
from typing import Any

from .. import __version__
from ..build import build_site
from ..config import REDIRECTS_FILE, SiteConfig, TargetSpec, join_remote
from ..remote import Runner, runner_for
from ..util import UsageError, YMError, compact_utc, console, run
from ..verify import verify
from .base import DeployOptions, Target, release_info

MIN_PHP = (8, 1)
STATE_DIR = ".yourls-manager"


def helper_source():
    """Traversable for the packaged PHP helper."""
    return importlib.resources.files("yourls_manager") / "php" / "ym-helper.php"


class RsyncTarget(Target):
    def __init__(self, cfg: SiteConfig, spec: TargetSpec, runner: Runner | None = None):
        super().__init__(cfg, spec)
        self.runner = runner or runner_for(spec)
        self._preflighted = False

    # ------------------------------------------------------------------ paths
    @property
    def web_root(self) -> str:
        return self.runner.abspath(self.spec.path)

    @property
    def config_file(self) -> str:
        return self.runner.abspath(self.spec.config_file)

    @property
    def backup_dir(self) -> str:
        return self.runner.abspath(self.spec.backup_dir)

    @property
    def helper_path(self) -> str:
        return self.runner.abspath(join_remote(STATE_DIR, f"ym-helper-{__version__}.php"))

    @property
    def release_file(self) -> str:
        return self.runner.abspath(join_remote(STATE_DIR, self.cfg.site.slug, "release.json"))

    @property
    def helper_env(self) -> dict[str, str]:
        return {"YOURLS_CONFIG_FILE": self.config_file}

    # ------------------------------------------------------------------ steps
    def preflight(self, *, need_rsync: bool = True, need_config: bool = True) -> None:
        if self._preflighted:
            return
        r = self.runner
        console.step(f"preflight ({r.describe()})")
        if need_rsync and shutil.which("rsync") is None:
            raise UsageError("rsync not found on this machine; deploy from Linux, macOS, WSL or CI")
        if not r.is_local:
            r.run(["true"])  # reachable + key auth works (BatchMode)
            if need_rsync:
                r.run(["rsync", "--version"])
        console.detail(f"home: {r.home()}")
        ver = self.php_version()
        if ver < MIN_PHP:
            raise YMError(
                f"{self.spec.php}: PHP {'.'.join(map(str, ver))} is too old; need >= 8.1"
                " (set [deploy.targets.<name>].php, e.g. /usr/local/php83/bin/php)"
            )
        console.info(f"  php {'.'.join(map(str, ver))} ({self.spec.php})")
        if not need_config:
            return
        if not r.exists(self.config_file, "-f"):
            raise UsageError(
                f"server-local config not found: {self.config_file} on {r.describe()}\n"
                f"Create it with `ym target config {self.spec.name}` (or"
                f" `ym target config {self.spec.name} --from-existing` when adopting)."
            )
        console.info(f"  config {self.config_file}")
        self._preflighted = True

    def php_version(self) -> tuple[int, ...]:
        out = self.runner.run([self.spec.php, "-r", "echo PHP_VERSION;"]).stdout.decode()
        m = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", out.strip())
        if not m:
            raise YMError(f"could not parse PHP version from {out!r}")
        return tuple(int(x) for x in m.groups(default="0"))

    def rsync(self, src: str, dest: str, extra: list[str]) -> str:
        argv = ["rsync", *self.runner.rsync_transport(), *extra, str(src), dest]
        return run(argv).stdout.decode(errors="replace")

    def upload_helper(self) -> None:
        console.step("upload helper")
        self.runner.mkdir(self.runner.abspath(STATE_DIR))
        with importlib.resources.as_file(helper_source()) as local:
            self.rsync(
                str(local),
                self.runner.rsync_dest(self.helper_path),
                ["-tp", "--chmod=F600"],
            )
        console.detail(self.helper_path)

    def helper(self, command: str, *args: str, env: dict[str, str] | None = None) -> dict[str, Any]:
        argv = [self.spec.php, self.helper_path, command, *args]
        proc = self.runner.run(argv, env=self.helper_env if env is None else env, check=False)
        stderr = proc.stderr.decode(errors="replace").strip()
        for line in stderr.splitlines():
            console.detail(f"helper: {line}")
        out = proc.stdout.decode(errors="replace").strip()
        try:
            payload = json.loads(out.splitlines()[-1]) if out else None
        except json.JSONDecodeError:
            payload = None
        if not isinstance(payload, dict):
            raise YMError(
                f"ym-helper {command}: no JSON result (exit {proc.returncode})\n"
                f"{(out or stderr)[-2000:]}"
            )
        if proc.returncode != 0 or payload.get("ok") is False:
            raise YMError(f"ym-helper {command}: {payload.get('error') or 'failed'}")
        return payload

    def backup(self) -> str | None:
        console.step("backup database")
        name = f"{self.cfg.site.slug}-{compact_utc()}.sql.gz"
        out = join_remote(self.backup_dir, name)
        res = self.helper("dump", f"--config={self.config_file}", f"--out={out}")
        if res.get("skipped"):
            console.info(f"  skipped: {res.get('reason', 'nothing to dump')}")
            return None
        console.info(f"  {out} ({res.get('bytes', '?')} bytes)")
        self.prune_backups()
        return out

    def prune_backups(self) -> list[str]:
        proc = self.runner.run(["ls", "-1", "--", self.backup_dir], check=False)
        pat = re.compile(rf"^{re.escape(self.cfg.site.slug)}-\d{{8}}T\d{{6}}Z\.sql\.gz$")
        names = sorted(n for n in proc.stdout.decode().splitlines() if pat.match(n))
        stale = names[: max(0, len(names) - self.spec.backup_keep)]
        if stale:
            self.runner.run(["rm", "-f", "--", *(join_remote(self.backup_dir, n) for n in stale)])
            console.info(f"  pruned {len(stale)} old dump(s), keeping {self.spec.backup_keep}")
        return stale

    def sync_files(self, dist: Path, *, dry_run: bool) -> str:
        console.step(
            f"rsync -> {self.runner.describe()}:{self.web_root}" + (" (dry run)" if dry_run else "")
        )
        if not dry_run:
            self.runner.mkdir(self.web_root)
        extra = ["-az", "--delete", "--delay-updates"]
        extra += [f"--filter=P {p}" for p in self.spec.protect]
        if dry_run or console.verbose:
            extra += ["--dry-run", "-i"] if dry_run else ["-i"]
        output = self.rsync(f"{dist}/", self.runner.rsync_dest(self.web_root) + "/", extra)
        if output.strip() and (dry_run or console.verbose):
            console.info(output.rstrip())
        return output

    def info(self) -> dict[str, Any]:
        return self.helper("info", f"--root={self.web_root}")

    def upgrade(self) -> dict[str, Any]:
        console.step("database upgrade")
        res = self.helper("upgrade", f"--root={self.web_root}")
        if res.get("upgraded"):
            console.info(f"  upgraded db_version {res.get('from')} -> {res.get('to')}")
        else:
            console.info("  not needed")
        return res

    def plugins(self) -> dict[str, Any]:
        console.step("plugins")
        active = ",".join(self.cfg.active_plugins)
        res = self.helper("plugins", f"--root={self.web_root}", f"--active={active}")
        for key in ("activated", "deactivated"):
            if res.get(key):
                console.info(f"  {key}: {', '.join(res[key])}")
        console.info(f"  active: {', '.join(res.get('active', [])) or '(none)'}")
        return res

    def record(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        console.step("record release")
        release = {**release_info(self.cfg, self.spec), **(extra or {})}
        self.runner.mkdir(self.runner.abspath(join_remote(STATE_DIR, self.cfg.site.slug)))
        data = (json.dumps(release, indent=2, sort_keys=True) + "\n").encode()
        self.runner.write_file(self.release_file, data, "0600")
        console.detail(self.release_file)
        return release

    # ------------------------------------------------------------------ flows
    def deploy(self, opts: DeployOptions) -> bool:
        if opts.no_build:
            dist = self.cfg.out_dir
            if not dist.is_dir():
                raise UsageError(f"--no-build given but {dist} does not exist")
        else:
            console.step("build")
            dist = build_site(self.cfg)
        self.preflight()
        if opts.dry_run:
            self.sync_files(dist, dry_run=True)
            console.info("dry run: stopped before making changes")
            return True
        self.upload_helper()
        backup = None
        if opts.skip_backup:
            console.step("backup database (skipped: --skip-backup)")
        else:
            backup = self.backup()
        self.sync_files(dist, dry_run=False)
        state = self.info()
        if not state.get("installed"):
            console.warn(
                "YOURLS is not installed in this database yet: open "
                f"{self.spec.url}/admin/install.php to install, then run "
                f"`ym plugin apply {self.spec.name}`. Skipping DB upgrade and plugins."
            )
            plugin_res: dict[str, Any] = {}
        else:
            self.upgrade()
            plugin_res = self.plugins()
        self.record({"backup": backup, "active_plugins": plugin_res.get("active")})
        if opts.skip_verify:
            console.step("verify (skipped: --skip-verify)")
            return True
        return verify(self.spec.url, self.cfg.root / REDIRECTS_FILE)

    def apply_plugins(self) -> dict[str, Any]:
        self.preflight()
        self.upload_helper()
        state = self.info()
        if not state.get("installed"):
            raise YMError(
                f"YOURLS is not installed yet; open {self.spec.url}/admin/install.php first"
            )
        return self.plugins()
