"""`ym adopt` (design §9): bring an existing, unmanaged YOURLS install under management.

Works inside a site repo created by ``ym new``. The install is snapshotted (a local
directory, or an rsync pull of a target's web root), compared with pristine upstream core
at the installed version, and every site-specific difference is carried into ``overlay/``
(or ``adopt-review/`` when it collides with an existing overlay file) so that the first
``ym deploy`` reproduces what runs today.
"""

from __future__ import annotations

import contextlib
import importlib.resources
import json
import re
import shutil
import tempfile
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from . import gitsrc
from .config import (
    OVERLAY_DIR,
    PLUGIN_NAME_RE,
    SiteConfig,
    TargetSpec,
    join_remote,
    load_site,
)
from .core import read_yourls_version
from .util import UsageError, YMError, console, iso_utc, run
from .vendor import (
    copy_tree,
    file_sha256,
    glob_match,
    iter_files,
    make_exclude,
    rmtree,
    tree_sha256,
)

DEFAULT_IGNORES: tuple[str, ...] = (
    ".well-known/**",
    "**/error_log",
    "**/*.log",
    "**/.DS_Store",
    "**/Thumbs.db",
    ".git/**",
)
CONFIG_FILES = ("user/config.php", "includes/config.php")
PLUGINS_REL = "user/plugins"
REVIEW_DIR = "adopt-review"
REPORT_FILE = "REPORT.md"

_PLUGIN_URI_RE = re.compile(r"^[ \t/*#]*Plugin URI:\s*(\S.*?)\s*$", re.MULTILINE | re.IGNORECASE)
_TABLE_HEADER_RE = re.compile(r"^\s*\[\[?\s*([^\[\]]+?)\s*\]\]?\s*(#.*)?$")


# --------------------------------------------------------------------------- data


@dataclass
class Snapshot:
    root: Path
    source: str
    active_plugins: list[str] | None = None  # None: unknown
    helper_version: str | None = None
    cleanup: Path | None = None
    extra_ignores: tuple[str, ...] = ()


@dataclass
class PluginDecision:
    name: str
    kind: str  # bundled | custom | managed | overlay | invalid
    active: bool | None  # None: unknown
    action: str  # none | overlay | review | compare
    add_entry: bool = False
    uri: str | None = None
    note: str = ""


@dataclass
class Plan:
    version: str
    tag: str
    commit: str
    overlay_files: list[str] = field(default_factory=list)
    review_files: list[str] = field(default_factory=list)
    already_in_overlay: list[str] = field(default_factory=list)
    unchanged: int = 0
    missing: list[str] = field(default_factory=list)
    missing_excluded: list[str] = field(default_factory=list)
    plugins: list[PluginDecision] = field(default_factory=list)
    toml_not_in_snapshot: list[str] = field(default_factory=list)
    active_without_dir: list[str] = field(default_factory=list)


@dataclass
class AdoptResult:
    plan: Plan
    parity: Parity | None = None
    report: Path | None = None
    toml_added: list[str] = field(default_factory=list)


@dataclass
class Parity:
    add: dict[str, str] = field(default_factory=dict)  # path -> category
    change: dict[str, str] = field(default_factory=dict)
    delete: dict[str, str] = field(default_factory=dict)

    def unexpected(self) -> dict[str, list[str]]:
        return {
            kind: sorted(p for p, cat in getattr(self, kind).items() if cat == "other")
            for kind in ("add", "change", "delete")
        }

    @property
    def unexpected_count(self) -> int:
        return sum(len(v) for v in self.unexpected().values())


# --------------------------------------------------------------------------- matching


class Ignore:
    def __init__(self, patterns: Iterable[str]):
        self.patterns = tuple(dict.fromkeys(p.strip() for p in patterns if p.strip()))

    def __call__(self, relpath: str) -> bool:
        return any(glob_match(p, relpath) for p in self.patterns)

    def under(self, prefix: str):
        return lambda rel: self(f"{prefix}/{rel}")


