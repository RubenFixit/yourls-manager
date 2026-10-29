"""Run commands on a target: locally (host omitted) or over ssh.

Every command is built as a single shell string with ``shlex.quote`` applied to each
argument, then executed by ``sh -c`` locally or handed to ``ssh -- <cmd>`` remotely, so
quoting behaves identically in both modes. Relative paths are relative to ``$HOME``:
remote shells start there, and local commands are run with ``cwd=$HOME``.
"""

from __future__ import annotations

import posixpath
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from .config import TargetSpec
from .util import YMError, console


def shell_join(argv: Sequence[str], env: Mapping[str, str] | None = None) -> str:
    """Quote argv (with optional leading ``env K=V``) into one POSIX shell string."""
    parts = [str(a) for a in argv]
    if env:
        parts = ["env", *(f"{k}={v}" for k, v in env.items()), *parts]
    return shlex.join(parts)


def to_home_relative(path: str) -> str:
    """``~/x`` -> ``x``; ``~`` -> ``.``; others unchanged (quoting would break ``~``)."""
    if path in ("~", "~/"):
        return "."
    if path.startswith("~/"):
        return path[2:]
    return path


class Runner:
    is_local: bool = True

    def __init__(self) -> None:
        self._home: str | None = None

    # -- primitives ----------------------------------------------------------------
    def _exec(self, script: str, input: bytes | None) -> subprocess.CompletedProcess[bytes]:
        raise NotImplementedError

    def describe(self) -> str:
        raise NotImplementedError

    def rsync_dest(self, path: str) -> str:
        raise NotImplementedError

    def rsync_transport(self) -> list[str]:
        return []

    def home(self) -> str:
        raise NotImplementedError

    # -- helpers -------------------------------------------------------------------
    def run_script(
        self, script: str, *, input: bytes | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[bytes]:
        console.detail(f"[{self.describe()}] $ {script}")
        try:
            proc = self._exec(script, input)
        except FileNotFoundError as exc:
            raise YMError(f"command not found: {exc.filename}") from exc
        if check and proc.returncode != 0:
            err = proc.stderr.decode(errors="replace").strip()
            out = proc.stdout.decode(errors="replace").strip()
            raise YMError(
                f"[{self.describe()}] command failed ({proc.returncode}): {script}"
                + (f"\n{err or out}" if (err or out) else "")
                + self._failure_hint(proc.returncode, err)
            )
        return proc

    def _failure_hint(self, returncode: int, stderr: str) -> str:
        return ""

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        input: bytes | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        return self.run_script(shell_join(argv, env), input=input, check=check)

    def abspath(self, path: str) -> str:
        path = to_home_relative(path)
        if path.startswith("/"):
            return posixpath.normpath(path)
        return posixpath.normpath(posixpath.join(self.home(), path))

    def exists(self, path: str, kind: str = "-e") -> bool:
        return self.run(["test", kind, path], check=False).returncode == 0

    def mkdir(self, path: str) -> None:
        self.run(["mkdir", "-p", "--", path])

    def write_file(self, path: str, data: bytes, mode: str = "0644") -> None:
        script = shell_join(
            ["sh", "-c", 'umask 077 && cat > "$1" && chmod "$2" "$1"', "sh", path, mode]
        )
        self.run_script(script, input=data)


class LocalRunner(Runner):
    is_local = True

    def describe(self) -> str:
        return "local"

    def home(self) -> str:
        if self._home is None:
            self._home = str(Path.home())
        return self._home

    def _exec(self, script: str, input: bytes | None) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["sh", "-c", script],
            cwd=self.home(),
            input=input,
            capture_output=True,
            check=False,
        )

    def rsync_dest(self, path: str) -> str:
        return self.abspath(path)


class SshRunner(Runner):
    is_local = False

    def __init__(self, host: str, user: str | None = None, port: int = 22):
        super().__init__()
        self.host = host
        self.user = user
        self.port = port

    @property
    def destination(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    def describe(self) -> str:
        return self.destination

    def ssh_base(self) -> list[str]:
        return [
            "ssh",
            "-p",
            str(self.port),
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=20",
        ]

    def _failure_hint(self, returncode: int, stderr: str) -> str:
        low = stderr.lower()
        if returncode != 255:
            return ""
        if "permission denied" in low:
            return (
                "\nym needs SSH key authentication (it never prompts for passwords)."
                " Set it up with: ym target login <target>"
            )
        if "host key verification failed" in low:
            return (
                "\nThe server's host key is not trusted yet. Run `ym target login <target>`"
                f" or connect once with `ssh -p {self.port} {self.destination}`."
            )
        return ""

    def ssh_argv(self, script: str) -> list[str]:
        return [*self.ssh_base(), self.destination, "--", script]

    def _exec(self, script: str, input: bytes | None) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            self.ssh_argv(script),
            input=input if input is not None else b"",
            capture_output=True,
            check=False,
        )

    def home(self) -> str:
        if self._home is None:
            out = self.run_script('printf %s "$HOME"').stdout.decode().strip()
            if not out.startswith("/"):
                raise YMError(f"[{self.describe()}] could not determine remote $HOME")
            self._home = out
        return self._home

    def rsync_transport(self) -> list[str]:
        return ["-e", shlex.join(self.ssh_base())]

    def rsync_dest(self, path: str) -> str:
        # Paths are validated against a conservative charset in config, so no remote-shell
        # word splitting can occur regardless of rsync version.
        return f"{self.destination}:{self.abspath(path)}"


def runner_for(target: TargetSpec) -> Runner:
    if target.is_local:
        return LocalRunner()
    assert target.host is not None
    return SshRunner(target.host, target.user, target.port)
