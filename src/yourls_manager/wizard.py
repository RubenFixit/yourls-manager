"""`ym init`: interactive onboarding wizard (design §9).

The wizard only orchestrates the non-interactive commands (``ym new``, ``ym target login``,
``ym adopt``, ``ym target config``, ``ym deploy --dry-run``), printing each equivalent
command before running it. ``run_wizard`` is pure apart from its injected callables, so
tests drive it with scripted answers.

Resuming: progress is saved in ``<site>/.git/ym-init.json`` (never committed) and the
last unfinished site is remembered in ``$XDG_STATE_HOME/yourls-manager/init.json``
(default ``~/.local/state``). Running ``ym init`` again (or ``ym init DIR``) offers to
continue where it stopped; steps that are already done are skipped.
"""

from __future__ import annotations

import json
import os
import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import SITE_FILE, load_site
from .targets import describe
from .util import EXIT_OK, YMError, console, iso_utc

Ask = Callable[[str, str | None], str]
Invoke = Callable[[Sequence[str]], int]
ConfigExists = Callable[[Path, str], bool]
SshOk = Callable[[Path, str], bool]

STATE_NAME = "ym-init.json"
POINTER_NAME = "init.json"
TARGET = "prod"


@dataclass
class WizardResult:
    exit_code: int = EXIT_OK
    site_dir: Path | None = None
    done: list[str] = field(default_factory=list)
    remaining: list[str] = field(default_factory=list)
    resumable: bool = False


# --------------------------------------------------------------------------- state


def state_home() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "yourls-manager"


