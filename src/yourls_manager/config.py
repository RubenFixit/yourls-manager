"""Load and validate ``site.toml`` (design §2)."""

from __future__ import annotations

import posixpath
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .util import UsageError, console

SITE_FILE = "site.toml"
LOCK_FILE = "yourls.lock.json"
VENDOR_DIR = "yourls"
OVERLAY_DIR = "overlay"
REDIRECTS_FILE = "tests/redirects.toml"

DEFAULT_CORE_REPO = "https://github.com/YOURLS/YOURLS.git"

SLUG_RE = re.compile(r"^[a-z0-9-]+$")
PLUGIN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
TARGET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# Characters allowed in paths on a remote (ssh) target. Keeps rsync's remote-shell
# argument splitting and shell quoting trivially safe on every rsync version.
REMOTE_PATH_RE = re.compile(r"^[A-Za-z0-9._/@+~-]+$")
HOST_RE = re.compile(r"^[A-Za-z0-9.-]+$|^\[[0-9A-Fa-f:.]+\]$")
USER_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class ConfigError(UsageError):
    """Friendly configuration error that names the offending key."""

    def __init__(self, key: str, message: str, file: str = SITE_FILE):
        self.key = key
        super().__init__(f"{file}: {key}: {message}")


@dataclass(frozen=True)
class SiteInfo:
    name: str
    slug: str
    url: str


@dataclass(frozen=True)
class CoreSpec:
    repo: str
    version: str
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class PluginSpec:
    name: str
    repo: str | None = None
    ref: str = "HEAD"
    path: str = "."
    active: bool = True

    @property
    def managed(self) -> bool:
        """True when the plugin is vendored from a repo (and therefore in the lock)."""
        return self.repo is not None


@dataclass(frozen=True)
class BuildSpec:
    out: str = "dist"


@dataclass(frozen=True)
class TargetSpec:
    name: str
    type: str
    path: str
    host: str | None = None
    user: str | None = None
    port: int = 22
    php: str = "php"
    config_file: str = ""
    backup_dir: str = ""
    protect: tuple[str, ...] = ()
    backup_keep: int = 10
    url: str = ""

    @property
    def is_local(self) -> bool:
        return self.host is None


@dataclass
class SiteConfig:
    root: Path
    site: SiteInfo
    core: CoreSpec
    plugins: list[PluginSpec]
    build: BuildSpec
    targets: dict[str, TargetSpec]
    warnings: list[str] = field(default_factory=list)

    @property
    def site_file(self) -> Path:
        return self.root / SITE_FILE

    @property
    def lock_file(self) -> Path:
        return self.root / LOCK_FILE

    @property
    def vendor_dir(self) -> Path:
        return self.root / VENDOR_DIR

    @property
    def overlay_dir(self) -> Path:
        return self.root / OVERLAY_DIR

    @property
    def out_dir(self) -> Path:
        return (self.root / self.build.out).resolve()

    @property
    def managed_plugins(self) -> list[PluginSpec]:
        return [p for p in self.plugins if p.managed]

    @property
    def active_plugins(self) -> list[str]:
        return [p.name for p in self.plugins if p.active]

    def plugin(self, name: str) -> PluginSpec | None:
        for p in self.plugins:
            if p.name == name:
                return p
        return None

    def target(self, name: str) -> TargetSpec:
        try:
            return self.targets[name]
        except KeyError:
            known = ", ".join(sorted(self.targets)) or "(none defined)"
            raise ConfigError(
                f"deploy.targets.{name}", f"no such target; known targets: {known}"
            ) from None


# --------------------------------------------------------------------------- helpers


def find_site_dir(start: Path | None = None) -> Path:
    """Return the nearest directory at or above ``start`` containing site.toml."""
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / SITE_FILE).is_file():
            return candidate
    raise UsageError(f"no {SITE_FILE} found in {here} or any parent directory (use --site-dir)")


class _Reader:
    """Typed accessors over a TOML table that track unknown keys."""

    def __init__(self, table: dict[str, Any], prefix: str, warnings: list[str]):
        if not isinstance(table, dict):
            raise ConfigError(prefix, "must be a table")
        self.table = table
        self.prefix = prefix
        self.warnings = warnings
        self.seen: set[str] = set()

    def key(self, name: str) -> str:
        return f"{self.prefix}.{name}" if self.prefix else name

    def _get(self, name: str, required: bool) -> Any:
        self.seen.add(name)
        if name not in self.table:
            if required:
                raise ConfigError(self.key(name), "is required")
            return None
        return self.table[name]

    def str(self, name: str, default: str | None = None, *, required: bool = False) -> Any:
        value = self._get(name, required)
        if value is None:
            return default
        if not isinstance(value, str):
            raise ConfigError(self.key(name), f"must be a string, got {type(value).__name__}")
        if required and not value.strip():
            raise ConfigError(self.key(name), "must not be empty")
        return value

    def bool(self, name: str, default: bool) -> bool:
        value = self._get(name, False)
        if value is None:
            return default
        if not isinstance(value, bool):
            raise ConfigError(self.key(name), "must be true or false")
        return value

    def int(self, name: str, default: int, *, lo: int, hi: int) -> int:
        value = self._get(name, False)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(self.key(name), "must be an integer")
        if not lo <= value <= hi:
            raise ConfigError(self.key(name), f"must be between {lo} and {hi}")
        return value

    def str_list(self, name: str) -> tuple[str, ...]:
        value = self._get(name, False)
        if value is None:
            return ()
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            raise ConfigError(self.key(name), "must be a list of non-empty strings")
        return tuple(value)

    def finish(self) -> None:
        for extra in sorted(set(self.table) - self.seen):
            self.warnings.append(f"{SITE_FILE}: unknown key '{self.key(extra)}' ignored")


