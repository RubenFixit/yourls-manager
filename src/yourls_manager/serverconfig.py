"""`ym target config`: create the server-local YOURLS config on a target.

Secrets (DB password, admin password) are read from the environment or prompted with
``getpass``; they never appear in argv or logs. The admin password is stored in the format
YOURLS uses (``phpass:`` + ``password_hash()`` with ``$`` stored as ``!``): hashed by the
target's PHP (password on stdin) for rsync targets, or locally with bcrypt for sftp targets,
which have no PHP CLI.
"""

from __future__ import annotations

import getpass
import os
import re
import secrets
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .config import SiteConfig, TargetSpec, join_remote, parent_of
from .remote import Runner, runner_for
from .util import UsageError, YMError, compact_utc, console

if TYPE_CHECKING:  # pragma: no cover
    from .transport.sftp import SftpTransport

EXAMPLE_FILE = "server/config.example.php"
DB_PASSWORD_ENV = "YM_DB_PASSWORD"
ADMIN_PASSWORD_ENV = "YM_ADMIN_PASSWORD"

# Hash on the target's PHP; the password arrives on stdin, never in argv.
HASH_PHP = (
    "$p = stream_get_contents(STDIN);"
    " if ($p === false || $p === '') { fwrite(STDERR, 'empty password'); exit(1); }"
    " echo 'phpass:', str_replace('$', '!', password_hash($p, PASSWORD_DEFAULT));"
)
PHPASS_RE = re.compile(r"^phpass:![0-9a-z]+![A-Za-z0-9!./]+$")
BCRYPT_COST = 10
BCRYPT_MAX_BYTES = 72

# Markers that identify the ym config shim (overlay/user/config.php from the template).
SHIM_MARKERS = ("YOURLS_CONFIG_FILE", "server-local configuration missing")

# A quoted CHANGE-ME on a code line (comment lines are ignored).
_PLACEHOLDER_RE = re.compile(
    r"""^(?![ \t]*(?:\*|//|\#|/\*)).*['"][^'"\n]*CHANGE-ME.*$""", re.MULTILINE
)

USERNAME_RE = re.compile(r"^[A-Za-z0-9._@+-]+$")
DB_IDENT_RE = re.compile(r"^[A-Za-z0-9_$-]+$")
DB_PREFIX_RE = re.compile(r"^[A-Za-z0-9_]*$")

AskFn = Callable[[str], str]


# --------------------------------------------------------------------------- PHP editing


def php_string(value: str) -> str:
    """A single-quoted PHP string literal for ``value``."""
    if any(c in value for c in "\0\r\n"):
        raise UsageError("values must not contain newlines or NUL bytes")
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _define_re(name: str) -> re.Pattern[str]:
    return re.compile(
        r"^(?P<head>[ \t]*define\s*\(\s*(['\"])" + re.escape(name) + r"\2\s*,\s*)"
        r"(?P<value>.*?)"
        r"(?P<tail>\s*\)\s*;[^\n]*)$",
        re.MULTILINE,
    )


_PASSWORDS_RE = re.compile(
    r"^(?P<indent>[ \t]*)\$yourls_user_passwords\s*=\s*(?:\[.*?\]|array\s*\(.*?\))\s*;",
    re.MULTILINE | re.DOTALL,
)


def set_define(text: str, name: str, php_literal: str) -> str:
    rx = _define_re(name)
    matches = list(rx.finditer(text))
    if not matches:
        raise YMError(f"{EXAMPLE_FILE}: no `define( '{name}', ... );` line found")
    if len(matches) > 1:
        raise YMError(f"{EXAMPLE_FILE}: `define( '{name}', ... );` appears more than once")
    return rx.sub(lambda m: m["head"] + php_literal + m["tail"], text, count=1)


def set_user_passwords(text: str, users: Mapping[str, str]) -> str:
    m = _PASSWORDS_RE.search(text)
    if not m:
        raise YMError(f"{EXAMPLE_FILE}: no `$yourls_user_passwords = [ ... ];` block found")
    indent = m["indent"]
    body = "".join(f"{indent}    {php_string(u)} => {php_string(h)},\n" for u, h in users.items())
    block = f"{indent}$yourls_user_passwords = [\n{body}{indent}];"
    return text[: m.start()] + block + text[m.end() :]


