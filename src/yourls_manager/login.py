"""`ym target login` (formerly ``ym ssh-setup``): make ym's non-interactive login work.

ym always runs ssh with ``BatchMode=yes`` (a deploy opens many connections; a password
prompt in the middle of rsync would hang), so targets need key authentication. This
command checks it and, when it fails, walks through creating a key (``ssh-keygen``) and
installing it on the server (``ssh-copy-id``, which asks for the password once).

SFTP targets are checked with an SFTP connection instead of a shell command
(SFTP-only accounts have no shell). A password from ``YM_<TARGET>_PASSWORD`` is a valid
login there, so a key is optional; when wanted, it is installed by logging in with the
password over SFTP and appending it to ``.ssh/authorized_keys`` (``.ssh`` 700, file 600).
"""

from __future__ import annotations

import os
import posixpath
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import TargetSpec
from .remote import SshRunner
from .util import UsageError, YMError, console

if TYPE_CHECKING:  # pragma: no cover
    from .transport.sftp import SftpTransport

TransportFactory = Callable[..., "SftpTransport"]

# Runs a command attached to the terminal (password / passphrase prompts); returns exit code.
Interactive = Callable[[Sequence[str]], int]
Confirm = Callable[[str], bool]


@dataclass(frozen=True)
class LoginCheck:
    ok: bool
    reason: str  # "ok" | "auth" | "hostkey" | "network" | "other"
    detail: str = ""


def classify(returncode: int, stderr: str) -> LoginCheck:
    if returncode == 0:
        return LoginCheck(True, "ok")
    err = stderr.strip()
    low = err.lower()
    if "host key verification failed" in low or "remote host identification has changed" in low:
        return LoginCheck(False, "hostkey", err)
    if "permission denied" in low:
        return LoginCheck(False, "auth", err)
    if any(
        s in low
        for s in (
            "could not resolve",
            "connection timed out",
            "connection refused",
            "no route to host",
            "network is unreachable",
            "operation timed out",
        )
    ):
        return LoginCheck(False, "network", err)
    return LoginCheck(False, "other", err)