def _check_url(key: str, value: str) -> str:
    if not re.match(r"^https?://[^/\s]+(/\S*)?$", value):
        raise ConfigError(key, f"must be an http(s) URL, got {value!r}")
    if value.endswith("/"):
        raise ConfigError(key, "must not end with a trailing slash")
    return value


def _check_rel_subpath(key: str, value: str) -> str:
    norm = posixpath.normpath(value)
    if value.startswith("/") or norm == ".." or norm.startswith("../"):
        raise ConfigError(key, f"must be a relative path inside the repo, got {value!r}")
    return norm


def normalize_remote_path(value: str) -> str:
    """Normalize a target path: ``~/x`` -> ``x`` (relative paths are relative to $HOME)."""
    value = value.strip()
    if value == "~" or value == "~/":
        return "."
    if value.startswith("~/"):
        value = value[2:]
    if len(value) > 1:
        value = value.rstrip("/")
    return posixpath.normpath(value) if value else value


def parent_of(path: str) -> str:
    parent = posixpath.dirname(path)
    return parent or "."


def join_remote(base: str, *parts: str) -> str:
    joined = posixpath.join(base, *parts)
    return posixpath.normpath(joined)


# --------------------------------------------------------------------------- loader


def load_site(root: Path) -> SiteConfig:
    root = root.resolve()
    path = root / SITE_FILE
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        raise UsageError(f"{path} not found") from None
    except tomllib.TOMLDecodeError as exc:
        raise UsageError(f"{path}: invalid TOML: {exc}") from None
    cfg = parse_site(data, root)
    for w in cfg.warnings:
        console.warn(w)
    return cfg


def parse_site(data: dict[str, Any], root: Path) -> SiteConfig:
    warnings: list[str] = []
    top = _Reader(data, "", warnings)

    # [site]
    site_r = _Reader(top._get("site", True), "site", warnings)
    name = site_r.str("name", required=True)
    slug = site_r.str("slug", required=True)
    if not SLUG_RE.match(slug):
        raise ConfigError("site.slug", f"must match [a-z0-9-]+, got {slug!r}")
    url = _check_url("site.url", site_r.str("url", required=True))
    site_r.finish()
    site = SiteInfo(name=name, slug=slug, url=url)

    # [core]
    core_r = _Reader(top._get("core", True), "core", warnings)
    repo = core_r.str("repo", DEFAULT_CORE_REPO)
    version = core_r.str("version", required=True).strip()
    if version.lower() == "latest":
        raise ConfigError(
            "core.version",
            "'latest' is not allowed here; run `ym core --version latest` to resolve a tag",
        )
    if version.startswith("-") or any(c.isspace() for c in version):
        raise ConfigError("core.version", f"invalid tag {version!r}")
    exclude = core_r.str_list("exclude")
    for pat in exclude:
        if pat.startswith("/") or ".." in pat.split("/"):
            raise ConfigError("core.exclude", f"pattern must be relative to the repo root: {pat!r}")
    core_r.finish()
    core = CoreSpec(repo=repo, version=version, exclude=exclude)

    # [[plugins]]
    raw_plugins = top._get("plugins", False) or []
    if not isinstance(raw_plugins, list):
        raise ConfigError("plugins", "must be an array of tables ([[plugins]])")
    plugins: list[PluginSpec] = []
    seen_names: set[str] = set()
    for i, raw in enumerate(raw_plugins):
        pr = _Reader(raw, f"plugins[{i}]", warnings)
        pname = pr.str("name", required=True)
        if not PLUGIN_NAME_RE.match(pname):
            raise ConfigError(f"plugins[{i}].name", f"invalid plugin directory name {pname!r}")
        if pname.casefold() in seen_names:
            raise ConfigError(f"plugins[{i}].name", f"duplicate plugin {pname!r}")
        seen_names.add(pname.casefold())
        prepo = pr.str("repo")
        ref = pr.str("ref", "HEAD")
        ppath = pr.str("path", ".")
        active = pr.bool("active", True)
        if prepo is None:
            if "ref" in raw or "path" in raw:
                warnings.append(
                    f"{SITE_FILE}: plugins[{i}] ({pname}) has no repo; 'ref'/'path' are ignored"
                )
            ref, ppath = "HEAD", "."
        else:
            if not prepo.strip() or prepo.startswith("-"):
                raise ConfigError(f"plugins[{i}].repo", "invalid repository URL")
            if not ref or ref.startswith("-") or any(c.isspace() for c in ref):
                raise ConfigError(f"plugins[{i}].ref", f"invalid ref {ref!r}")
            ppath = _check_rel_subpath(f"plugins[{i}].path", ppath)
        pr.finish()
        plugins.append(PluginSpec(name=pname, repo=prepo, ref=ref, path=ppath, active=active))

    # [build]
    build_r = _Reader(top._get("build", False) or {}, "build", warnings)
    out = build_r.str("out", "dist")
    _check_rel_subpath("build.out", out)
    if posixpath.normpath(out) in (".", "yourls", "overlay"):
        raise ConfigError("build.out", f"refusing to use {out!r} as build output")
    build_r.finish()
    build = BuildSpec(out=out)

    # [deploy]
    targets: dict[str, TargetSpec] = {}
    deploy_raw = top._get("deploy", False) or {}
    deploy_r = _Reader(deploy_raw, "deploy", warnings)
    targets_raw = deploy_r._get("targets", False) or {}
    if not isinstance(targets_raw, dict):
        raise ConfigError("deploy.targets", "must be a table of targets")
    for tname, traw in targets_raw.items():
        if not TARGET_NAME_RE.match(tname):
            raise ConfigError(f"deploy.targets.{tname}", "invalid target name")
        targets[tname] = _parse_target(tname, traw, site, warnings)
    deploy_r.finish()

    top.finish()
    return SiteConfig(
        root=root,
        site=site,
        core=core,
        plugins=plugins,
        build=build,
        targets=targets,
        warnings=warnings,
    )


