"""`ym new` and `ym template update`."""

from __future__ import annotations

import json
import tomllib

import pytest

from yourls_manager import newsite
from yourls_manager.cli import main
from yourls_manager.util import UsageError

from .conftest import (
    YOURLS_CORE_URL,
    core_files,
    git,
    git_insteadof,
    needs_copier,
    new_site_args,
    write_files,
)


def test_copier_command_prefers_uvx(monkeypatch):
    paths = {"uv": "/x/uv", "uvx": "/x/uvx", "copier": "/x/copier"}
    monkeypatch.setattr(newsite.shutil, "which", lambda n: paths.get(n))
    assert newsite.copier_command() == ["/x/uvx", "copier"]
    del paths["uvx"]
    assert newsite.copier_command() == ["/x/uv", "tool", "run", "copier"]
    del paths["uv"]
    assert newsite.copier_command() == ["/x/copier"]
    del paths["copier"]
    with pytest.raises(UsageError, match="Install uv"):
        newsite.copier_command()


def test_data_args_validation():
    assert newsite._data_args(["a=1", "b_c=x=y"]) == ["--data", "a=1", "--data", "b_c=x=y"]
    for bad in ["noequals", "=v", "1a=b", "a-b=c"]:
        with pytest.raises(UsageError):
            newsite._data_args([bad])


def test_new_refuses_non_empty_dest(tmp_path, capsys):
    (tmp_path / "x").write_text("x")
    assert main(["new", str(tmp_path)]) == 2
    assert "non-empty" in capsys.readouterr().err


def test_new_bad_data_is_usage_error(tmp_path, capsys):
    assert main(["new", str(tmp_path / "s"), "--data", "nope"]) == 2


def test_template_update_requires_answers_and_clean_tree(tmp_path, capsys):
    site = tmp_path / "site"
    site.mkdir()
    (site / "site.toml").write_text("")
    assert main(["--site-dir", str(site), "template", "update"]) == 2
    assert ".copier-answers.yml" in capsys.readouterr().err
    (site / ".copier-answers.yml").write_text("_src_path: x\n")
    assert main(["--site-dir", str(site), "template", "update"]) == 2
    assert "not a git repository" in capsys.readouterr().err
    git(site, "init", "-q")
    assert main(["--site-dir", str(site), "template", "update", "--manager-version", "0.2"]) == 2
    assert "--manager-version" in capsys.readouterr().err
    assert main(["--site-dir", str(site), "template", "update"]) == 2
    assert "uncommitted changes" in capsys.readouterr().err