def rsync_excludes(patterns: Iterable[str]) -> list[str]:
    """Translate anchored ``glob_match`` patterns to rsync ``--exclude`` patterns.

    Only an optimization for the pull; the Python-side filter remains authoritative.
    """
    out = []
    for raw in patterns:
        p = raw.strip().strip("/")
        if not p:
            continue
        while p.endswith("/**"):
            p = p[:-3]
        if p.startswith("**/"):
            rest = p[3:]
            out.append(rest if "/" not in rest else p)
        else:
            out.append("/" + p)
    return out


def plugin_header_uri(plugin_php: Path) -> str | None:
    try:
        head = plugin_php.read_text(encoding="utf-8", errors="replace")[:8192]
    except OSError:
        return None
    m = _PLUGIN_URI_RE.search(head)
    return m[1] if m else None


def _plugin_dirs(root: Path, ignore: Ignore) -> list[str]:
    base = root / PLUGINS_REL
    if not base.is_dir():
        return []
    names = []
    for entry in sorted(base.iterdir(), key=lambda p: p.name):
        rel = f"{PLUGINS_REL}/{entry.name}"
        is_plugin = entry.is_dir() and not entry.is_symlink() and (entry / "plugin.php").is_file()
        if is_plugin and not ignore(rel):
            names.append(entry.name)
    return names


def _same_tree(a: Path, b: Path, skip) -> bool:
    return a.is_dir() and b.is_dir() and tree_sha256(a, skip) == tree_sha256(b, skip)


def _same_file(a: Path, b: Path) -> bool:
    return (
        a.is_file()
        and b.is_file()
        and a.stat().st_size == b.stat().st_size
        and file_sha256(a) == file_sha256(b)
    )


# --------------------------------------------------------------------------- snapshot


def _helper_info_local(root: Path) -> dict | None:
    php = shutil.which("php")
    if php is None:
        console.warn("php not found locally; cannot read the active plugin set")
        return None
    from .serverconfig import is_shim

    cfg_file = next((root / c for c in CONFIG_FILES if (root / c).is_file()), None)
    if cfg_file is None:
        console.warn(f"no user/config.php in {root}; cannot read the active plugin set")
        return None
    if is_shim(cfg_file.read_text(encoding="utf-8", errors="replace")):
        console.warn(f"{cfg_file} is the yourls-manager shim; cannot read the active plugin set")
        return None
    from .deploy.rsync import helper_source

    with importlib.resources.as_file(helper_source()) as helper:
        proc = run([php, str(helper), "info", f"--root={root}"], check=False)
    return _parse_helper(
        proc.stdout.decode(errors="replace"), proc.stderr.decode("utf-8", "replace")
    )


def _parse_helper(out: str, err: str) -> dict | None:
    try:
        data = json.loads(out.strip().splitlines()[-1]) if out.strip() else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict) or not data.get("ok"):
        reason = (data or {}).get("error") if isinstance(data, dict) else (err or out)[-500:]
        console.warn(f"ym-helper info failed: {reason}")
        return None
    return data


def _apply_info(snap: Snapshot, info: dict | None) -> None:
    if info is None:
        return
    if not info.get("installed"):
        console.warn("the install's database reports YOURLS is not installed")
        return
    snap.active_plugins = [str(p) for p in info.get("active_plugins") or []]
    snap.helper_version = info.get("yourls_version")


def snapshot_dir(path: Path, *, use_helper: bool = True) -> Snapshot:
    root = path.expanduser().resolve()
    if not (root / "includes" / "version.php").is_file():
        raise UsageError(f"{root} is not a YOURLS install (includes/version.php missing)")
    snap = Snapshot(root=root, source=str(root))
    if use_helper:
        _apply_info(snap, _helper_info_local(root))
    return snap


