"""`ym ssh-setup`: make SSH key authentication to a target work.

ym always runs ssh with ``BatchMode=yes`` (a deploy opens many connections; a password
prompt in the middle of rsync would hang), so targets need key authentication. This
command checks it and, when it fails, walks through creating a key (``ssh-keygen``) and
installing it on the server (``ssh-copy-id``, which asks for the password once).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import TargetSpec
from .remote import SshRunner
from .util import UsageError, YMError, console

# Runs a command attached to the terminal (password / passphrase prompts); returns exit code.
Interactive = Callable[[Sequence[str]], int]
Confirm = Callable[[str], bool]


@dataclass(frozen=True)
class SshCheck:
    ok: bool
    reason: str  # "ok" | "auth" | "hostkey" | "network" | "other"
    detail: str = ""


def classify(returncode: int, stderr: str) -> SshCheck:
    if returncode == 0:
        return SshCheck(True, "ok")
    err = stderr.strip()
    low = err.lower()
    if "host key verification failed" in low or "remote host identification has changed" in low:
        return SshCheck(False, "hostkey", err)
    if "permission denied" in low:
        return SshCheck(False, "auth", err)
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
        return SshCheck(False, "network", err)
    return SshCheck(False, "other", err)


def check_ssh(spec: TargetSpec, identity: Path | None = None) -> SshCheck:
    """Try a non-interactive connection the same way every other ym command does
    (optionally forcing one key with ``-i``)."""
    if spec.is_local:
        return SshCheck(True, "ok")
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


def ssh_setup(
    spec: TargetSpec,
    *,
    confirm: Confirm,
    interactive: Interactive = _terminal,
    key: Path | None = None,
    check: Callable[..., SshCheck] = check_ssh,
) -> bool:
    """Return True when key authentication to ``spec`` works afterwards."""
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
    if not pub.is_file():
        if not confirm(f"No SSH key at {key}. Create one now (ssh-keygen -t ed25519)?"):
            _manual_help(dest, spec.port, key)
            return False
        key.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        console.info(
            "  ssh-keygen asks for a passphrase. With a passphrase, load the key into an"
            " agent before running ym (ssh-add); leave it empty for unattended use."
        )
        comment = f"yourls-manager@{os.uname().nodename}" if hasattr(os, "uname") else "ym"
        if interactive(["ssh-keygen", "-t", "ed25519", "-f", str(key), "-C", comment]) != 0:
            raise YMError("ssh-keygen failed")
    else:
        console.info(f"  using existing key {pub}")

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
                "Add this to ~/.ssh/config, then run `ym ssh-setup` again:\n"
                f"  Host {spec.host}\n    IdentityFile {key}"
            )
        raise YMError(
            f"the key was installed but key login to {dest} still fails.\n"
            "If the key has a passphrase, load it into an agent first:\n"
            '  eval "$(ssh-agent -s)" && ssh-add\n'
            f"{res.detail}"
        )
    raise YMError(f"ssh to {dest} still fails: {res.detail}")


def _sq(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


def _manual_help(dest: str, port: int, key: Path) -> None:
    console.info("To set it up yourself:")
    console.info(f"  ssh-keygen -t ed25519 -f {key}")
    console.info(f"  ssh-copy-id -i {key}.pub -p {port} {dest}")
    console.info(f"  ssh -p {port} -o BatchMode=yes {dest} true   # must succeed without a prompt")