def test_git_init_without_identity_warns(tmp_path, monkeypatch, capsys):
    for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(k, raising=False)
    gcfg = tmp_path / "gitconfig"
    gcfg.write_text("[user]\n\tuseConfigOnly = true\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gcfg))
    monkeypatch.setenv("HOME", str(tmp_path))
    site = tmp_path / "s"
    site.mkdir()
    (site / "f").write_text("x")
    assert newsite._git_init_commit(site) is False
    assert "not committed" in capsys.readouterr().err
    status = git(site, "status", "--porcelain")
    assert status == "A  f"


@pytest.fixture
def fake_github(upstream):
    """Fake YOURLS core + the template's example plugins, reached via git insteadOf."""
    core = upstream("core")
    core.commit(core_files("1.0.0"), tag="1.0.0")
    core.commit(core_files("1.1.0"), tag="1.1.0")
    git_insteadof(YOURLS_CORE_URL, core.url)
    plugins = {}
    for name in ("timezones", "404-if-not-found"):
        up = upstream(name)
        up.commit({"plugin.php": f"<?php /* Plugin Name: {name} */\n"})
        git_insteadof(f"https://github.com/YOURLS/{name}", up.url)
        plugins[name] = up
    return core, plugins


@needs_copier
def test_new_and_template_update(tmp_path, fake_github, no_uv_lock, capfd):
    dest = tmp_path / "go-example-com"
    assert main(new_site_args(dest, "yourls_version=1.0.0")) == 0
    out = capfd.readouterr().out
    assert "ym target config prod" in out and "ym deploy prod --dry-run" in out

    # rendered, vendored, locked
    cfg = tomllib.loads((dest / "site.toml").read_text())
    assert cfg["core"]["version"] == "1.0.0"
    assert "'1.0.0'" in (dest / "yourls/includes/version.php").read_text()
    lock = json.loads((dest / "yourls.lock.json").read_text())
    assert lock["core"]["version"] == "1.0.0"
    assert set(lock["plugins"]) == {"timezones", "404-if-not-found"}
    assert (dest / "yourls/user/plugins/timezones/plugin.php").is_file()
    assert (dest / "overlay/user/config.php").is_file()
    assert no_uv_lock == [dest]

    # one clean initial commit
    assert git(dest, "log", "--format=%s") == newsite.INITIAL_COMMIT_MESSAGE
    assert git(dest, "status", "--porcelain") == ""
    assert git(dest, "branch", "--show-current") == "main"
    assert "yourls/includes/version.php" in git(dest, "ls-files")

    # second `ym new` into the same dir is refused
    assert main(new_site_args(dest)) == 2

    # the new repo is healthy (no drift)
    assert main(["--site-dir", str(dest), "doctor", "--local"]) == 0
    assert "no drift" in capfd.readouterr().out

    # template update: dirty tree refused, then pins a new manager version
    (dest / "site.toml").write_text((dest / "site.toml").read_text() + "\n# local edit\n")
    assert main(["--site-dir", str(dest), "template", "update", "--defaults"]) == 2
    git(dest, "commit", "-qam", "edit")
    S = ["--site-dir", str(dest)]
    rc = main(
        [*S, "template", "update", "--vcs-ref", "HEAD", "--defaults", "--manager-version", "v9.9.9"]
    )
    assert rc == 0
    out = capfd.readouterr().out
    assert 'tag = "v9.9.9"' in (dest / "pyproject.toml").read_text()
    assert "manager_version: v9.9.9" in (dest / ".copier-answers.yml").read_text()
    assert "pyproject.toml" in out and "git add -A" in out
    assert "# local edit" in (dest / "site.toml").read_text()  # site-owned, untouched
    assert len(no_uv_lock) == 2


@needs_copier
def test_artifact_only_gitlab_site(tmp_path, fake_github, no_uv_lock, capfd):
    """Template ci_provider=gitlab + release_artifact end to end."""
    dest = tmp_path / "app"
    argv = new_site_args(dest, "yourls_version=1.0.0", "ci_provider=gitlab",
                         "release_artifact=true")  # fmt: skip
    assert main(argv) == 0
    out = capfd.readouterr().out
    assert "ym artifact build" in out and "ym target config" not in out
    assert (dest / ".gitlab-ci.yml").is_file() and not (dest / ".github").exists()
    assert "ym artifact build" in (dest / ".gitlab-ci.yml").read_text()
    cfg = tomllib.loads((dest / "site.toml").read_text())
    assert cfg["artifact"]["enabled"] is True and "deploy" not in cfg

    S = ["--site-dir", str(dest)]
    assert main([*S, "release"]) == 0
    assert main([*S, "artifact", "build"]) == 0
    assert (dest / "dist-artifacts" / "go-example-com-1.0.0-1.tar.gz").is_file()
    assert git(dest, "status", "--porcelain") == ""  # dist-artifacts/ is gitignored
    capfd.readouterr()
    assert main([*S, "doctor"]) == 0
    out = capfd.readouterr().out
    assert "artifact: tar.gz, published by .gitlab-ci.yml" in out
    assert "targets: none (artifact-only site)" in out

    # switch the CI files with `ym template update --data`
    rc = main([*S, "template", "update", "--vcs-ref", "HEAD", "--defaults",
               "--data", "ci_provider=github"])  # fmt: skip
    assert rc == 0
    assert not (dest / ".gitlab-ci.yml").exists()
    assert (dest / ".github/workflows/publish.yml").is_file()


@needs_copier
def test_new_no_vendor_no_git(tmp_path, no_uv_lock, capfd):
    dest = tmp_path / "s"
    assert main([*new_site_args(dest), "--no-vendor", "--no-git"]) == 0
    assert (dest / "site.toml").is_file()
    assert not (dest / "yourls").exists()
    assert not (dest / ".git").exists()
    assert "ym core" in capfd.readouterr().out


def test_uv_lock_failure_is_a_warning(tmp_path, monkeypatch, capsys):
    script = tmp_path / "uv"
    script.write_text("#!/bin/sh\necho 'failed to fetch tag v9.9.9' >&2\nexit 2\n")
    script.chmod(0o755)
    monkeypatch.setattr(newsite.shutil, "which", lambda n: str(script) if n == "uv" else None)
    assert newsite.uv_lock(tmp_path) is False
    err = capsys.readouterr().err
    assert "uv lock" in err and "v9.9.9" in err


def test_uv_lock_success(tmp_path, monkeypatch):
    script = tmp_path / "uv"
    script.write_text('#!/bin/sh\n[ "$1" = lock ] && touch uv.lock\n')
    script.chmod(0o755)
    monkeypatch.setattr(newsite.shutil, "which", lambda n: str(script) if n == "uv" else None)
    assert newsite.uv_lock(tmp_path) is True
    assert (tmp_path / "uv.lock").exists()


def test_copier_failure_reported(tmp_path, monkeypatch, capsys):
    script = tmp_path / "copier"
    script.write_text("#!/bin/sh\nexit 3\n")
    script.chmod(0o755)
    monkeypatch.setattr(newsite, "copier_command", lambda: [str(script)])
    assert main(["new", str(tmp_path / "s"), "--defaults"]) == 1
    assert "copier failed (exit 3)" in capsys.readouterr().err


def test_template_update_passes_data(tmp_path, monkeypatch, no_uv_lock, capsys):
    site = tmp_path / "site"
    write_files(site, {"site.toml": "", ".copier-answers.yml": "_src_path: x\n"})
    git(site, "init", "-q")
    git(site, "add", "-A")
    git(site, "commit", "-q", "-m", "init")
    log = tmp_path / "argv.txt"
    script = tmp_path / "copier"
    script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{log}"\n')
    script.chmod(0o755)
    monkeypatch.setattr(newsite, "copier_command", lambda: [str(script)])
    S = ["--site-dir", str(site), "template", "update"]
    assert main([*S, "--data", "ci_provider", "--defaults"]) == 2
    assert "--data expects KEY=VALUE" in capsys.readouterr().err
    assert main([*S, "--manager-version", "v0.8.0", "--data", "ci_provider=gitlab",
                 "--data", "release_artifact=true"]) == 0  # fmt: skip
    argv = log.read_text().splitlines()
    assert argv[:2] == ["update", "--skip-answered"]
    pairs = [argv[i + 1] for i, a in enumerate(argv) if a == "--data"]
    assert pairs == ["manager_version=v0.8.0", "ci_provider=gitlab", "release_artifact=true"]
