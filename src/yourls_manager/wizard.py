"""`ym setup`: interactive onboarding wizard (design §9).

The wizard only orchestrates the non-interactive commands (``ym new``, ``ym adopt``,
``ym server-config``, ``ym deploy --dry-run``), printing each equivalent command before
running it. ``run_wizard`` is pure apart from its injected callables, so tests drive it
with scripted answers.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .config import load_site
from .util import EXIT_OK, YMError, console

Ask = Callable[[str, str | None], str]
Invoke = Callable[[Sequence[str]], int]
ConfigExists = Callable[[Path, str], bool]


@dataclass
class WizardResult:
    exit_code: int = EXIT_OK
    site_dir: Path | None = None
    done: list[str] = field(default_factory=list)
    remaining: list[str] = field(default_factory=list)


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


def default_config_exists(site: Path, target: str) -> bool:
    """True when the target's server-local config file already exists."""
    from .remote import runner_for

    cfg = load_site(site)
    spec = cfg.target(target)
    runner = runner_for(spec)
    return runner.exists(runner.abspath(spec.config_file), "-f")


def _cmd(argv: Sequence[str]) -> str:
    return "ym " + shlex.join(argv)


def run_wizard(
    ask: Ask,
    *,
    invoke: Invoke,
    config_exists: ConfigExists = default_config_exists,
    cwd: Path | None = None,
) -> WizardResult:
    """Run the onboarding flow. ``ask(question, default)`` returns the raw answer."""
    res = WizardResult()
    cwd = cwd or Path.cwd()

    def step(argv: list[str]) -> int:
        console.info("")
        console.info(f"$ {_cmd(argv)}")
        code = invoke(argv)
        if code == EXIT_OK:
            res.done.append(_cmd(argv))
        return code

    console.info("yourls-manager setup: create a Git-managed YOURLS site repo.")
    console.info("Every step prints the equivalent `ym` command so you can script it later.")
    mode = _choice(
        ask,
        "What do you want to do?",
        {"new": "start a new YOURLS site", "adopt": "adopt an existing YOURLS install"},
        "new",
    )
    adopting = mode == "adopt"
    dest_raw = _text(ask, "Directory for the new site repo", "yourls-site")
    dest = (cwd / Path(dest_raw).expanduser()).resolve()
    res.site_dir = dest

    new_argv = ["new", str(dest)]
    if adopting:
        new_argv += ["--data", "include_example_plugins=false"]
    console.info("Copier will now ask about the site (name, URL, deploy host, ...).")
    code = step(new_argv)
    if code != EXIT_OK:
        res.exit_code = code
        res.remaining.append(f"fix the error above and re-run `{_cmd(new_argv)}`")
        return _summary(res)

    site = ["--site-dir", str(dest)]
    try:
        cfg = load_site(dest)
    except YMError as exc:  # pragma: no cover - ym new validated it already
        console.error(str(exc))
        res.exit_code = exc.exit_code
        return _summary(res)
    has_prod = "prod" in cfg.targets
    target = "prod"

    if adopting:
        adopt_argv: list[str] | None = None
        if has_prod and _yes(
            ask, f"Adopt the install at the prod target ({_describe(cfg.targets['prod'])})?"
        ):
            adopt_argv = [*site, "adopt", "--from-target", "prod"]
        else:
            path = ask(
                "Path to a local copy of the existing install (empty: stop here and configure"
                " [deploy.targets.prod] in site.toml first)",
                None,
            ).strip()
            if path:
                adopt_argv = [*site, "adopt", "--from-dir", str(Path(path).expanduser())]
        if adopt_argv is None:
            res.remaining += [
                f"edit {dest / 'site.toml'}: fill in [deploy.targets.prod]",
                f"{_cmd([*site, 'adopt', '--from-target', 'prod'])}",
            ]
            return _summary(res)
        code = step(adopt_argv)
        if code != EXIT_OK:
            res.exit_code = code
            res.remaining.append(f"fix the error above and re-run `{_cmd(adopt_argv)}`")
            return _summary(res)
        res.remaining.append(
            f"review {dest / 'adopt-review'} (REPORT.md; merge files into overlay/ by hand)"
        )
        res.remaining.append(f'commit: git -C {dest} add -A && git -C {dest} commit -m "Adopt"')

    if not has_prod:
        res.remaining += [
            f"edit {dest / 'site.toml'}: uncomment and fill in [deploy.targets.prod]",
            _cmd([*site, "server-config", target]),
            _cmd([*site, "deploy", target, "--dry-run"]),
            _cmd([*site, "deploy", target]),
        ]
        return _summary(res)

    sc_argv = [*site, "server-config", target]
    if adopting:
        sc_argv.append("--from-existing")
    try:
        exists = config_exists(dest, target)
    except YMError as exc:
        console.warn(f"could not check the server-local config on {target}: {exc}")
        exists = True
        res.remaining.append(f"check that the server-local config exists: {_cmd(sc_argv)}")
    if not exists:
        what = "copy the existing install's config to" if adopting else "create"
        if _yes(
            ask, f"The server-local config is missing on {target}. {what.capitalize()} it now?"
        ):
            code = step(sc_argv)
            if code != EXIT_OK:
                res.remaining.append(f"re-run `{_cmd(sc_argv)}`")
        else:
            res.remaining.append(_cmd(sc_argv))

    dry = [*site, "deploy", target, "--dry-run"]
    if _yes(ask, f"Preview a deploy to {target} now (dry run, changes nothing)?"):
        if step(dry) != EXIT_OK:
            res.remaining.append(f"re-run `{_cmd(dry)}` after fixing the error above")
    else:
        res.remaining.append(_cmd(dry))
    res.remaining.append(_cmd([*site, "deploy", target]))
    if adopting:
        res.remaining.append(
            f"update YOURLS: {_cmd([*site, 'core', '--version', 'latest'])}, commit,"
            f" {_cmd([*site, 'deploy', target])}"
        )
    return _summary(res)


def _describe(spec) -> str:
    where = f"{spec.user + '@' if spec.user else ''}{spec.host}" if spec.host else "local"
    return f"{where}:{spec.path}"


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
    return res
