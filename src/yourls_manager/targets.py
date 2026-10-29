"""`ym target add` / `ym target list`: deploy targets in site.toml (design §12).

``add`` appends a ``[deploy.targets.NAME]`` block to site.toml as text (comments and layout
are kept, like ``ym plugin add``), re-parses the file and restores it if the result doesn't
validate.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass

from .config import TARGET_NAME_RE, ConfigError, SiteConfig, TargetSpec, load_site
from .util import UsageError, console

Ask = Callable[[str, str | None], str]

TYPES = ("rsync", "sftp")
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass
class TargetOptions:
    type: str | None = None
    host: str | None = None
    user: str | None = None
    port: int | None = None
    path: str | None = None
    php: str | None = None
    url: str | None = None
    accept: str | None = None


def describe(spec: TargetSpec) -> str:
    """``user@host:path``, ``sftp://user@host:path`` or ``local:path``."""
    where = f"{spec.user + '@' if spec.user else ''}{spec.host}" if spec.host else "local"
    return f"{'sftp://' if spec.is_sftp else ''}{where}:{spec.path}"


def _toml_str(value: str) -> str:
    return json.dumps(value)  # a JSON string is a valid TOML basic string


def _header(name: str) -> str:
    key = name if _BARE_KEY.match(name) else _toml_str(name)
    return f"[deploy.targets.{key}]"


def _fill(cfg: SiteConfig, name: str, o: TargetOptions, ask: Ask | None) -> TargetOptions:
    """Ask for what's missing (on a terminal), else apply defaults / require flags."""
    o = TargetOptions(**vars(o))

    def q(question: str, default: str | None) -> str:
        assert ask is not None
        return ask(question, default).strip()

    if o.type is None:
        o.type = q(f"Target type ({' or '.join(TYPES)})", "rsync") if ask else "rsync"
    if o.type not in TYPES:
        raise UsageError(f"--type must be one of {', '.join(TYPES)}, got {o.type!r}")
    sftp = o.type == "sftp"
    if o.host is None and ask:
        hint = "" if sftp else " (empty: this machine)"
        o.host = q(f"Server host name{hint}", None) or None
    if sftp and not o.host:
        raise UsageError("sftp targets need --host")
    if o.host and o.user is None and ask:
        o.user = q("Login user (empty: from ~/.ssh/config or your local user)", None) or None
    if o.host and o.port is None and ask:
        answer = q("Port", "22")
        if not answer.isdigit():
            raise UsageError(f"port must be a number, got {answer!r}")
        o.port = int(answer)
    if o.path is None:
        if not ask:
            raise UsageError("--path (the web root) is required")
        rel = "relative to the login directory" if o.host else "on this machine"
        o.path = q(f"Web root ({rel}), e.g. go.example.com", None)
    if not o.path:
        raise UsageError("the web root path is required")
    if sftp and o.php is not None:
        console.warn("--php does not apply to sftp targets (no PHP CLI there); ignored")
        o.php = None
    elif not sftp and o.host and o.php is None and ask:
        o.php = q("PHP CLI on the server", "php")
    if o.url is None and ask:
        o.url = q("Public URL of the site on this target", cfg.site.url)
    if o.accept is None:
        o.accept = "release" if name == "prod" else "any"
    return o


def render_block(cfg: SiteConfig, name: str, o: TargetOptions) -> str:
    lines = [_header(name), f"type   = {_toml_str(o.type or 'rsync')}"]
    if o.host:
        lines.append(f"host   = {_toml_str(o.host)}")
    if o.user:
        lines.append(f"user   = {_toml_str(o.user)}")
    if o.port and o.port != 22:
        lines.append(f"port   = {o.port}")
    lines.append(f"path   = {_toml_str(o.path or '')}")
    if o.php and o.php != "php":
        lines.append(f"php    = {_toml_str(o.php)}")
    if o.url and o.url != cfg.site.url:
        lines.append(f"url    = {_toml_str(o.url)}")
    lines.append(f"accept = {_toml_str(o.accept or 'any')}")
    return "\n".join(lines) + "\n"


def add_target(
    cfg: SiteConfig, name: str, opts: TargetOptions, *, ask: Ask | None = None
) -> TargetSpec:
    if not TARGET_NAME_RE.match(name):
        raise UsageError(f"invalid target name {name!r} (letters, digits, '.', '_', '-')")
    if name in cfg.targets:
        raise UsageError(f"target {name} already exists in site.toml")
    o = _fill(cfg, name, opts, ask)
    block = render_block(cfg, name, o)
    original = cfg.site_file.read_text(encoding="utf-8")
    cfg.site_file.write_text(original.rstrip("\n") + "\n\n" + block, encoding="utf-8")
    try:
        spec = load_site(cfg.root).target(name)
    except ConfigError as exc:
        cfg.site_file.write_text(original, encoding="utf-8")
        raise UsageError(f"site.toml left unchanged: {exc}") from exc
    except BaseException:
        cfg.site_file.write_text(original, encoding="utf-8")
        raise
    console.info(f"added target {name} ({spec.type}, {describe(spec)}) to site.toml:")
    console.info("")
    console.info(block.rstrip("\n"))
    console.info("")
    console.info("next steps:")
    if not spec.is_local:
        console.info(f"  ym target login {name}    # check that ym can log in")
    console.info(f"  ym target config {name}   # create the server-local config (secrets)")
    console.info(f"  ym deploy {name} --dry-run")
    if spec.accept == "release":
        console.info(
            f'  ({name} has accept = "release": it only takes a final release tag;'
            " see `ym release`)"
        )
    return spec


def target_rows(cfg: SiteConfig) -> list[dict]:
    return [
        {
            "name": s.name,
            "type": s.type,
            "where": describe(s),
            "url": s.url,
            "accept": s.accept,
            "ci": s.ci,
        }
        for s in cfg.targets.values()
    ]


def list_targets(cfg: SiteConfig, *, as_json: bool = False) -> None:
    from .report import _table

    rows = target_rows(cfg)
    if as_json:
        console.info(json.dumps(rows, indent=2))
        return
    if not rows:
        console.info("no deploy targets in site.toml; add one with `ym target add NAME`")
        return
    header = ["target", "type", "where", "url", "accept", "ci"]
    table = [[r[k] if k != "ci" else ("yes" if r[k] else "no") for k in r] for r in rows]
    console.info(_table(table, header))