def render_config(
    template: str,
    *,
    db_user: str,
    db_pass: str,
    db_name: str,
    db_host: str,
    db_prefix: str,
    site_url: str,
    cookie_key: str,
    users: Mapping[str, str],
) -> str:
    text = template
    text = set_define(text, "YOURLS_DB_USER", php_string(db_user))
    text = set_define(text, "YOURLS_DB_PASS", php_string(db_pass))
    text = set_define(text, "YOURLS_DB_NAME", php_string(db_name))
    text = set_define(text, "YOURLS_DB_HOST", php_string(db_host))
    text = set_define(text, "YOURLS_DB_PREFIX", php_string(db_prefix))
    text = set_define(text, "YOURLS_SITE", php_string(site_url))
    text = set_define(text, "YOURLS_COOKIEKEY", php_string(cookie_key))
    text = set_user_passwords(text, users)
    left = _PLACEHOLDER_RE.findall(text)
    if left:
        lines = [ln.strip() for ln in left]
        raise YMError(
            f"{EXAMPLE_FILE}: placeholder(s) left after filling in values:\n  "
            + "\n  ".join(lines[:10])
        )
    return text


def is_shim(text: str) -> bool:
    return all(marker in text for marker in SHIM_MARKERS)


def plaintext_users(text: str) -> list[str]:
    """Users in a config whose password is stored in clear text (best effort)."""
    m = _PASSWORDS_RE.search(text)
    if not m:
        return []
    users = []
    for um in re.finditer(r"""(['"])([^'"]+)\1\s*=>\s*(['"])([^'"]*)\3""", m.group(0)):
        if not um[4].startswith(("phpass:", "md5:")):
            users.append(um[2])
    return users


# --------------------------------------------------------------------------- inputs


@dataclass
class ServerConfigOptions:
    force: bool = False
    from_existing: bool = False
    db_host: str | None = None
    db_name: str | None = None
    db_user: str | None = None
    db_prefix: str | None = None
    admin_user: str | None = None


def _interactive() -> bool:
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def _ask_value(
    label: str, flag: str, value: str | None, ask: AskFn | None, default: str | None = None
) -> str:
    if value is not None:
        return value
    if ask is None:
        if not _interactive():
            if default is not None:
                return default
            raise UsageError(f"{label} is required: pass {flag}")
        ask = input
    suffix = f" [{default}]" if default is not None else ""
    while True:
        answer = ask(f"{label}{suffix}: ").strip()
        if answer:
            return answer
        if default is not None:
            return default


def example_value(text: str, name: str) -> str | None:
    """The single-quoted string value of ``define('NAME', '...')`` in ``text``, if simple."""
    m = _define_re(name).search(text)
    if not m:
        return None
    vm = re.fullmatch(r"'([^'\\]*)'", m["value"].strip())
    return vm[1] if vm else None


def _secret(
    env_name: str, label: str, env: Mapping[str, str], secret_prompt: AskFn | None, confirm: bool
) -> str:
    value = env.get(env_name)
    if value:
        console.info(f"  {label}: from ${env_name}")
        return value
    if secret_prompt is None:
        if not _interactive():
            raise UsageError(f"{label} is required: set ${env_name} or run interactively")
        secret_prompt = getpass.getpass
    while True:
        first = secret_prompt(f"{label}: ")
        if not first:
            console.warn("empty password; try again")
            continue
        if not confirm or secret_prompt(f"{label} (again): ") == first:
            return first
        console.warn("passwords do not match; try again")


def hash_password(runner: Runner, php: str, password: str) -> str:
    proc = runner.run([php, "-r", HASH_PHP], input=password.encode("utf-8"), check=False)
    out = proc.stdout.decode(errors="replace").strip()
    if proc.returncode != 0 or not PHPASS_RE.match(out):
        err = proc.stderr.decode(errors="replace").strip()
        raise YMError(
            f"could not hash the admin password with {php} on {runner.describe()}"
            + (f": {err}" if err else "")
        )
    return out


def bcrypt_phpass(password: str, *, rounds: int = BCRYPT_COST) -> str:
    """Hash locally in YOURLS' format: ``phpass:`` + bcrypt with ``$2y$`` and ``$`` -> ``!``.

    PHP's ``password_hash(PASSWORD_DEFAULT)`` is bcrypt ``$2y$``; ``$2b$`` hashes are the
    same algorithm, and ``password_verify`` accepts the ``$2y$`` spelling of them.
    """
    import bcrypt

    raw = password.encode("utf-8")
    if not raw:
        raise UsageError("empty password")
    if len(raw) > BCRYPT_MAX_BYTES:
        raise UsageError(f"the admin password must be at most {BCRYPT_MAX_BYTES} bytes (bcrypt)")
    hashed = bcrypt.hashpw(raw, bcrypt.gensalt(rounds=rounds)).decode("ascii")
    if hashed.startswith("$2b$"):
        hashed = "$2y$" + hashed[4:]
    out = "phpass:" + hashed.replace("$", "!")
    if not PHPASS_RE.match(out):  # pragma: no cover - defensive
        raise YMError("bcrypt produced an unexpected hash format")
    return out


