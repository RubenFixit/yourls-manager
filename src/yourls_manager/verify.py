"""`ym verify`: health endpoint + redirect expectations from tests/redirects.toml."""

from __future__ import annotations

import json
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .config import ConfigError
from .util import YMError, console

USER_AGENT = f"yourls-manager/{__version__} (+https://github.com/RubenFixit/yourls-manager)"


@dataclass(frozen=True)
class Case:
    path: str
    status: tuple[int, ...]
    location: str | None = None


@dataclass
class Response:
    status: int
    location: str | None
    body: bytes


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def fetch(url: str, timeout: float = 15.0) -> Response:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return Response(resp.status, resp.headers.get("Location"), resp.read(65536))
    except urllib.error.HTTPError as err:
        body = err.read(65536) if err.fp else b""
        return Response(err.code, err.headers.get("Location"), body)
    except (urllib.error.URLError, OSError) as err:
        raise YMError(f"GET {url} failed: {getattr(err, 'reason', err)}") from None


def load_cases(path: Path) -> list[Case]:
    if not path.exists():
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError("case", f"invalid TOML: {exc}", file=str(path)) from None
    raw_cases = data.get("case", [])
    if not isinstance(raw_cases, list):
        raise ConfigError("case", "must be an array of tables ([[case]])", file=str(path))
    cases = []
    for i, raw in enumerate(raw_cases):
        cases.append(_parse_case(i, raw, str(path)))
    return cases


def _parse_case(i: int, raw: Any, file: str) -> Case:
    key = f"case[{i}]"
    if not isinstance(raw, dict):
        raise ConfigError(key, "must be a table", file=file)
    path = raw.get("path")
    if not isinstance(path, str) or not path.startswith("/"):
        raise ConfigError(f"{key}.path", "must be a string starting with '/'", file=file)
    status = raw.get("status")
    if isinstance(status, int) and not isinstance(status, bool):
        statuses: tuple[int, ...] = (status,)
    elif (
        isinstance(status, list)
        and status
        and all(isinstance(s, int) and not isinstance(s, bool) for s in status)
    ):
        statuses = tuple(status)
    else:
        raise ConfigError(f"{key}.status", "must be an integer or a list of integers", file=file)
    location = raw.get("location")
    if location is not None and not isinstance(location, str):
        raise ConfigError(f"{key}.location", "must be a string", file=file)
    for extra in sorted(set(raw) - {"path", "status", "location"}):
        console.warn(f"{file}: unknown key '{key}.{extra}' ignored")
    return Case(path=path, status=statuses, location=location)


def location_matches(expected: str, actual: str | None) -> bool:
    if actual is None:
        return False
    if expected.endswith("*"):
        return actual.startswith(expected[:-1])
    return actual == expected


def check_case(base_url: str, case: Case) -> tuple[bool, str]:
    resp = fetch(base_url + case.path)
    problems = []
    if resp.status not in case.status:
        problems.append(f"status {resp.status} not in {list(case.status)}")
    if case.location is not None and not location_matches(case.location, resp.location):
        problems.append(f"location {resp.location!r} != {case.location!r}")
    detail = f"{resp.status}" + (f" -> {resp.location}" if resp.location else "")
    return (not problems, "; ".join(problems) if problems else detail)


def check_health(base_url: str) -> tuple[bool, str]:
    resp = fetch(base_url + "/health.php")
    try:
        payload = json.loads(resp.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    ok = resp.status == 200 and isinstance(payload, dict) and payload.get("ok") is True
    return ok, f"HTTP {resp.status} {payload if payload is not None else '(non-JSON body)'}"


def verify(base_url: str, cases_file: Path) -> bool:
    base_url = base_url.rstrip("/")
    console.step(f"verify {base_url}")
    all_ok = True
    ok, detail = check_health(base_url)
    console.info(f"  {'PASS' if ok else 'FAIL'}  /health.php  {detail}")
    all_ok &= ok
    cases = load_cases(cases_file)
    if not cases:
        console.warn(f"no redirect cases in {cases_file}")
    for case in cases:
        ok, detail = check_case(base_url, case)
        console.info(f"  {'PASS' if ok else 'FAIL'}  {case.path}  {detail}")
        all_ok &= ok
    total = len(cases) + 1
    console.info(
        f"verify: {'all' if all_ok else 'some'} checks {'passed' if all_ok else 'FAILED'}"
        f" ({total} total)"
    )
    return all_ok