def check_login(
    spec: TargetSpec, identity: Path | None = None, *, password: bool = False
) -> LoginCheck:
    """Try a non-interactive connection the same way every other ym command does
    (optionally forcing one key with ``-i``). For sftp targets an SFTP session is opened;
    ``password=True`` also accepts a password from ``YM_<TARGET>_PASSWORD``."""
    if spec.is_sftp:
        from .transport.sftp import check_sftp

        kw: dict[str, Any] = {} if password else {"env": {}}
        reason, detail = check_sftp(spec, identity, **kw)
        return LoginCheck(reason == "ok", reason, detail)
    if spec.is_local:
        return LoginCheck(True, "ok")
    assert spec.host is not None
    runner = SshRunner(spec.host, spec.user, spec.port)
    try:
        proc = subprocess.run(
            [
                *runner.ssh_base(),
                *(["-i", str(identity)] if identity else []),
                runner.destination,
                "--",
                "true",
            ],
            input=b"",
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise UsageError("ssh not found on this machine (install OpenSSH)") from exc
    return classify(proc.returncode, proc.stderr.decode(errors="replace"))


def default_key() -> Path:
    return Path.home() / ".ssh" / "id_ed25519"


def _terminal(argv: Sequence[str]) -> int:
    console.info(f"$ {' '.join(argv)}")
    try:
        return subprocess.run(list(argv), check=False).returncode
    except FileNotFoundError:
        return 127


def login(
    spec: TargetSpec,
    *,
    confirm: Confirm,
    interactive: Interactive = _terminal,
    key: Path | None = None,
    check: Callable[..., LoginCheck] = check_login,
    transport: TransportFactory | None = None,
) -> bool:
    """Return True when key authentication to ``spec`` works afterwards."""
    if spec.is_sftp:
        return sftp_setup(
            spec,
            confirm=confirm,
            interactive=interactive,
            key=key,
            check=check,
            transport=transport,
        )
    if spec.is_local:
        console.info(f"target {spec.name} is local; no SSH needed")
        return True
    runner = SshRunner(spec.host or "", spec.user, spec.port)
    dest = runner.destination
    console.step(f"check ssh key login to {dest} (port {spec.port})")
    res = check(spec)
    if res.ok:
        console.info("  key authentication works")
        return True
    if res.reason == "network":
        raise YMError(f"cannot reach {dest}: {res.detail}\nCheck the host name, port and network.")
    if res.reason not in ("auth", "hostkey"):
        raise YMError(f"ssh to {dest} failed: {res.detail}")
    console.info(
        "  key authentication is not set up yet"
        if res.reason == "auth"
        else "  the server's host key is not trusted yet (first connection)"
    )

    key = key or default_key()
    pub = key.with_name(key.name + ".pub")
    if not _ensure_key(key, confirm, interactive):
        _manual_help(dest, spec.port, key)
        return False

    if not confirm(
        f"Install {pub.name} on {dest}? You will be asked for the SSH password once"
        " (and to trust the host key on first connect)."
    ):
        _manual_help(dest, spec.port, key)
        return False
    if shutil.which("ssh-copy-id"):
        code = interactive(["ssh-copy-id", "-i", str(pub), "-p", str(spec.port), dest])
    else:  # e.g. Windows OpenSSH: append the key ourselves
        console.info("  ssh-copy-id not found; appending the key with ssh")
        code = interactive(
            [
                "ssh",
                "-p",
                str(spec.port),
                dest,
                f"umask 077; mkdir -p ~/.ssh && printf '%s\\n' {_sq(pub.read_text().strip())}"
                " >> ~/.ssh/authorized_keys",
            ]
        )
    if code != 0:
        raise YMError("installing the key failed (see the output above)")

    res = check(spec)
    if res.ok:
        console.info("  key authentication works")
        return True
    if res.reason == "auth":
        if check(spec, key).ok:
            raise YMError(
                f"the key works with `ssh -i {key}`, but ssh does not use it by default.\n"
                "Add this to ~/.ssh/config, then run `ym target login` again:\n"
                f"  Host {spec.host}\n    IdentityFile {key}"
            )
        raise YMError(
            f"the key was installed but key login to {dest} still fails.\n"
            "If the key has a passphrase, load it into an agent first:\n"
            '  eval "$(ssh-agent -s)" && ssh-add\n'
            f"{res.detail}"
        )
    raise YMError(f"ssh to {dest} still fails: {res.detail}")


def _ensure_key(key: Path, confirm: Confirm, interactive: Interactive) -> bool:
    """Make sure ``key``/``key.pub`` exist (offering ssh-keygen). False when declined."""
    pub = key.with_name(key.name + ".pub")
    if pub.is_file():
        console.info(f"  using existing key {pub}")
        return True
    if not confirm(f"No SSH key at {key}. Create one now (ssh-keygen -t ed25519)?"):
        return False
    key.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    console.info(
        "  ssh-keygen asks for a passphrase. With a passphrase, load the key into an"
        " agent before running ym (ssh-add); leave it empty for unattended use."
    )
    comment = f"yourls-manager@{os.uname().nodename}" if hasattr(os, "uname") else "ym"
    if interactive(["ssh-keygen", "-t", "ed25519", "-f", str(key), "-C", comment]) != 0:
        raise YMError("ssh-keygen failed")
    return True


# --------------------------------------------------------------------------- sftp


def install_key_sftp(t: SftpTransport, public_key: str) -> bool:
    """Append ``public_key`` to ``.ssh/authorized_keys`` in the SFTP login dir.

    Creates ``.ssh`` (0700) and the file (0600) as needed. Returns False when the key was
    already there."""
    line = public_key.strip()
    if not line or "\n" in line:
        raise UsageError("the public key must be a single line")
    blob = line.split()[1] if len(line.split()) > 1 else line
    ssh_dir = t.abspath(".ssh")
    if not t.is_dir(ssh_dir):
        t.mkdirs(ssh_dir, 0o700)
    path = posixpath.join(ssh_dir, "authorized_keys")
    existing = t.read_bytes(path).decode("utf-8", "replace") if t.is_file(path) else ""
    for old in existing.splitlines():
        parts = old.split()
        if blob in parts:
            console.info(f"  the key is already in {path}")
            return False
    if existing and not existing.endswith("\n"):
        existing += "\n"
    t.write_bytes(path, (existing + line + "\n").encode("utf-8"), 0o600)
    console.info(f"  added the key to {path}")
    return True


def sftp_setup(
    spec: TargetSpec,
    *,
    confirm: Confirm,
    interactive: Interactive = _terminal,
    key: Path | None = None,
    check: Callable[..., LoginCheck] = check_login,
    transport: TransportFactory | None = None,
) -> bool:
    """`target login` for sftp targets: everything happens over SFTP (no shell needed)."""
    from .transport.sftp import SftpAuthError, SftpTransport, password_env

    factory: TransportFactory = transport or SftpTransport.for_target
    probe = factory(spec, interactive=False, env={})
    login = probe.login
    console.step(f"check SFTP key login to {login}")
    res = check(spec)
    if res.ok:
        console.info("  key authentication works")
        return True
    if res.reason == "network":
        raise YMError(res.detail)
    if res.reason == "hostkey":
        if "CHANGED" in res.detail:
            raise YMError(res.detail)
        console.info("  the server's host key is not trusted yet (first connection)")
        t = factory(spec, interactive=True, confirm=confirm, allow_password_prompt=False)
        try:
            with t:
                t.home()
        except SftpAuthError:
            pass  # host key is trusted now; key login is set up below
        res = check(spec)
        if res.ok:
            console.info("  key authentication works")
            return True
    if res.reason != "auth":
        raise YMError(f"SFTP login to {login} failed: {res.detail}")
    console.info("  key authentication is not set up yet")

    key = key or default_key()
    pub = key.with_name(key.name + ".pub")
    env_name = password_env(spec.name)
    if os.environ.get(env_name) and check(spec, password=True).ok:
        console.info(f"  password login works (${env_name}); an SSH key is optional")
        if not confirm("Set up an SSH key anyway, so no password is needed?"):
            return True
    if not _ensure_key(key, confirm, interactive):
        _manual_help_sftp(login, pub, env_name)
        return False
    if not confirm(
        f"Install {pub.name} on {login} over SFTP? You will be asked for the SFTP password"
        f" once (or set ${env_name})."
    ):
        _manual_help_sftp(login, pub, env_name)
        return False
    with factory(spec, interactive=True, confirm=confirm) as t:
        install_key_sftp(t, pub.read_text(encoding="utf-8"))

    res = check(spec)
    if res.ok:
        console.info("  key authentication works")
        return True
    if res.reason == "auth" and check(spec, key).ok:
        raise YMError(
            f"the key works when given explicitly, but ym does not use {key} by default.\n"
            "Add this to ~/.ssh/config, then run `ym target login` again:\n"
            f"  Host {spec.host}\n    IdentityFile {key}"
        )
    raise YMError(
        f"the key was installed but key login to {login} still fails.\n"
        "Some hosts read authorized_keys from another place for SFTP-only (chrooted)"
        " accounts; ask your host how to add an SSH key. If the key has a passphrase, load"
        ' it into an agent first: eval "$(ssh-agent -s)" && ssh-add\n'
        f"{res.detail}"
    )


def _manual_help_sftp(login: str, pub: Path, env_name: str) -> None:
    console.info("To set it up yourself:")
    console.info(f"  ssh-keygen -t ed25519 -f {pub.with_suffix('')}")
    console.info(
        f"  add the line in {pub} to .ssh/authorized_keys in the SFTP login dir of {login}"
        " (.ssh mode 700, file mode 600), or ask your host to add it"
    )
    console.info(f"Or use the password: set ${env_name} (ym never stores it).")


def _sq(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


def _manual_help(dest: str, port: int, key: Path) -> None:
    console.info("To set it up yourself:")
    console.info(f"  ssh-keygen -t ed25519 -f {key}")
    console.info(f"  ssh-copy-id -i {key}.pub -p {port} {dest}")
    console.info(f"  ssh -p {port} -o BatchMode=yes {dest} true   # must succeed without a prompt")