def _parse_target(name: str, raw: Any, site: SiteInfo, warnings: list[str]) -> TargetSpec:
    from .deploy import TARGET_TYPES  # local import: registry lives with implementations

    prefix = f"deploy.targets.{name}"
    r = _Reader(raw, prefix, warnings)
    ttype = r.str("type", "rsync")
    if ttype not in TARGET_TYPES:
        raise ConfigError(
            f"{prefix}.type", f"unknown target type {ttype!r}; known: {', '.join(TARGET_TYPES)}"
        )
    host = r.str("host")
    if host is not None and not HOST_RE.match(host):
        raise ConfigError(f"{prefix}.host", f"invalid host {host!r}")
    user = r.str("user")
    if user is not None and not USER_RE.match(user):
        raise ConfigError(f"{prefix}.user", f"invalid user {user!r}")
    port = r.int("port", 22, lo=1, hi=65535)
    path = normalize_remote_path(r.str("path", required=True))
    if path in ("", ".", "/"):
        raise ConfigError(f"{prefix}.path", "must name a web root directory, not $HOME or /")
    php = r.str("php", "php")
    parent = parent_of(path)
    config_file = normalize_remote_path(
        r.str("config_file") or join_remote(parent, "yourls-config", f"{site.slug}.php")
    )
    backup_dir = normalize_remote_path(
        r.str("backup_dir") or join_remote(parent, "yourls-backups", site.slug)
    )
    protect = r.str_list("protect")
    backup_keep = r.int("backup_keep", 10, lo=1, hi=10000)
    url = _check_url(f"{prefix}.url", r.str("url", site.url))
    r.finish()

    if host is not None:
        for key, value in (
            ("path", path),
            ("config_file", config_file),
            ("backup_dir", backup_dir),
            ("php", php),
        ):
            if not REMOTE_PATH_RE.match(value):
                raise ConfigError(
                    f"{prefix}.{key}",
                    f"remote paths may only contain [A-Za-z0-9._/@+~-], got {value!r}",
                )
    for i, p in enumerate(protect):
        if "\n" in p or p.startswith(("+", "-", "P ")):
            raise ConfigError(f"{prefix}.protect[{i}]", f"invalid protect pattern {p!r}")

    return TargetSpec(
        name=name,
        type=ttype,
        path=path,
        host=host,
        user=user,
        port=port,
        php=php,
        config_file=config_file,
        backup_dir=backup_dir,
        protect=protect,
        backup_keep=backup_keep,
        url=url,
    )


# --------------------------------------------------------------------------- editing

_TABLE_RE = re.compile(r"^\s*\[\[?\s*([^\[\]]+?)\s*\]\]?\s*(#.*)?$")
_VERSION_RE = re.compile(r"""^(\s*version\s*=\s*)(["'])(.*?)\2(.*)$""")


def set_core_version(site_file: Path, version: str) -> bool:
    """Rewrite ``[core].version`` in place, preserving formatting. Returns True if changed."""
    text = site_file.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    table = ""
    for i, line in enumerate(lines):
        m = _TABLE_RE.match(line)
        if m:
            table = m.group(1)
            continue
        if table != "core":
            continue
        vm = _VERSION_RE.match(line.rstrip("\r\n"))
        if vm:
            if vm.group(3) == version:
                return False
            eol = line[len(line.rstrip("\r\n")) :]
            lines[i] = f"{vm.group(1)}{vm.group(2)}{version}{vm.group(2)}{vm.group(4)}{eol}"
            site_file.write_text("".join(lines), encoding="utf-8")
            return True
    raise ConfigError("core.version", "could not locate the key to update")