# --------------------------------------------------------------------------- target files


class _RunnerFiles:
    """target config's file access through a shell runner (rsync targets)."""

    def __init__(self, runner: Runner):
        self.runner = runner

    def describe(self) -> str:
        return self.runner.describe()

    def abspath(self, path: str) -> str:
        return self.runner.abspath(path)

    def exists(self, path: str) -> bool:
        return self.runner.exists(path, "-e")

    def is_file(self, path: str) -> bool:
        return self.runner.exists(path, "-f")

    def read_text(self, path: str) -> str:
        return self.runner.run(["cat", "--", path]).stdout.decode("utf-8", errors="replace")

    def backup(self, path: str, dest: str) -> None:
        self.runner.run(["cp", "-p", "--", path, dest])
        self.runner.run(["chmod", "600", "--", dest])

    def write_secret(self, path: str, data: bytes) -> None:
        self.runner.mkdir(parent_of(path))
        self.runner.write_file(path, data, "0600")


class _SftpFiles:
    """target config's file access over SFTP (sftp targets)."""

    def __init__(self, transport: SftpTransport):
        self.t = transport

    def describe(self) -> str:
        return self.t.describe()

    def abspath(self, path: str) -> str:
        return self.t.abspath(path)

    def exists(self, path: str) -> bool:
        return self.t.exists(path)

    def is_file(self, path: str) -> bool:
        return self.t.is_file(path)

    def read_text(self, path: str) -> str:
        return self.t.read_bytes(path).decode("utf-8", errors="replace")

    def backup(self, path: str, dest: str) -> None:
        self.t.write_bytes(dest, self.t.read_bytes(path), 0o600)

    def write_secret(self, path: str, data: bytes) -> None:
        self.t.mkdirs(parent_of(path))
        self.t.write_bytes(path, data, 0o600)  # atomic; 0600 before any data is written


Files = _RunnerFiles | _SftpFiles


# --------------------------------------------------------------------------- command


def _existing_install_config(files: Files, web_root: str) -> str:
    for rel in ("user/config.php", "includes/config.php"):
        candidate = join_remote(web_root, rel)
        if files.is_file(candidate):
            return candidate
    raise UsageError(
        f"no existing YOURLS config at {web_root}/user/config.php or"
        f" {web_root}/includes/config.php on {files.describe()}"
    )


def _write_config(files: Files, path: str, data: bytes, force: bool) -> None:
    if files.exists(path):
        if not force:
            raise UsageError(
                f"{path} already exists on {files.describe()}; pass --force to replace it"
                " (the old file is kept as a .bak-<timestamp> copy)"
            )
        backup = f"{path}.bak-{compact_utc()}"
        files.backup(path, backup)
        console.info(f"  backed up the old file to {backup}")
    files.write_secret(path, data)


def server_config(
    cfg: SiteConfig,
    target_name: str,
    opts: ServerConfigOptions,
    *,
    runner: Runner | None = None,
    transport: SftpTransport | None = None,
    env: Mapping[str, str] | None = None,
    ask: AskFn | None = None,
    secret_prompt: AskFn | None = None,
) -> str:
    """Create the target's server-local config. Returns its absolute path on the target."""
    spec: TargetSpec = cfg.target(target_name)
    env = os.environ if env is None else env
    if spec.is_sftp:
        from .transport.sftp import SftpTransport

        t = transport or SftpTransport.for_target(spec)
        with t:
            return _server_config(cfg, spec, opts, _SftpFiles(t), None, env, ask, secret_prompt)
    runner = runner or runner_for(spec)
    return _server_config(cfg, spec, opts, _RunnerFiles(runner), runner, env, ask, secret_prompt)


