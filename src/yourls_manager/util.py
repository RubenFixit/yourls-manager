"""Shared errors, console output and small helpers."""

from __future__ import annotations

import datetime as _dt
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_UPDATES = 10


class YMError(Exception):
    """An operational error; maps to exit code 1."""

    exit_code = EXIT_ERROR


class UsageError(YMError):
    """A usage or configuration error; maps to exit code 2."""

    exit_code = EXIT_USAGE


class _Console:
    verbose = False

    def info(self, msg: str = "") -> None:
        print(msg, flush=True)

    def detail(self, msg: str) -> None:
        if self.verbose:
            print(f"  {msg}", flush=True)

    def warn(self, msg: str) -> None:
        print(f"warning: {msg}", file=sys.stderr, flush=True)

    def error(self, msg: str) -> None:
        print(f"error: {msg}", file=sys.stderr, flush=True)

    def step(self, msg: str) -> None:
        print(f"==> {msg}", flush=True)


console = _Console()


def utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0)


def iso_utc(when: _dt.datetime | None = None) -> str:
    return (when or utc_now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def compact_utc(when: _dt.datetime | None = None) -> str:
    return (when or utc_now()).strftime("%Y%m%dT%H%M%SZ")


def run(
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    input: bytes | None = None,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    """Run a command without a shell. Raises YMError on failure when check=True."""
    full_env = None
    if env is not None:
        full_env = dict(os.environ)
        full_env.update(env)
    console.detail("$ " + " ".join(str(a) for a in argv))
    try:
        proc = subprocess.run(
            [str(a) for a in argv],
            cwd=cwd,
            env=full_env,
            input=input,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            check=False,
        )
    except FileNotFoundError as exc:
        raise YMError(f"command not found: {argv[0]}") from exc
    if check and proc.returncode != 0:
        detail = ""
        if capture:
            detail = (proc.stderr or b"").decode(errors="replace").strip() or (
                proc.stdout or b""
            ).decode(errors="replace").strip()
        raise YMError(
            f"command failed ({proc.returncode}): {' '.join(str(a) for a in argv)}"
            + (f"\n{detail}" if detail else "")
        )
    return proc


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True