def snapshot_target(cfg: SiteConfig, name: str, ignore: Ignore, *, dry_run: bool) -> Snapshot:
    from .deploy import get_target
    from .deploy.rsync import RsyncTarget

    target = get_target(cfg, name)
    if not isinstance(target, RsyncTarget):
        raise UsageError(f"target {name}: adopt supports rsync targets only")
    runner = target.runner
    target.preflight(need_config=False)
    web_root = target.web_root
    if not runner.exists(join_remote(web_root, "includes/version.php"), "-f"):
        raise UsageError(
            f"{runner.describe()}:{web_root} is not a YOURLS install (includes/version.php missing)"
        )
    tmp = Path(tempfile.mkdtemp(prefix="ym-adopt-"))
    snap_root = tmp / "snapshot"
    snap_root.mkdir()
    snap = Snapshot(root=snap_root, source=f"{runner.describe()}:{web_root}", cleanup=tmp)
    try:
        console.step(f"snapshot {snap.source} (read-only pull)")
        extra = ["-rlt", "--safe-links", "--chmod=u+rwX"]
        extra += [f"--exclude={x}" for x in rsync_excludes(ignore.patterns)]
        target.rsync(runner.rsync_dest(web_root) + "/", f"{snap_root}/", extra)
        n = sum(1 for _ in iter_files(snap_root))
        console.info(f"  {n} files")
        if dry_run:
            console.info("  dry run: not uploading the helper; active plugins are not read")
            return snap
        target.upload_helper()
        if runner.exists(target.config_file, "-f"):
            env = {"YOURLS_CONFIG_FILE": target.config_file}
        else:
            env = {"YOURLS_CONFIG_FILE": join_remote(web_root, "user/config.php")}
        try:
            info = target.helper("info", f"--root={web_root}", env=env)
        except YMError as exc:
            console.warn(f"ym-helper info failed on {runner.describe()}: {exc}")
            info = None
        _apply_info(snap, info)
    except BaseException:
        rmtree(tmp)
        raise
    return snap


# --------------------------------------------------------------------------- version


def _nearby_tags(tags: Iterable[str], version: str, n: int = 8) -> list[str]:
    want = gitsrc.parse_stable_semver(version.removeprefix("v")) or (0, 0, 0)

    def dist(t: str) -> tuple[int, ...]:
        v = gitsrc.parse_stable_semver(t) or (999, 999, 999)
        return tuple(abs(a - b) for a, b in zip(v, want, strict=True))

    stable = [t for t in tags if gitsrc.parse_stable_semver(t)]
    return sorted(stable, key=dist)[:n]


def resolve_installed_tag(repo: str, version: str) -> tuple[str, str]:
    try:
        return gitsrc.resolve_core_tag(repo, version)
    except YMError:
        tags = gitsrc.remote_tags(repo)
        near = _nearby_tags(tags, version)
        raise YMError(
            f"installed YOURLS version {version!r} has no matching tag at {repo}"
            + (f"; nearby tags: {', '.join(near)}" if near else "")
            + ". Set [core].repo to the fork it came from, or adopt from a matching install."
        ) from None


# --------------------------------------------------------------------------- analysis