class InitState:
    """Per-site progress (in .git/) plus a global pointer to the last unfinished site."""

    def __init__(self, site: Path, home: Path):
        self.site = site
        self.home = home
        self.data: dict[str, Any] = {"mode": None, "completed": [], "finished": False}
        source = self.file
        if source and source.is_file():
            try:
                loaded = json.loads(source.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data.update(loaded)
            except (OSError, json.JSONDecodeError):
                pass

    @property
    def file(self) -> Path | None:
        git_dir = self.site / ".git"
        return git_dir / STATE_NAME if git_dir.is_dir() else None

    def exists(self) -> bool:
        return self.file is not None and self.file.is_file()

    @property
    def mode(self) -> str | None:
        return self.data.get("mode")

    @mode.setter
    def mode(self, value: str) -> None:
        self.data["mode"] = value

    def is_done(self, step: str) -> bool:
        return step in self.data.get("completed", [])

    def mark(self, step: str) -> None:
        if step not in self.data["completed"]:
            self.data["completed"].append(step)
        self.save()

    def save(self) -> None:
        self.data["updated_at"] = iso_utc()
        if self.file is not None:
            try:
                self.file.write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")
            except OSError as exc:  # pragma: no cover - best effort
                console.warn(f"could not save setup progress: {exc}")
        pointer = self.home / POINTER_NAME
        try:
            if self.data.get("finished"):
                if _read_pointer(self.home) == self.site:
                    pointer.unlink(missing_ok=True)
            else:
                self.home.mkdir(parents=True, exist_ok=True)
                pointer.write_text(json.dumps({"site": str(self.site)}) + "\n", encoding="utf-8")
        except OSError:  # pragma: no cover - best effort
            pass

    def finish(self) -> None:
        self.data["finished"] = True
        self.save()


def _read_pointer(home: Path) -> Path | None:
    try:
        data = json.loads((home / POINTER_NAME).read_text(encoding="utf-8"))
        return Path(data["site"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def find_resumable(cwd: Path, home: Path) -> Path | None:
    """cwd if it is a site repo with unfinished setup, else the remembered site."""
    for cand in (cwd, _read_pointer(home)):
        if cand is None or not (cand / SITE_FILE).is_file():
            continue
        st = InitState(cand, home)
        if st.exists() and not st.data.get("finished"):
            return cand
    return None


# --------------------------------------------------------------------------- prompts


def _yes(ask: Ask, question: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        answer = ask(f"{question} [{hint}]", None).strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        console.info("  please answer y or n")


def _choice(ask: Ask, question: str, options: dict[str, str], default: str) -> str:
    listing = "\n".join(f"  {k}) {v}" for k, v in options.items())
    while True:
        answer = ask(f"{question}\n{listing}\nChoose", default).strip().lower() or default
        if answer in options:
            return answer
        console.info(f"  please answer one of: {', '.join(options)}")


def _text(ask: Ask, question: str, default: str | None = None) -> str:
    while True:
        answer = ask(question, default).strip()
        if answer:
            return answer
        if default:
            return default


# --------------------------------------------------------------------------- defaults


def default_config_exists(site: Path, target: str) -> bool:
    """True when the target's server-local config file already exists."""
    from .remote import runner_for

    cfg = load_site(site)
    spec = cfg.target(target)
    if spec.is_sftp:
        from .transport.sftp import SftpTransport

        with SftpTransport.for_target(spec) as t:
            return t.is_file(t.abspath(spec.config_file))
    runner = runner_for(spec)
    return runner.exists(runner.abspath(spec.config_file), "-f")


def default_ssh_ok(site: Path, target: str) -> bool:
    """Login works non-interactively (sftp targets: an SFTP session; a password from
    ``YM_<TARGET>_PASSWORD`` counts)."""
    from .login import check_login

    return check_login(load_site(site).target(target), password=True).ok


def _cmd(argv: Sequence[str]) -> str:
    return "ym " + shlex.join(argv)


# --------------------------------------------------------------------------- flow


def run_wizard(
    ask: Ask,
    *,
    invoke: Invoke,
    config_exists: ConfigExists = default_config_exists,
    ssh_ok: SshOk = default_ssh_ok,
    cwd: Path | None = None,
    start: Path | None = None,
    home: Path | None = None,
) -> WizardResult:
    """Run the onboarding flow. ``ask(question, default)`` returns the raw answer.

    ``start``: a site dir to resume (``ym init DIR``). Otherwise an unfinished setup in
    ``cwd`` or the remembered one is offered for resuming.
    """
    res = WizardResult()
    cwd = cwd or Path.cwd()
    home = home or state_home()

    def step(argv: list[str]) -> int:
        console.info("")
        console.info(f"$ {_cmd(argv)}")
        code = invoke(argv)
        if code == EXIT_OK:
            res.done.append(_cmd(argv))
        return code

    def failed(code: int, argv: list[str]) -> WizardResult:
        res.exit_code = code
        res.resumable = True
        res.remaining.append("fix the error above, then run `ym init` again to resume")
        res.remaining.append(f"(or run the failed step yourself: `{_cmd(argv)}`)")
        return _summary(res)

    console.info("yourls-manager init: create a Git-managed YOURLS site repo.")
    console.info("Every step prints the equivalent `ym` command so you can script it later.")

    # ---- resume or start fresh
    dest: Path | None = None
    if start is not None:
        start = (cwd / start.expanduser()).resolve()
        if not (start / SITE_FILE).is_file():
            raise YMError(f"{start} is not a site repo (no {SITE_FILE}); run `ym init` instead")
        dest = start
    else:
        found = find_resumable(cwd, home)
        if found is not None:
            st = InitState(found, home)
            done = ", ".join(st.data.get("completed", [])) or "repo created"
            console.info(f"Unfinished setup found: {found} ({st.mode or '?'}; done: {done})")
            if _yes(ask, "Resume it?"):
                dest = found

    if dest is not None:
        state = InitState(dest, home)
        res.site_dir = dest
        console.info(f"Resuming setup of {dest}")
        if state.mode is None:
            state.mode = _choice(ask, "Is this a new site or an adopted install?",
                                 {"new": "new site", "adopt": "adopt an existing install"},
                                 "new")  # fmt: skip
        state.mark("new")
    else:
        mode = _choice(
            ask,
            "What do you want to do?",
            {"new": "start a new YOURLS site", "adopt": "adopt an existing YOURLS install"},
            "new",
        )
        dest_raw = _text(ask, "Directory for the new site repo", "yourls-site")
        dest = (cwd / Path(dest_raw).expanduser()).resolve()
        res.site_dir = dest
        state = InitState(dest, home)
        if (dest / SITE_FILE).is_file():
            console.info(f"{dest} already contains a site repo; continuing with it.")
        else:
            new_argv = ["new", str(dest)]
            if mode == "adopt":
                new_argv += ["--data", "include_example_plugins=false"]
            console.info("Copier will now ask about the site (name, URL, deploy host, ...).")
            code = step(new_argv)
            if code != EXIT_OK:
                res.exit_code = code
                res.remaining.append("fix the error above and re-run `ym init`")
                return _summary(res)
            state = InitState(dest, home)  # .git exists now
        state.mode = mode
        state.mark("new")

    adopting = state.mode == "adopt"
    site = ["--site-dir", str(dest)]
    try:
        cfg = load_site(dest)
    except YMError as exc:
        console.error(str(exc))
        res.exit_code = exc.exit_code
        res.resumable = True
        res.remaining.append(f"fix {dest / SITE_FILE}, then run `ym init` again to resume")
        return _summary(res)
    has_prod = TARGET in cfg.targets
    if not has_prod:
        console.info("")
        console.info(f"site.toml has no {TARGET} target yet (where and how the site deploys).")
        add_argv = [*site, "target", "add", TARGET]
        if _yes(ask, f"Add the {TARGET} target now?"):
            code = step(add_argv)
            if code != EXIT_OK:
                return failed(code, add_argv)
            cfg = load_site(dest)
            has_prod = TARGET in cfg.targets
    remote_prod = has_prod and not cfg.targets[TARGET].is_local

    # ---- ssh key authentication
    if remote_prod and not ssh_ok(dest, TARGET):
        ssh_argv = [*site, "target", "login", TARGET]
        console.info("")
        if cfg.targets[TARGET].is_sftp:
            from .transport.sftp import password_env

            console.info(
                f"ym can't log in to {describe(cfg.targets[TARGET])} without a password."
                f" An SSH key is best (or set ${password_env(TARGET)} for each command)."
            )
        else:
            console.info(
                f"ym needs SSH key authentication to {describe(cfg.targets[TARGET])}"
                " (it never prompts for passwords)."
            )
        if _yes(ask, "Set up an SSH key for it now?"):
            code = step(ssh_argv)
            if code != EXIT_OK:
                return failed(code, ssh_argv)
        else:
            res.resumable = True
            res.remaining += [_cmd(ssh_argv), "then run `ym init` again to resume"]
            return _summary(res)

    # ---- adopt
    if adopting and state.is_done("adopt"):
        console.info(f"\n✓ already done: adopt ({dest / 'adopt-review' / 'REPORT.md'})")
    elif adopting:
        adopt_argv: list[str] | None = None
        if has_prod and _yes(
            ask, f"Adopt the install at the prod target ({describe(cfg.targets[TARGET])})?"
        ):
            adopt_argv = [*site, "adopt", "--from-target", TARGET]
        else:
            path = ask(
                "Path to a local copy of the existing install (empty: stop here and add the"
                f" {TARGET} target first)",
                None,
            ).strip()
            if path:
                adopt_argv = [*site, "adopt", "--from-dir", str(Path(path).expanduser())]
        if adopt_argv is None:
            res.resumable = True
            res.remaining += [
                _cmd([*site, "target", "add", TARGET]),
                "then run `ym init` again to resume",
            ]
            return _summary(res)
        code = step(adopt_argv)
        if code != EXIT_OK:
            return failed(code, adopt_argv)
        state.mark("adopt")
    if adopting:
        res.remaining.append(
            f"review {dest / 'adopt-review'} (REPORT.md; merge files into overlay/ by hand)"
        )
        res.remaining.append(f'commit: git -C {dest} add -A && git -C {dest} commit -m "Adopt"')

    if not has_prod:
        res.resumable = True
        res.remaining += [
            _cmd([*site, "target", "add", TARGET]),
            "then run `ym init` again to resume (server config, deploy preview)",
        ]
        return _summary(res)

    # ---- server-local config
    sc_argv = [*site, "target", "config", TARGET]
    if adopting:
        sc_argv.append("--from-existing")
    try:
        exists = config_exists(dest, TARGET)
    except YMError as exc:
        console.warn(f"could not check the server-local config on {TARGET}: {exc}")
        exists = True
        res.remaining.append(f"check that the server-local config exists: {_cmd(sc_argv)}")
    if exists:
        console.info(f"\n✓ server-local config exists on {TARGET}")
    else:
        what = "copy the existing install's config to" if adopting else "create"
        if _yes(
            ask, f"The server-local config is missing on {TARGET}. {what.capitalize()} it now?"
        ):
            code = step(sc_argv)
            if code != EXIT_OK:
                return failed(code, sc_argv)
        else:
            res.resumable = True
            res.remaining += [_cmd(sc_argv), "then run `ym init` again to resume"]
            return _summary(res)

    # ---- deploy preview
    dry = [*site, "deploy", TARGET, "--dry-run"]
    if _yes(ask, f"Preview a deploy to {TARGET} now (dry run, changes nothing)?"):
        code = step(dry)
        if code != EXIT_OK:
            return failed(code, dry)
    else:
        res.remaining.append(_cmd(dry))
    res.remaining.append(_cmd([*site, "deploy", TARGET]))
    if cfg.targets[TARGET].is_sftp:
        from .deploy.sftp import manual_steps

        console.info("")
        console.info(f"{TARGET} is an sftp target; after deploying, in the browser:")
        for item in manual_steps(cfg, cfg.targets[TARGET]):
            console.info(f"  - {item}")
            res.remaining.append(f"after deploying: {item}")
    if adopting:
        res.remaining.append(
            f"update YOURLS: {_cmd([*site, 'update', '--commit'])},"
            f" {_cmd([*site, 'deploy', TARGET])}"
        )
    state.finish()
    return _summary(res)


def _summary(res: WizardResult) -> WizardResult:
    console.info("")
    console.info("Summary")
    console.info("  done:")
    for d in res.done or ["(nothing)"]:
        console.info(f"    {d}")
    if res.remaining:
        console.info("  still to do:")
        for r in res.remaining:
            console.info(f"    {r}")
    if res.resumable:
        console.info("Progress is saved: `ym init` picks up where this left off.")
    return res
