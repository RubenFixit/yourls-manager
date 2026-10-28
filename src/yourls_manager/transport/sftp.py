"""SFTP transport for ``type = "sftp"`` targets (design §10.2).

paramiko is imported lazily (only when a connection is made), so commands that never touch
an sftp target don't load it.

- ``~/.ssh/config`` supplies HostName, Port, User and IdentityFile for the target's host
  unless the target sets them itself.
- Host keys come from ``~/.ssh/known_hosts``. An unknown host is trusted only after a yes on
  a terminal (then appended to known_hosts); without a terminal ym fails with instructions.
  A changed host key always fails.
- Auth order: ssh-agent, IdentityFile / default keys, then a password from
  ``YM_<TARGET>_PASSWORD`` or, on a terminal, one ``getpass`` prompt per command.
  ym never stores passwords.
- Writes are atomic: ``.<name>.ym-tmp`` in the same directory, then ``posix_rename``
  (falling back to remove + rename on servers without the extension).
"""

from __future__ import annotations

import base64
import contextlib
import getpass
import hashlib
import logging
import os
import posixpath
import re
import socket
import stat
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import TargetSpec
from ..remote import to_home_relative
from ..util import UsageError, YMError, console

if TYPE_CHECKING:  # pragma: no cover
    import paramiko

Confirm = Callable[[str], bool]
SecretPrompt = Callable[[str], str]
PathFilter = Callable[[str], bool]

TMP_SUFFIX = ".ym-tmp"
CONNECT_TIMEOUT = 20

# One password prompt per command (process), shared by every connection to the same login.
_PASSWORDS: dict[tuple[str, str, int], str] = {}


class SftpError(YMError):
    """Connection-level failure; ``reason`` is auth | hostkey | network | other."""

    reason = "other"


class SftpAuthError(SftpError):
    reason = "auth"


class SftpNetworkError(SftpError):
    reason = "network"


class SftpHostKeyError(SftpError):
    reason = "hostkey"
    exit_code = UsageError.exit_code

    def __init__(self, message: str, *, changed: bool = False):
        super().__init__(message)
        self.changed = changed


def password_env(target_name: str) -> str:
    """``YM_<TARGET>_PASSWORD``: target name upper-cased, non-alphanumerics -> ``_``."""
    return "YM_" + re.sub(r"[^A-Za-z0-9]", "_", target_name).upper() + "_PASSWORD"