def analyze(
    cfg: SiteConfig,
    snap: Snapshot,
    pristine: Path,
    ignore: Ignore,
    version: str,
    tag: str,
    commit: str,
) -> Plan:
    plan = Plan(version=version, tag=tag, commit=commit)
    plugin_dirs = _plugin_dirs(snap.root, ignore)
    upstream_plugins = _plugin_dirs(pristine, Ignore(()))
    plugin_prefixes = tuple(f"{PLUGINS_REL}/{n}/" for n in plugin_dirs)

    def skip_snap(rel: str) -> bool:
        return rel in CONFIG_FILES or ignore(rel) or (rel + "/").startswith(plugin_prefixes)

    up_prefixes = tuple(f"{PLUGINS_REL}/{n}/" for n in upstream_plugins)

    def skip_up(rel: str) -> bool:
        return rel in CONFIG_FILES or ignore(rel) or (rel + "/").startswith(up_prefixes)

    overlay = cfg.overlay_dir
    seen: set[str] = set()
    for rel, path in iter_files(snap.root, skip_snap):
        seen.add(rel)
        up = pristine / rel
        if _same_file(path, up):
            plan.unchanged += 1
            continue
        existing = overlay / rel
        if existing.exists():
            if _same_file(path, existing):
                plan.already_in_overlay.append(rel)
            else:
                plan.review_files.append(rel)
        else:
            plan.overlay_files.append(rel)

    excluded = make_exclude(cfg.core.exclude)
    for rel, _ in iter_files(pristine, skip_up):
        if rel not in seen:
            (plan.missing_excluded if excluded(rel) else plan.missing).append(rel)

    # ---- plugins
    active = set(snap.active_plugins) if snap.active_plugins is not None else None
    for name in plugin_dirs:
        sdir = snap.root / PLUGINS_REL / name
        skip = ignore.under(f"{PLUGINS_REL}/{name}")
        is_active = None if active is None else name in active
        uri = plugin_header_uri(sdir / "plugin.php")
        spec = cfg.plugin(name)
        odir = overlay / PLUGINS_REL / name
        bundled = name in upstream_plugins and _same_tree(sdir, pristine / PLUGINS_REL / name, skip)
        if not PLUGIN_NAME_RE.match(name):
            d = PluginDecision(
                name,
                "invalid",
                is_active,
                "review",
                uri=uri,
                note="directory name not usable in site.toml",
            )
        elif spec is not None and spec.managed:
            d = PluginDecision(
                name,
                "managed",
                is_active,
                "compare",
                uri=uri,
                note=f"site.toml vendors it from {spec.repo}",
            )
        elif odir.exists():
            same = _same_tree(sdir, odir, skip)
            d = PluginDecision(
                name,
                "overlay",
                is_active,
                "none" if same else "review",
                uri=uri,
                add_entry=spec is None,
                note="already in overlay/" + ("" if same else "; server copy differs"),
            )
        elif bundled:
            d = PluginDecision(
                name,
                "bundled",
                is_active,
                "none",
                uri=uri,
                add_entry=spec is None and is_active is True,
            )
            if is_active is None:
                d.note = "ships with core; active state unknown"
        else:
            d = PluginDecision(
                name, "custom", is_active, "overlay", uri=uri, add_entry=spec is None
            )
        if spec is not None and is_active is not None and spec.active != is_active:
            d.note = (d.note + "; " if d.note else "") + (
                f"site.toml says active = {str(spec.active).lower()}, the install has it"
                f" {'active' if is_active else 'inactive'}"
            )
        plan.plugins.append(d)

    present = set(plugin_dirs)
    overlay_plugins = set(_plugin_dirs(overlay, Ignore(()))) if overlay.is_dir() else set()
    plan.toml_not_in_snapshot = [
        p.name for p in cfg.plugins if p.name not in present and p.name not in overlay_plugins
    ]
    if active is not None:
        plan.active_without_dir = sorted(a for a in active if a not in present)
    return plan


# --------------------------------------------------------------------------- site.toml


def plugin_block(d: PluginDecision) -> str:
    lines = []
    if d.kind == "bundled":
        lines.append("# Adopted by `ym adopt`: ships with YOURLS core.")
    else:
        lines.append(f"# Adopted by `ym adopt`: site-owned copy in overlay/user/plugins/{d.name}/.")
        if d.uri:
            lines.append(f"# Plugin URI: {d.uri}")
            lines.append("#   (to get updates, switch to `repo = ...` and remove the overlay copy)")
    lines.append("[[plugins]]")
    lines.append(f'name   = "{d.name}"')
    if d.active is None:
        lines.append("active = true   # TODO verify: active state could not be read")
    else:
        lines.append(f"active = {'true' if d.active else 'false'}")
    return "\n".join(lines) + "\n"