def _server_config(
    cfg: SiteConfig,
    spec: TargetSpec,
    opts: ServerConfigOptions,
    files: Files,
    runner: Runner | None,
    env: Mapping[str, str],
    ask: AskFn | None,
    secret_prompt: AskFn | None,
) -> str:
    target_name = spec.name
    path = files.abspath(spec.config_file)
    console.step(f"target config {target_name} ({files.describe()}:{path})")

    if opts.from_existing:
        flags = [opts.db_host, opts.db_name, opts.db_user, opts.db_prefix, opts.admin_user]
        if any(f is not None for f in flags):
            raise UsageError(
                "--from-existing copies the existing config; it takes no --db-*/--admin-user values"
            )
        if files.exists(path) and not opts.force:
            raise UsageError(
                f"{path} already exists on {files.describe()}; pass --force to replace it"
            )
        web_root = files.abspath(spec.path)
        source = _existing_install_config(files, web_root)
        text = files.read_text(source)
        if is_shim(text):
            raise UsageError(
                f"{source} is already the yourls-manager config shim, not a real config;"
                f" create {path} with `ym target config {target_name}` instead"
            )
        if "YOURLS_DB_" not in text:
            raise UsageError(f"{source} does not look like a YOURLS config (no YOURLS_DB_*)")
        _write_config(files, path, text.encode("utf-8"), opts.force)
        console.info(f"  copied {source} -> {path} (mode 0600)")
        if "__DIR__" in text or "__FILE__" in text or "dirname(" in text:
            console.warn(
                f"{source} refers to its own location (__DIR__/__FILE__/dirname); check that"
                f" those paths still resolve from {path}"
            )
        clear = plaintext_users(text)
        if clear:
            console.warn(
                "password(s) stored in clear text for: " + ", ".join(clear) + ". The config"
                " shim disables YOURLS' automatic re-hashing; replace them with phpass:"
                " hashes (see server/config.example.php)."
            )
        return path

    example = cfg.root / EXAMPLE_FILE
    if not example.is_file():
        raise UsageError(f"{example} not found (the template provides it)")
    template = example.read_text(encoding="utf-8")
    if files.exists(path) and not opts.force:
        raise UsageError(
            f"{path} already exists on {files.describe()}; pass --force to replace it"
            " (the old file is kept as a .bak-<timestamp> copy)"
        )

    db_host = _ask_value(
        "Database host",
        "--db-host",
        opts.db_host,
        ask,
        example_value(template, "YOURLS_DB_HOST") or "localhost",
    )
    db_name = _ask_value("Database name", "--db-name", opts.db_name, ask)
    db_user = _ask_value("Database user", "--db-user", opts.db_user, ask)
    db_prefix = _ask_value(
        "Table prefix",
        "--db-prefix",
        opts.db_prefix,
        ask,
        example_value(template, "YOURLS_DB_PREFIX") or "yourls_",
    )
    admin_user = _ask_value("Admin username", "--admin-user", opts.admin_user, ask)
    if not DB_IDENT_RE.match(db_name):
        raise UsageError(f"invalid database name {db_name!r}")
    if not USERNAME_RE.match(admin_user):
        raise UsageError(f"invalid admin username {admin_user!r} (use [A-Za-z0-9._@+-])")
    if not DB_PREFIX_RE.match(db_prefix):
        raise UsageError(f"invalid table prefix {db_prefix!r} (use [A-Za-z0-9_])")
    db_pass = _secret(DB_PASSWORD_ENV, "Database password", env, secret_prompt, False)
    admin_pass = _secret(ADMIN_PASSWORD_ENV, f"Password for {admin_user}", env, secret_prompt, True)

    if runner is None:
        console.info(f"  hashing the admin password locally (bcrypt, cost {BCRYPT_COST})")
        phpass = bcrypt_phpass(admin_pass)
    else:
        console.info(f"  hashing the admin password with {spec.php} on {runner.describe()}")
        phpass = hash_password(runner, spec.php, admin_pass)
    text = render_config(
        template,
        db_user=db_user,
        db_pass=db_pass,
        db_name=db_name,
        db_host=db_host,
        db_prefix=db_prefix,
        site_url=spec.url,
        cookie_key=secrets.token_hex(32),
        users={admin_user: phpass},
    )
    _write_config(files, path, text.encode("utf-8"), opts.force)
    console.info(
        f"  wrote {path} (mode 0600): db {db_user}@{db_host}"
        f"/{db_name}, site {spec.url}, admin user {admin_user}"
    )
    return path


__all__ = [
    "ServerConfigOptions",
    "bcrypt_phpass",
    "hash_password",
    "is_shim",
    "render_config",
    "server_config",
]