def fingerprint(key: Any) -> str:
    """OpenSSH-style ``SHA256:<base64>`` fingerprint of a paramiko public key."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def known_hosts_name(host: str, port: int) -> str:
    return host if port == 22 else f"[{host}]:{port}"


def _tty() -> bool:
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def _ask_yes(question: str) -> bool:
    while True:
        answer = input(f"{question} [y/N]: ").strip().lower()
        if answer in ("", "n", "no"):
            return False
        if answer in ("y", "yes"):
            return True


def _quiet_paramiko() -> None:
    # paramiko logs transport errors that ym reports itself; keep them off stderr.
    log = logging.getLogger("paramiko")
    if not any(isinstance(h, logging.NullHandler) for h in log.handlers):
        log.addHandler(logging.NullHandler())
        log.propagate = False


@dataclass(frozen=True)
class Endpoint:
    alias: str  # host as written in site.toml
    host: str  # host actually connected to (ssh config HostName)
    port: int
    user: str
    identity_files: tuple[str, ...] = ()


def resolve_endpoint(
    host: str,
    *,
    port: int | None = None,
    user: str | None = None,
    ssh_config: Path | None = None,
) -> Endpoint:
    """Apply ``~/.ssh/config`` for ``host``; explicit ``port``/``user`` win."""
    cfg: dict[str, Any] = {}
    path = ssh_config if ssh_config is not None else Path.home() / ".ssh" / "config"
    if path.is_file():
        import paramiko

        try:
            cfg = dict(paramiko.SSHConfig.from_path(str(path)).lookup(host))
        except Exception as exc:  # paramiko raises several types for bad configs
            console.warn(f"{path}: could not parse ({exc}); ignoring it")
            cfg = {}
    real_host = str(cfg.get("hostname") or host)
    if port is None:
        try:
            port = int(cfg.get("port", 22))
        except (TypeError, ValueError):
            port = 22
    user = user or cfg.get("user") or getpass.getuser()
    identities = tuple(os.path.expanduser(str(f)) for f in cfg.get("identityfile") or [])
    return Endpoint(alias=host, host=real_host, port=port, user=user, identity_files=identities)


class _HostKeyPolicy:
    """paramiko MissingHostKeyPolicy: ask on a terminal, otherwise refuse with help."""

    def __init__(self, transport: SftpTransport):
        self.t = transport

    def missing_host_key(self, client: Any, hostname: str, key: Any) -> None:
        t = self.t
        fp = fingerprint(key)
        what = f"{key.get_name()} {fp}"
        if not t.interactive:
            raise SftpHostKeyError(
                f"the SSH host key of {hostname} is not known yet ({what}).\n"
                "Check the fingerprint with your host, then either run"
                f" `ym ssh-setup {t.target_name or '<target>'}` in a terminal (it asks to trust"
                f" it), or add it to {t.known_hosts}:\n"
                f"  ssh-keyscan -p {t.endpoint.port} {t.endpoint.host} >> {t.known_hosts}\n"
                "In CI, write the known_hosts secret to that file before running ym."
            )
        console.info(f"The SSH host key of {hostname} is not known yet.")
        console.info(f"  {what}")
        if not t.confirm("Trust this host key and add it to known_hosts?"):
            raise SftpHostKeyError(f"host key of {hostname} not trusted; nothing was changed")
        t.add_known_host(hostname, key)
        client.get_host_keys().add(hostname, key.get_name(), key)


@dataclass
class WalkResult:
    files: list[tuple[str, Any]] = field(default_factory=list)  # (relpath, SFTPAttributes)
    dirs: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)  # symlinks and other non-regular entries


class SftpTransport:
    """An SFTP session to one login. Connects lazily; use as a context manager."""

    def __init__(
        self,
        host: str,
        *,
        port: int | None = None,
        user: str | None = None,
        target_name: str = "",
        known_hosts: Path | None = None,
        ssh_config: Path | None = None,
        password_env_name: str | None = None,
        interactive: bool | None = None,
        confirm: Confirm | None = None,
        secret_prompt: SecretPrompt | None = None,
        allow_agent: bool = True,
        look_for_keys: bool = True,
        identity_files: tuple[str, ...] = (),
        password: str | None = None,
        allow_password_prompt: bool = True,
        env: dict[str, str] | None = None,
    ):
        self.endpoint = resolve_endpoint(host, port=port, user=user, ssh_config=ssh_config)
        if identity_files:
            self.endpoint = Endpoint(
                self.endpoint.alias,
                self.endpoint.host,
                self.endpoint.port,
                self.endpoint.user,
                tuple(identity_files) + self.endpoint.identity_files,
            )
        self.target_name = target_name
        self.known_hosts = known_hosts or Path.home() / ".ssh" / "known_hosts"
        self.password_env_name = password_env_name or (
            password_env(target_name) if target_name else None
        )
        self.interactive = _tty() if interactive is None else interactive
        self.confirm = confirm or _ask_yes
        self.secret_prompt = secret_prompt or getpass.getpass
        self.allow_agent = allow_agent
        self.look_for_keys = look_for_keys
        self.password = password
        self.allow_password_prompt = allow_password_prompt
        self.env = os.environ if env is None else env
        self._client: Any = None
        self._sftp: Any = None
        self._home: str | None = None

    @classmethod
    def for_target(cls, spec: TargetSpec, **kw: Any) -> SftpTransport:
        if spec.host is None:
            raise UsageError(f"deploy.targets.{spec.name}.host is required for sftp targets")
        port = spec.port if "port" in spec.given else None
        return cls(spec.host, port=port, user=spec.user, target_name=spec.name, **kw)

    # ------------------------------------------------------------------ connection
    def describe(self) -> str:
        ep = self.endpoint
        return f"sftp://{ep.user}@{ep.host}" + ("" if ep.port == 22 else f":{ep.port}")

    @property
    def login(self) -> str:
        ep = self.endpoint
        return f"{ep.user}@{ep.host}" + ("" if ep.port == 22 else f" (port {ep.port})")

    def add_known_host(self, hostname: str, key: Any) -> None:
        path = self.known_hosts
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        prefix = ""
        if path.is_file() and path.stat().st_size:
            with path.open("rb") as fh:
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    prefix = "\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{prefix}{hostname} {key.get_name()} {key.get_base64()}\n")
        with contextlib.suppress(OSError):
            path.chmod(0o600)
        console.info(f"  added {hostname} to {path}")

    def _env_password(self) -> str | None:
        if self.password is not None:
            return self.password
        if self.password_env_name:
            value = self.env.get(self.password_env_name)
            if value:
                return value
        return _PASSWORDS.get(self._cache_key)

    @property
    def _cache_key(self) -> tuple[str, str, int]:
        ep = self.endpoint
        return (ep.user, ep.host, ep.port)

    def _new_client(self) -> Any:
        import paramiko

        _quiet_paramiko()
        client = paramiko.SSHClient()
        if self.known_hosts.is_file():
            try:
                client.load_system_host_keys(str(self.known_hosts))
            except (OSError, paramiko.SSHException) as exc:
                raise SftpHostKeyError(f"cannot read {self.known_hosts}: {exc}") from None
        client.set_missing_host_key_policy(_HostKeyPolicy(self))
        return client

    def _try_connect(self, password: str | None, *, keys: bool) -> Any:
        import paramiko

        ep = self.endpoint
        client = self._new_client()
        key_files = [f for f in ep.identity_files if Path(f).is_file()] if keys else []
        try:
            client.connect(
                ep.host,
                port=ep.port,
                username=ep.user,
                password=password,
                key_filename=key_files or None,
                allow_agent=self.allow_agent and keys,
                look_for_keys=self.look_for_keys and keys,
                timeout=CONNECT_TIMEOUT,
                banner_timeout=CONNECT_TIMEOUT,
                auth_timeout=CONNECT_TIMEOUT,
            )
        except BaseException as exc:
            client.close()
            if isinstance(exc, SftpError):
                raise
            if isinstance(exc, paramiko.BadHostKeyException):
                name = known_hosts_name(ep.host, ep.port)
                raise SftpHostKeyError(
                    f"HOST KEY CHANGED for {name}: expected {fingerprint(exc.expected_key)},"
                    f" the server sent {exc.key.get_name()} {fingerprint(exc.key)}.\n"
                    "This can mean someone is intercepting the connection, or the server was"
                    " reinstalled. Ask your host for the new fingerprint; if it matches,"
                    f" remove the old entry with `ssh-keygen -R '{name}' -f {self.known_hosts}`"
                    " and connect again.",
                    changed=True,
                ) from None
            if isinstance(exc, paramiko.AuthenticationException) or (
                # raised before trying anything: no agent, no key file, no password
                isinstance(exc, paramiko.SSHException)
                and "no authentication methods" in str(exc).lower()
            ):
                raise SftpAuthError(str(exc) or "authentication failed") from None
            if isinstance(exc, (socket.timeout, socket.gaierror, ConnectionError, OSError)):
                raise SftpNetworkError(
                    f"cannot reach {ep.host} port {ep.port}: {exc}\n"
                    "Check the host name, port and network."
                ) from None
            if isinstance(exc, paramiko.SSHException):
                raise SftpError(f"SSH connection to {self.login} failed: {exc}") from None
            raise
        return client

    def connect(self) -> SftpTransport:
        if self._sftp is not None:
            return self
        password = self._env_password()
        console.detail(f"connecting to {self.describe()}")
        try:
            client = self._try_connect(password, keys=True)
        except SftpAuthError:
            if password is not None or not (self.interactive and self.allow_password_prompt):
                ways = [f"an SSH key (`ym ssh-setup {self.target_name or '<target>'}`)"]
                if self.password_env_name and password is None:
                    ways.append(f"the password in ${self.password_env_name}")
                if password is None:
                    ways.append("a terminal (ym asks for the password)")
                raise SftpAuthError(
                    f"SFTP login to {self.login} failed: "
                    + ("the password was rejected" if password else "no key was accepted")
                    + ".\nUse "
                    + ", or ".join(ways)
                    + "."
                ) from None
            password = self.secret_prompt(f"SFTP password for {self.login}: ")
            try:
                client = self._try_connect(password, keys=False)
            except SftpAuthError:
                raise SftpAuthError(f"SFTP login to {self.login} failed: wrong password") from None
            _PASSWORDS[self._cache_key] = password
        try:
            self._sftp = client.open_sftp()
        except Exception as exc:
            client.close()
            raise SftpError(f"{self.login}: SFTP subsystem not available: {exc}") from None
        self._client = client
        return self

    def close(self) -> None:
        if self._sftp is not None:
            with contextlib.suppress(Exception):
                self._sftp.close()
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
        self._sftp = self._client = None

    def __enter__(self) -> SftpTransport:
        return self.connect()

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def sftp(self) -> paramiko.SFTPClient:
        if self._sftp is None:
            self.connect()
        return self._sftp

    # ------------------------------------------------------------------ paths
    def home(self) -> str:
        """The SFTP login directory (what relative target paths are relative to)."""
        if self._home is None:
            with self._io("normalize", "."):
                self._home = self.sftp.normalize(".")
        return self._home

    normalize = home

    def abspath(self, path: str) -> str:
        path = to_home_relative(path)
        if path.startswith("/"):
            return posixpath.normpath(path)
        return posixpath.normpath(posixpath.join(self.home(), path))

    @contextlib.contextmanager
    def _io(self, op: str, path: str) -> Iterator[None]:
        try:
            yield
        except OSError as exc:
            detail = exc.strerror or str(exc) or type(exc).__name__
            raise YMError(f"[{self.describe()}] {op} {path}: {detail}") from None

    # ------------------------------------------------------------------ queries
    def stat(self, path: str) -> Any | None:
        """SFTPAttributes (following symlinks), or None when the path does not exist."""
        try:
            return self.sftp.stat(path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if getattr(exc, "errno", None) == 2:
                return None
            raise YMError(f"[{self.describe()}] stat {path}: {exc}") from None

    def exists(self, path: str) -> bool:
        return self.stat(path) is not None

    def is_file(self, path: str) -> bool:
        st = self.stat(path)
        return st is not None and stat.S_ISREG(st.st_mode or 0)

    def is_dir(self, path: str) -> bool:
        st = self.stat(path)
        return st is not None and stat.S_ISDIR(st.st_mode or 0)

    def listdir(self, path: str) -> list[Any]:
        """SFTPAttributes (lstat) of the entries in ``path``, sorted by name."""
        with self._io("listdir", path):
            return sorted(self.sftp.listdir_attr(path), key=lambda a: a.filename)

    def walk(self, root: str, skip: PathFilter | None = None) -> WalkResult:
        """Every regular file under ``root`` as (posix relpath, attrs), sorted.

        ``skip(relpath)`` prunes files and whole directories. Symlinks are never followed
        (they are listed in ``links``).
        """
        res = WalkResult()
        stack: list[str] = [""]
        while stack:
            rel_dir = stack.pop()
            for attr in self.listdir(posixpath.join(root, rel_dir) if rel_dir else root):
                rel = f"{rel_dir}/{attr.filename}" if rel_dir else attr.filename
                if skip is not None and skip(rel):
                    continue
                mode = attr.st_mode or 0
                if stat.S_ISDIR(mode):
                    res.dirs.append(rel)
                    stack.append(rel)
                elif stat.S_ISREG(mode):
                    res.files.append((rel, attr))
                else:
                    res.links.append(rel)
        res.files.sort(key=lambda t: t[0])
        res.dirs.sort()
        res.links.sort()
        return res

    # ------------------------------------------------------------------ reads
    def read_bytes(self, path: str) -> bytes:
        with self._io("read", path), self.sftp.open(path, "rb") as fh:
            fh.prefetch()
            return fh.read()

    def download(self, remote: str, local: Path) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        with self._io("download", remote):
            self.sftp.get(remote, str(local))

    # ------------------------------------------------------------------ writes
    def mkdirs(self, path: str, mode: int | None = None) -> None:
        """``mkdir -p``; ``mode`` applies to directories created here."""
        path = self.abspath(path)
        parts = [p for p in path.split("/") if p]
        current = "/"
        for part in parts:
            current = posixpath.join(current, part)
            st = self.stat(current)
            if st is not None:
                if not stat.S_ISDIR(st.st_mode or 0):
                    raise YMError(f"[{self.describe()}] {current} exists and is not a directory")
                continue
            with self._io("mkdir", current):
                if mode is None:
                    self.sftp.mkdir(current)
                else:
                    self.sftp.mkdir(current, mode)
                    self.sftp.chmod(current, mode)  # mkdir's mode is subject to the umask

    def _atomic(self, path: str, write: Callable[[Any], None], mode: int) -> None:
        directory, name = posixpath.split(path)
        tmp = posixpath.join(directory, f".{name}{TMP_SUFFIX}")
        sftp = self.sftp
        try:
            with self._io("write", tmp), sftp.open(tmp, "wb") as fh:
                fh.chmod(mode)  # before any data: secrets are never readable by others
                fh.set_pipelined(True)
                write(fh)
            with self._io("rename", path):
                try:
                    sftp.posix_rename(tmp, path)
                except OSError:
                    # No posix-rename@openssh.com: replace non-atomically.
                    with contextlib.suppress(OSError):
                        sftp.remove(path)
                    sftp.rename(tmp, path)
        except BaseException:
            with contextlib.suppress(Exception):
                sftp.remove(tmp)
            raise

    def write_bytes(self, path: str, data: bytes, mode: int = 0o644) -> None:
        self._atomic(path, lambda fh: fh.write(data), mode)

    def upload(self, local: Path, remote: str, mode: int = 0o644) -> None:
        """Atomic upload. Web files get 0644 regardless of local permissions (a checkout's
        modes vary by OS and umask; PHP files never need the executable bit)."""

        def write(fh: Any) -> None:
            with local.open("rb") as src:
                for chunk in iter(lambda: src.read(1 << 16), b""):
                    fh.write(chunk)

        self._atomic(remote, write, mode)

    def remove(self, path: str) -> None:
        with self._io("remove", path):
            self.sftp.remove(path)

    def rmdir(self, path: str) -> None:
        with self._io("rmdir", path):
            self.sftp.rmdir(path)

    def chmod(self, path: str, mode: int) -> None:
        with self._io("chmod", path):
            self.sftp.chmod(path, mode)


def check_sftp(spec: TargetSpec, identity: Path | None = None, **kw: Any) -> tuple[str, str]:
    """Non-interactive login check: returns (reason, detail); reason "ok" on success."""
    kw.setdefault("interactive", False)
    if identity is not None:
        kw["identity_files"] = (str(identity),)
    t = SftpTransport.for_target(spec, **kw)
    try:
        with t:
            t.home()
    except SftpError as exc:
        return exc.reason, str(exc)
    return "ok", ""


__all__ = [
    "SftpAuthError",
    "SftpError",
    "SftpHostKeyError",
    "SftpNetworkError",
    "SftpTransport",
    "check_sftp",
    "fingerprint",
    "password_env",
]