def append_plugin_blocks(site_file: Path, blocks: list[str]) -> None:
    """Insert ``[[plugins]]`` blocks after the existing plugin entries, keeping comments."""
    if not blocks:
        return
    text = site_file.read_text(encoding="utf-8")
    before = tomllib.loads(text)
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    last_plugins = None
    for i, line in enumerate(lines):
        m = _TABLE_HEADER_RE.match(line)
        if m and line.lstrip().startswith("[[") and m[1] == "plugins":
            last_plugins = i
    insert_at = len(lines)
    if last_plugins is not None:
        for j in range(last_plugins + 1, len(lines)):
            m = _TABLE_HEADER_RE.match(lines[j])
            if m:
                k = j
                while k > last_plugins + 1 and lines[k - 1].lstrip().startswith("#"):
                    k -= 1
                insert_at = k
                break
    prev_blank = insert_at == 0 or lines[insert_at - 1].strip() == ""
    chunk = ("" if prev_blank else "\n") + "\n".join(blocks)
    if insert_at < len(lines):
        chunk += "\n"  # blank line before the following table
    new_text = "".join(lines[:insert_at]) + chunk + "".join(lines[insert_at:])
    after = tomllib.loads(new_text)
    want = len(before.get("plugins", [])) + len(blocks)
    if len(after.get("plugins", [])) != want:  # pragma: no cover - defensive
        raise YMError("internal error: could not add [[plugins]] entries to site.toml")
    site_file.write_text(new_text, encoding="utf-8")


# --------------------------------------------------------------------------- parity


def compare_parity(
    dist: Path,
    snap: Path,
    ignore: Ignore,
    template_owned: set[str],
    pristine: Path,
    core_excluded,
) -> Parity:
    def skip(rel: str) -> bool:
        return rel in CONFIG_FILES or ignore(rel)

    a = dict(iter_files(dist, skip))
    b = dict(iter_files(snap, skip))
    par = Parity()

    def category(rel: str, kind: str) -> str:
        if rel in template_owned:
            return "template"
        up = (pristine / rel).is_file()
        if kind == "delete" and up and core_excluded(rel):
            return "excluded"
        if kind == "add" and up:
            return "restored"
        return "other"

    for rel in sorted(a.keys() - b.keys()):
        par.add[rel] = category(rel, "add")
    for rel in sorted(b.keys() - a.keys()):
        par.delete[rel] = category(rel, "delete")
    for rel in sorted(a.keys() & b.keys()):
        if not _same_file(a[rel], b[rel]):
            par.change[rel] = category(rel, "change")
    return par


# --------------------------------------------------------------------------- report


def _bullets(items: Iterable[str], empty: str = "(none)") -> str:
    items = list(items)
    return "\n".join(f"- `{i}`" for i in items) if items else f"_{empty}_"


