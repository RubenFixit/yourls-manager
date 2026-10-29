"""`ym plugin list | enable | disable | add | remove` (design §11.1).

site.toml edits are textual.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from .config import SiteConfig, load_site
from .lockfile import load_lock
from .util import UsageError, YMError, console

_TABLE = re.compile(r"^\s*\[")
_PLUGINS = re.compile(r"^\s*\[\[\s*plugins\s*\]\]")
_ACTIVE = re.compile(r"^(\s*active\s*=\s*)(true|false)(.*)$")


def _name_re(name: str) -> re.Pattern[str]:
    return re.compile(rf"""^\s*name\s*=\s*(["']){re.escape(name)}\1""")


def set_active(cfg: SiteConfig, name: str, active: bool) -> bool:
    """Set ``active`` for plugin ``name`` in site.toml. Returns False if unchanged."""
    spec = cfg.plugin(name)
    if spec is None:
        raise UsageError(f"no plugin named {name!r} in site.toml (see `ym plugin list`)")
    if spec.active == active:
        return False
    path = cfg.site_file
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    start = end = None
    for i, line in enumerate(lines):
        if start is None and _PLUGINS.match(line):
            j = i + 1
            while j < len(lines) and not _TABLE.match(lines[j]):
                j += 1
            if any(_name_re(name).match(x) for x in lines[i + 1 : j]):
                start, end = i, j
    if start is None:
        raise UsageError(f"could not find the [[plugins]] block for {name!r} in site.toml")
    value = "true" if active else "false"
    for k in range(start + 1, end):
        m = _ACTIVE.match(lines[k].rstrip("\n"))
        if m:
            lines[k] = f"{m[1]}{value}{m[3]}\n"
            break
    else:
        k = next(k for k in range(start + 1, end) if _name_re(name).match(lines[k]))
        indent = re.match(r"\s*", lines[k])[0]
        lines.insert(k + 1, f"{indent}active = {value}\n")
    path.write_text("".join(lines), encoding="utf-8")
    try:
        if load_site(cfg.root).plugin(name).active != active:
            raise UsageError("edit did not take effect")
    except Exception:
        path.write_text(original, encoding="utf-8")
        raise
    return True


def list_plugins(cfg: SiteConfig) -> None:
    lock = load_lock(cfg.lock_file)
    rows = [("name", "active", "source", "ref", "commit")]
    for p in cfg.plugins:
        locked = lock.plugins.get(p.name)
        if p.repo:
            source = p.repo + ("" if p.path in ("", ".") else f" ({p.path})")
        elif (cfg.overlay_dir / "user/plugins" / p.name).is_dir():
            source = "overlay"
        else:
            source = "core"
        rows.append((p.name, "yes" if p.active else "no", source, p.ref if p.repo else "-",
                     locked.commit[:10] if locked else "-"))  # fmt: skip
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    for r in rows:
        console.info("  ".join(c.ljust(w) for c, w in zip(r, widths, strict=False)) + "  " + r[4])


def remove_plugin(cfg: SiteConfig, name: str, *, keep_files: bool = False) -> None:
    """Delete the plugin's [[plugins]] block; unless keep_files, drop its vendored copy."""
    from .plugins import sync_plugins

    spec = cfg.plugin(name)
    if spec is None:
        raise UsageError(f"no plugin named {name!r} in site.toml (see `ym plugin list`)")
    path = cfg.site_file
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    block = None
    for i, line in enumerate(lines):
        if _PLUGINS.match(line):
            j = i + 1
            while j < len(lines) and not _TABLE.match(lines[j]):
                j += 1
            if any(_name_re(name).match(x) for x in lines[i + 1 : j]):
                block = (i, j)
    if block is None:
        raise UsageError(f"could not find the [[plugins]] block for {name!r} in site.toml")
    start, end = block
    while end > start + 1 and lines[end - 1].lstrip().startswith("#"):
        end -= 1  # comment lines just before the next table belong to it
    del lines[start:end]
    path.write_text("".join(lines), encoding="utf-8")
    try:
        new_cfg = load_site(cfg.root)
        if new_cfg.plugin(name) is not None:
            raise UsageError("edit did not take effect")
    except Exception:
        path.write_text(original, encoding="utf-8")
        raise
    console.info(f"{name}: removed from site.toml")
    if spec.repo and not keep_files:
        sync_plugins(new_cfg, None)  # deletes vendored dirs of plugins no longer listed
    overlay = cfg.overlay_dir / "user/plugins" / name
    if overlay.is_dir():
        console.info(f"  its overlay copy stays: delete {overlay} if you don't need it")
    console.info(
        "  the next deploy deactivates it and removes its files; plugin data (options,"
        " tables) stays in the database"
    )


_WEB = re.compile(
    r"^https://(?P<host>github\.com|gitlab\.com|codeberg\.org|bitbucket\.org)/"
    r"(?P<repo>[^/]+/[^/#?]+?)(?:\.git)?"
    r"(?:/(?:-/)?(?:tree|src|blob)/(?P<ref>[^/]+)(?:/(?P<path>[^?#]*))?)?/?(?:[?#].*)?$"
)


def parse_plugin_url(url: str) -> tuple[str, str | None, str | None]:
    """Project page or clone URL -> (clone url, ref or None, subdir or None)."""
    url = url.strip()
    m = _WEB.match(url)
    if m:
        path = (m["path"] or "").strip("/") or None
        if path and path.endswith("plugin.php"):
            path = path.rsplit("/", 1)[0] if "/" in path else None
        return f"https://{m['host']}/{m['repo']}.git", m["ref"], path
    if re.match(r"^(https?://|ssh://|git@|file://)", url):
        return url, None, None
    raise UsageError(f"not a git or project URL: {url!r}")


_URL = re.compile(r"^(https?://|ssh://|git@|file://)")
_HEADER = re.compile(r"^[\s*#@/]*([A-Za-z][A-Za-z ]{1,40}?)\s*:\s*(.+?)\s*$")


def read_header(plugin_php: Path) -> dict[str, str]:
    """``Key: value`` lines of the first comment block of plugin.php (YOURLS plugin header)."""
    text = plugin_php.read_text(encoding="utf-8", errors="replace")[:8192]
    m = re.search(r"/\*(.*?)\*/", text, re.S)
    out: dict[str, str] = {}
    for line in (m[1] if m else "").splitlines():
        hm = _HEADER.match(line)
        if hm and hm[1].strip() not in out and "://" not in hm[1]:
            out[hm[1].strip()] = hm[2]
    return out


def find_plugin_dirs(root: Path) -> list[str]:
    """'.' if plugin.php is at the top, else the subdirectories (one level) that hold one."""
    if (root / "plugin.php").is_file():
        return ["."]
    return sorted(
        d.name
        for d in root.iterdir()
        if d.is_dir() and not d.name.startswith(".") and (d / "plugin.php").is_file()
    )


Chooser = Callable[[str, list[str]], str]


def add_plugin(
    cfg: SiteConfig,
    source: str,
    *,
    name: str | None = None,
    ref: str | None = None,
    path: str | None = None,
    active: bool = True,
    sync: bool = True,
    choose: Chooser | None = None,
    refresh: bool = False,
) -> str:
    """Add a plugin by catalog name or git/project URL (design §11.1). Returns its name."""
    from . import gitsrc
    from .drift import plugin_dir
    from .plugins import sync_plugins
    from .vendor import rmtree

    entry = None
    if _URL.match(source.strip()):
        url = source
    else:
        from .catalog import find, load_catalog

        entry = find(load_catalog(refresh=refresh), source)
        url = entry.url
        console.info(f"{entry.name}: {url}")
    try:
        repo, url_ref, url_path = parse_plugin_url(url)
    except UsageError:
        if entry is None:
            raise
        raise UsageError(_not_git(entry.name, url)) from None
    ref, path = ref or url_ref or "HEAD", (path or url_path or "").strip("/") or None
    for p in cfg.plugins:
        if p.repo == repo and (path is None or p.path == path):
            raise UsageError(f"already in site.toml as {p.name!r}")

    # Look at the plugin before touching site.toml.
    try:
        commit = gitsrc.resolve_ref(repo, ref)
    except YMError as exc:
        if entry is not None and ref == "HEAD":
            raise UsageError(_not_git(entry.name, url) + f"\n({exc})") from None
        raise
    checkout_root = gitsrc.temp_checkout_dir()
    try:
        co = gitsrc.fetch_commit(repo, commit, checkout_root / "src")
        if path is None:
            found = find_plugin_dirs(co.path)
            if not found:
                raise UsageError(f"no plugin.php in {repo} (top level or one directory down)")
            if len(found) > 1:
                if choose is None:
                    raise UsageError(
                        f"{repo} holds several plugins; pick one with --path: " + ", ".join(found)
                    )
                path = choose(f"{repo} holds several plugins; which one?", found)
            else:
                path = found[0]
        src = (co.path / path).resolve()
        if not src.is_relative_to(co.path.resolve()) or not (src / "plugin.php").is_file():
            subs = find_plugin_dirs(src) if src.is_dir() else []
            hint = f"; try --path {' or --path '.join(f'{path}/{s}' for s in subs)}" if subs else ""
            raise UsageError(f"no plugin.php in {repo} at {path!r}{hint}")
        header = read_header(src / "plugin.php")
    finally:
        rmtree(checkout_root)

    for p in cfg.plugins:
        if p.repo == repo and p.path == path:
            raise UsageError(f"already in site.toml as {p.name!r}")
    base = path.rsplit("/", 1)[-1] if path != "." else repo.rstrip("/").rsplit("/", 1)[-1]
    name = name or re.sub(r"[^A-Za-z0-9._-]+", "-", base.removesuffix(".git")).strip("-.")
    if not name:
        raise UsageError("could not derive a directory name; pass --name")
    if cfg.plugin(name) is not None:
        raise UsageError(f"a plugin named {name!r} is already in site.toml; pass --name")
    if (cfg.overlay_dir / "user/plugins" / name).exists():
        raise UsageError(f"overlay/user/plugins/{name} exists; pass a different --name")
    lock = load_lock(cfg.lock_file)
    if plugin_dir(cfg, name).exists() and name not in lock.plugins:
        raise UsageError(
            f"yourls/user/plugins/{name} already exists (bundled with core?); pass --name"
        )

    original = cfg.site_file.read_text(encoding="utf-8")
    block = f'\n[[plugins]]\nname   = "{name}"\nrepo   = "{repo}"\n'
    if ref != "HEAD":
        block += f'ref    = "{ref}"\n'
    if path != ".":
        block += f'path   = "{path}"\n'
    block += f"active = {'true' if active else 'false'}\n"
    cfg.site_file.write_text(original.rstrip("\n") + "\n" + block, encoding="utf-8")
    try:
        new_cfg = load_site(cfg.root)
        if new_cfg.plugin(name) is None:
            raise UsageError("edit did not take effect")
        if sync:
            sync_plugins(new_cfg, [name])
    except BaseException:
        cfg.site_file.write_text(original, encoding="utf-8")
        new = load_site(cfg.root)
        if plugin_dir(new, name).is_dir() and name not in {p.name for p in cfg.plugins}:
            rmtree(plugin_dir(new, name))
        raise
    title = header.get("Plugin Name", name)
    version = header.get("Version")
    console.info(
        f"{name}: added {title}" + (f" {version}" if version else "")
        + f" from {repo} (ref {ref}, path {path}); active = {str(active).lower()}"
    )  # fmt: skip
    if header.get("Description"):
        console.info(f"  {header['Description']}")
    for key, value in header.items():
        if key.lower().startswith("requires"):
            console.info(f"  {key}: {value}")
    if not sync:
        console.info(f"  not vendored yet: run `ym sync --plugin {name}`")
    return name


def _not_git(name: str, url: str) -> str:
    return (
        f"{name}: the catalog links to {url}, which isn't a git repository ym can fetch."
        " Open it, find the plugin's source repository, and run `ym plugin add <git-url>`."
    )
