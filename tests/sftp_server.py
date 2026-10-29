"""A real OpenSSH server on 127.0.0.1 for the SFTP tests.

Two accounts:

- ``ymsftp``: an ordinary account (shell, home ``/home/ymsftp``);
- ``ymsftponly``: SFTP-only (``ForceCommand internal-sftp``, no shell) and chrooted to
  ``/srv/ym-chroot/ymsftponly``; its login dir is ``/home/ymsftponly`` inside the chroot.
  Proves that nothing ym does over SFTP needs a shell.

Needs root (useradd, chpasswd, sshd) and ``/usr/sbin/sshd``; the fixtures skip otherwise.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

SSHD = Path("/usr/sbin/sshd")
HOST_KEY = Path("/etc/ssh/ssh_host_ed25519_key")
CHROOT_BASE = Path("/srv/ym-chroot")
PASSWORD = "ym-Test-pa55word"
SHELL_USER = "ymsftp"
SFTP_ONLY_USER = "ymsftponly"


def unavailable_reason() -> str | None:
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return "needs root (creates users and runs sshd)"
    for p in (SSHD, HOST_KEY):
        if not p.exists():
            return f"{p} not found"
    for tool in ("useradd", "chpasswd", "ssh-keygen"):
        if shutil.which(tool) is None:
            return f"{tool} not found"
    return None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run(*argv: str, input: str | None = None) -> None:
    subprocess.run(argv, check=True, capture_output=True, text=True, input=input)


def host_public_key() -> str:
    """``ssh-ed25519 AAAA...`` of the server's host key."""
    parts = HOST_KEY.with_suffix(".pub").read_text().split()
    return f"{parts[0]} {parts[1]}"


@dataclass
class Account:
    user: str
    port: int
    chroot: Path | None  # real path of the chroot dir (SFTP-only account)
    login_dir: str  # login directory as seen over SFTP
    password: str = PASSWORD
    host: str = "127.0.0.1"

    def real(self, sftp_path: str = "") -> Path:
        """Disk path of an SFTP path (absolute, or relative to the login dir)."""
        p = sftp_path if sftp_path.startswith("/") else f"{self.login_dir}/{sftp_path}"
        p = os.path.normpath(p)
        return Path(str(self.chroot) + p) if self.chroot else Path(p)

    @property
    def home(self) -> Path:
        return self.real(self.login_dir)

    def chown(self, path: Path | None = None) -> None:
        _run("chown", "-R", f"{self.user}:{self.user}", str(path or self.home))

    def reset(self) -> None:
        """Empty the login dir (keeps the dir itself, owned by the user)."""
        home = self.home
        home.mkdir(parents=True, exist_ok=True)
        for child in home.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        home.chmod(0o755)
        self.chown(home)

    def write(self, rel: str, content: str | bytes, mode: int = 0o644) -> Path:
        path = self.real(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
        path.chmod(mode)
        self.chown(self.home)
        return path

    def authorize(self, public_key: str) -> None:
        self.write(".ssh/authorized_keys", public_key.strip() + "\n", 0o600)
        self.real(".ssh").chmod(0o700)

    def target_toml(self, name: str = "prod", **extra: str) -> str:
        lines = [
            f"[deploy.targets.{name}]",
            'type = "sftp"',
            f'host = "{self.host}"',
            f'user = "{self.user}"',
            f"port = {self.port}",
        ]
        lines += [f"{k} = {v}" for k, v in extra.items()]
        return "\n".join(lines) + "\n"


SSHD_CONFIG = """\
Port {port}
ListenAddress 127.0.0.1
HostKey {host_key}
PidFile {pid}
UsePAM yes
PasswordAuthentication yes
KbdInteractiveAuthentication no
PubkeyAuthentication yes
PermitRootLogin no
AuthorizedKeysFile .ssh/authorized_keys
AllowUsers {shell_user} {sftp_user}
StrictModes yes
LogLevel VERBOSE
Subsystem sftp internal-sftp

Match User {sftp_user}
    ForceCommand internal-sftp
    ChrootDirectory {chroot_base}/%u
    AuthorizedKeysFile {chroot_base}/%u/home/%u/.ssh/authorized_keys
    AllowTcpForwarding no
    X11Forwarding no
"""


class SshServer:
    def __init__(self, workdir: Path):
        self.workdir = workdir
        self.port = free_port()
        self.proc: subprocess.Popen | None = None
        self.log = workdir / "sshd.log"

    def _ensure_users(self) -> None:
        for user, shell, home in (
            (SHELL_USER, "/bin/sh", f"/home/{SHELL_USER}"),
            (SFTP_ONLY_USER, "/usr/sbin/nologin", f"/home/{SFTP_ONLY_USER}"),
        ):
            if subprocess.run(["id", user], capture_output=True).returncode != 0:
                _run("useradd", "--no-create-home", "-d", home, "-s", shell, user)
            _run("chpasswd", input=f"{user}:{PASSWORD}\n")
        Path(f"/home/{SHELL_USER}").mkdir(exist_ok=True)
        # chroot dirs must be root-owned and not group/world writable
        chroot = CHROOT_BASE / SFTP_ONLY_USER
        for d in (CHROOT_BASE, chroot, chroot / "home"):
            d.mkdir(exist_ok=True)
            os.chown(d, 0, 0)
            d.chmod(0o755)
        (chroot / "home" / SFTP_ONLY_USER).mkdir(exist_ok=True)

    def start(self) -> None:
        self._ensure_users()
        Path("/run/sshd").mkdir(exist_ok=True)
        conf = self.workdir / "sshd_config"
        conf.write_text(
            SSHD_CONFIG.format(
                port=self.port,
                host_key=HOST_KEY,
                pid=self.workdir / "sshd.pid",
                shell_user=SHELL_USER,
                sftp_user=SFTP_ONLY_USER,
                chroot_base=CHROOT_BASE,
            )
        )
        test = subprocess.run([str(SSHD), "-t", "-f", str(conf)], capture_output=True, text=True)
        if test.returncode != 0:
            raise RuntimeError(f"sshd -t failed: {test.stderr}")
        self.proc = subprocess.Popen(
            [str(SSHD), "-D", "-f", str(conf), "-E", str(self.log)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"sshd exited: {self.log.read_text()}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("sshd did not start listening")

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None

    def account(self, sftp_only: bool) -> Account:
        if sftp_only:
            acct = Account(
                SFTP_ONLY_USER,
                self.port,
                CHROOT_BASE / SFTP_ONLY_USER,
                f"/home/{SFTP_ONLY_USER}",
            )
        else:
            acct = Account(SHELL_USER, self.port, None, f"/home/{SHELL_USER}")
        acct.reset()
        return acct

    def known_hosts_line(self) -> str:
        return f"[127.0.0.1]:{self.port} {host_public_key()}\n"