def write_report(
    cfg: SiteConfig,
    snap: Snapshot,
    plan: Plan,
    parity: Parity,
    target: str | None,
    toml_added: list[str],
) -> Path:
    review = cfg.root / REVIEW_DIR
    review.mkdir(exist_ok=True)
    tname = target or "TARGET"
    plug_rows = []
    for d in plan.plugins:
        act = "unknown" if d.active is None else ("yes" if d.active else "no")
        where = {
            "bundled": "core (vendored)",
            "custom": f"overlay/user/plugins/{d.name}/",
            "managed": "vendored by `ym sync`",
            "overlay": f"overlay/user/plugins/{d.name}/",
            "invalid": f"{REVIEW_DIR}/user/plugins/{d.name}/",
        }[d.kind]
        if d.action == "review":
            where += f" (server copy in {REVIEW_DIR}/)"
        plug_rows.append(f"| `{d.name}` | {d.kind} | {act} | {where} | {d.uri or ''} | {d.note} |")

    def par_lines(kind: str) -> str:
        entries = getattr(parity, kind)
        if not entries:
            return "_(none)_"
        return "\n".join(f"- `{p}` ({cat})" for p, cat in entries.items())

    unexpected = parity.unexpected_count
    active_read = (
        "yes"
        if snap.active_plugins is not None
        else "**no** (entries marked `# TODO verify` in site.toml)"
    )
    review_items = plan.review_files + [
        f"user/plugins/{d.name}/" for d in plan.plugins if d.action == "review"
    ]

    def names(items: Iterable[str]) -> str:
        return ", ".join(f"`{n}`" for n in items) or "_(none)_"

    text = f"""# Adoption report

Generated by `ym adopt` (yourls-manager) on {iso_utc()}.

- Source: `{snap.source}`
- YOURLS version: **{plan.version}** (core tag `{plan.tag}` @ `{plan.commit[:10]}`)
- Active plugins read from the install's database: {active_read}

## Overlay files added

Files that differ from (or are not in) pristine YOURLS {plan.tag}, copied into `overlay/`:

{_bullets(plan.overlay_files)}

## Files needing a manual merge

These collide with files already in `overlay/` (template-owned: `.htaccess`, `health.php`,
the site-branding plugin, ...). The server's version is in `{REVIEW_DIR}/<same path>`; merge
what you need into `overlay/` by hand, then delete `{REVIEW_DIR}/`.

{_bullets(review_items)}

Already identical in overlay: {len(plan.already_in_overlay)}.
Unchanged core files: {plan.unchanged}.

## Upstream files missing from the install

Deploy will add these (they are part of YOURLS {plan.tag}):

{_bullets(plan.missing)}

Not deployed anyway (matched by `[core].exclude`): {len(plan.missing_excluded)} file(s).

## Plugins

| Plugin | Kind | Active | Comes from | Plugin URI | Notes |
|---|---|---|---|---|---|
{chr(10).join(plug_rows) if plug_rows else "| _(none)_ | | | | | |"}

Added to site.toml: {names(toml_added)}

In site.toml but not on the install: {names(plan.toml_not_in_snapshot)}

Active in the database but missing on disk: {names(plan.active_without_dir)}

`ym deploy` makes the active plugin set exactly the `active = true` entries in site.toml.

## Parity: what a deploy would change

Compared `dist/` (from `ym build`) with the install, ignoring config files and
{", ".join(f"`{p}`" for p in (*DEFAULT_IGNORES, *snap.extra_ignores))}.
Categories: *template* = template-owned overlay files (expected to differ), *excluded* =
core files dropped by `[core].exclude`, *restored* = core files missing from the install,
*other* = unexpected (should be zero).

- add: {len(parity.add)}, change: {len(parity.change)}, delete: {len(parity.delete)}
- unexpected (*other*): **{unexpected}**

### add
{par_lines("add")}

### change
{par_lines("change")}

### delete
{par_lines("delete")}

## Next steps

1. Review `{REVIEW_DIR}/` and merge anything needed into `overlay/` (then delete it).
2. Check the `[[plugins]]` entries added to site.toml (and any `# TODO verify`).
3. Commit: `git add -A && git commit -m "Adopt existing install"`.
4. If the target has no server-local config yet: `ym server-config {tname} --from-existing`.
5. `ym deploy {tname} --dry-run`, then `ym deploy {tname}`.
6. Update: `ym core --version latest`, commit, `ym deploy {tname}`.

`{REVIEW_DIR}/` is never deployed (it is outside `overlay/`); commit or delete it.
"""
    path = review / REPORT_FILE
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- command


def adopt(
    cfg: SiteConfig,
    *,
    from_target: str | None = None,
    from_dir: Path | None = None,
    ignore: Iterable[str] = (),
    dry_run: bool = False,
    use_helper: bool = True,
) -> AdoptResult:
    if (from_target is None) == (from_dir is None):
        raise UsageError("give exactly one of --from-target TARGET or --from-dir PATH")
    patterns = [*DEFAULT_IGNORES, *ignore]
    spec: TargetSpec | None = None
    if from_target is not None:
        spec = cfg.target(from_target)
        patterns += list(spec.protect)
    ign = Ignore(patterns)

    if from_target is not None:
        snap = snapshot_target(cfg, from_target, ign, dry_run=dry_run)
    else:
        assert from_dir is not None
        snap = snapshot_dir(from_dir, use_helper=use_helper)
        if snap.root == cfg.root or cfg.root.is_relative_to(snap.root):
            raise UsageError("--from-dir must not contain the site repo")
    snap.extra_ignores = tuple(p for p in ign.patterns if p not in DEFAULT_IGNORES)

    pristine_tmp = gitsrc.temp_checkout_dir()
    try:
        version = read_yourls_version(snap.root)
        if version is None:
            raise UsageError(f"{snap.source}: no YOURLS_VERSION in includes/version.php")
        console.step(f"installed YOURLS {version}")
        if snap.helper_version and snap.helper_version != version:
            console.warn(f"helper reports YOURLS {snap.helper_version}, version.php says {version}")
        tag, commit = resolve_installed_tag(cfg.core.repo, version)
        console.info(f"  core tag {tag} @ {commit[:10]}")
        pristine = gitsrc.fetch_commit(cfg.core.repo, commit, pristine_tmp / "core").path

        plan = analyze(cfg, snap, pristine, ign, version, tag, commit)
        _print_plan(plan, snap)
        if dry_run:
            console.info("dry run: nothing written")
            return AdoptResult(plan)

        template_owned = (
            {rel for rel, _ in iter_files(cfg.overlay_dir)} if (cfg.overlay_dir.is_dir()) else set()
        )
        template_owned -= set(plan.already_in_overlay)

        # ---- vendor core at the installed version (also rewrites site.toml)
        from .core import update_core
        from .plugins import sync_plugins

        console.step(f"ym core --version {tag}")
        update_core(cfg, tag)
        cfg = load_site(cfg.root)
        console.step("ym sync")
        sync_plugins(cfg)

        # ---- copy files
        review = cfg.root / REVIEW_DIR
        for rel in plan.overlay_files:
            _copy_file(snap.root / rel, cfg.overlay_dir / rel)
        for rel in plan.review_files:
            _copy_file(snap.root / rel, review / rel)
        toml_added: list[str] = []
        blocks: list[str] = []
        for d in plan.plugins:
            sdir = snap.root / PLUGINS_REL / d.name
            skip = ign.under(f"{PLUGINS_REL}/{d.name}")
            if d.action == "compare":
                vdir = cfg.vendor_dir / PLUGINS_REL / d.name
                if _same_tree(sdir, vdir, skip):
                    d.action = "none"
                else:
                    d.action = "review"
                    d.note += "; server copy differs from the vendored one"
            if d.action == "overlay":
                copy_tree(sdir, cfg.overlay_dir / PLUGINS_REL / d.name, skip)
            elif d.action == "review":
                dest = review / PLUGINS_REL / d.name
                if dest.exists():
                    rmtree(dest)
                copy_tree(sdir, dest, skip)
            if d.add_entry:
                blocks.append(plugin_block(d))
                toml_added.append(d.name)
        append_plugin_blocks(cfg.site_file, blocks)
        cfg = load_site(cfg.root)

        # ---- parity
        from .build import build_site

        console.step("parity check (ym build + compare)")
        dist = build_site(cfg)
        parity = compare_parity(
            dist, snap.root, ign, template_owned, pristine, make_exclude(cfg.core.exclude)
        )
        _print_parity(parity)
        report = write_report(cfg, snap, plan, parity, from_target, toml_added)
    finally:
        rmtree(pristine_tmp)
        if snap.cleanup is not None:
            rmtree(snap.cleanup)

    _warn_plugins(plan, snap)
    console.info("")
    console.info(f"Report: {report.relative_to(cfg.root)}")
    tname = from_target or "TARGET"
    console.info("Next steps:")
    console.info(f"  review {REVIEW_DIR}/ (merge into overlay/ by hand), then commit")
    console.info(
        f"  ym server-config {tname} --from-existing   # if {tname} has no server-local config yet"
    )
    console.info(f"  ym deploy {tname} --dry-run")
    console.info(f"  ym deploy {tname}")
    console.info(f"  ym core --version latest && ym deploy {tname}   # then actually update")
    return AdoptResult(plan, parity, report, toml_added)


def _copy_file(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        dest.unlink()
    shutil.copy2(src, dest)


def _print_plan(plan: Plan, snap: Snapshot) -> None:
    console.step("plan")
    console.info(f"  core files unchanged: {plan.unchanged}")
    console.info(f"  -> overlay/: {len(plan.overlay_files)} file(s)")
    for rel in plan.overlay_files:
        console.info(f"       {rel}")
    console.info(f"  -> {REVIEW_DIR}/ (manual merge): {len(plan.review_files)} file(s)")
    for rel in plan.review_files:
        console.info(f"       {rel}")
    if plan.missing:
        console.info(f"  upstream files missing from the install: {len(plan.missing)}")
        for rel in plan.missing:
            console.detail(rel)
    console.info("  plugins:")
    for d in plan.plugins:
        act = "?" if d.active is None else ("active" if d.active else "inactive")
        dest = {"overlay": "-> overlay/", "review": f"-> {REVIEW_DIR}/", "compare": "", "none": ""}[
            d.action
        ]
        extra = f" [{d.note}]" if d.note else ""
        entry = " +site.toml" if d.add_entry else ""
        console.info(f"    {d.name}: {d.kind}, {act} {dest}{entry}{extra}".rstrip())


def _print_parity(par: Parity) -> None:
    def count(kind: str) -> str:
        entries = getattr(par, kind)
        other = sum(1 for c in entries.values() if c == "other")
        return f"{len(entries)} ({other} unexpected)"

    console.info(
        f"  a deploy would add {count('add')}, change {count('change')}, delete {count('delete')}"
    )
    for kind in ("add", "change", "delete"):
        for rel, cat in getattr(par, kind).items():
            console.detail(f"{kind:6} {rel} ({cat})")
    unexpected = par.unexpected()
    if par.unexpected_count:
        listing = "\n  ".join(f"{k} {p}" for k, ps in unexpected.items() for p in ps)
        console.warn(f"unexpected differences between the build and the install:\n  {listing}")


def _warn_plugins(plan: Plan, snap: Snapshot) -> None:
    if snap.active_plugins is None:
        added = [d.name for d in plan.plugins if d.add_entry]
        bundled = [d.name for d in plan.plugins if d.kind == "bundled"]
        console.warn(
            "could not read the active plugin set from the install's database.\n"
            + (
                f"  Adopted plugins written as `active = true  # TODO verify`: {', '.join(added)}\n"
                if added
                else ""
            )
            + (
                f"  Bundled plugins present (add [[plugins]] entries for the active ones):"
                f" {', '.join(bundled)}\n"
                if bundled
                else ""
            )
            + "  `ym deploy` makes the active set exactly site.toml's: fix site.toml first!"
        )
    if plan.toml_not_in_snapshot:
        console.warn(
            "in site.toml but not on the install (deploy will add/activate them): "
            + ", ".join(plan.toml_not_in_snapshot)
        )
    if plan.active_without_dir:
        console.warn(
            "active in the database but missing on disk: " + ", ".join(plan.active_without_dir)
        )
    for d in plan.plugins:
        if "site.toml says active" in d.note:
            console.warn(f"plugin {d.name}: {d.note}")


__all__ = ["DEFAULT_IGNORES", "OVERLAY_DIR", "adopt", "rsync_excludes"]
