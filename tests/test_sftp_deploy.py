"""`ym deploy` to sftp targets against a real OpenSSH server."""

from __future__ import annotations

import json
import re
import stat
import textwrap
from pathlib import Path

import pytest

from yourls_manager import __version__
from yourls_manager.cli import main
from yourls_manager.deploy.sftp import (
    compute_plan,
    parent_dirs,
    protect_matcher,
    upload_order,
    yourls_state,
)

from .conftest import core_files, git, install_client_key, needs_sshd, write_files

# --------------------------------------------------------------------------- units


def test_protect_matcher():
    p = protect_matcher([".well-known/", "/private", "cache/tmp", "*.bak"])
    assert p(".well-known/acme/x") and p("sub/.well-known/x")  # unanchored: any depth
    assert p("private/a") and not p("sub/private/a")  # leading / anchors
    assert p("cache/tmp/x") and not p("x/cache/tmp/y")  # inner / anchors
    assert p("a.bak") and p("d/a.bak") and not p("a.php")


def test_compute_plan():
    def m(*items):
        return {rel: {"sha256": sha, "size": size} for rel, sha, size in items}

    local = m(("a.php", "1", 1), ("b.php", "2", 2), ("yourls-loader.php", "9", 9),
              (".htaccess", "8", 8), ("new.php", "3", 3))  # fmt: skip
    remote = m(("a.php", "1", 1), ("b.php", "X", 2), ("yourls-loader.php", "9", 9),
               (".htaccess", "0", 8), ("gone.php", "4", 4), (".well-known/k", "5", 5),
               ("edited.php", "6", 6))  # fmt: skip
    prot = protect_matcher([".well-known/"])
    plan = compute_plan(local, remote, prot)
    assert plan.upload == ["b.php", "new.php", ".htaccess"]  # entry points last
    assert plan.reasons == {"b.php": "changed", "new.php": "new", ".htaccess": "changed"}
    assert plan.delete == ["edited.php", "gone.php"] and plan.protected == [".well-known/k"]
    assert plan.unchanged == 2 and plan.has_manifest

    # --verify-remote: size differences mean the server copy was edited
    server = {"a.php": 5, "b.php": 2, "yourls-loader.php": 9, ".htaccess": 8,
              "gone.php": None, ".well-known/k": 5, "edited.php": 7}  # fmt: skip
    plan = compute_plan(local, remote, prot, server)
    assert plan.reasons["a.php"] == "server-changed"
    assert plan.delete == [] and plan.modified_kept == ["edited.php"]

    # no manifest: upload everything, delete nothing
    plan = compute_plan(local, None, prot)
    assert not plan.has_manifest and len(plan.upload) == 5 and plan.delete == []
    assert plan.upload[-2:] == [".htaccess", "yourls-loader.php"]


def test_upload_order_and_parents():
    order = upload_order(["yourls-loader.php", "user/config.php", "z.php", ".htaccess", "a/b"])
    assert order == ["a/b", "z.php", ".htaccess", "user/config.php", "yourls-loader.php"]
    assert parent_dirs("a/b/c.php") == ["a", "a/b"] and parent_dirs("x") == []


def test_yourls_state(state_server):
    url, handler = state_server
    assert yourls_state(url) == ("ok", None)
    handler.mode = "install"
    assert yourls_state(url) == ("install", f"{url}/admin/install.php")
    handler.mode = "upgrade"
    assert yourls_state(url)[0] == "upgrade"
    assert yourls_state("http://127.0.0.1:9")[0] == "unknown"


# --------------------------------------------------------------------------- real server

ENTRY = [".htaccess", "user/config.php", "yourls-loader.php"]


def make_site(tmp_path: Path, core_url: str, target: str, url: str) -> Path:
    site = tmp_path / "site"
    write_files(
        site,
        {
            "site.toml": textwrap.dedent(
                f"""\
                [site]
                name = "Test Links"
                slug = "test-links"
                url  = "https://go.example.com"

                [core]
                repo    = "{core_url}"
                version = "1.0.0"
                exclude = [".github", ".git*", "tests", "*.md"]

                [[plugins]]
                name = "hyphens-in-urls"

                [[plugins]]
                name = "site-branding"

                [[plugins]]
                name = "sample-plugin"
                active = false

                """
            )
            + target
            + f'url = "{url}"\n',
            "overlay/.htaccess": "RewriteEngine On\n",
            "overlay/health.php": "<?php echo '{\"ok\":true}';\n",
            "overlay/user/config.php": "<?php // shim\n",
            "overlay/user/plugins/site-branding/plugin.php": "<?php // branding\n",
            "overlay/custom.php": "<?php // v1\n",
            "overlay/old/dir/x.php": "<?php // to be removed\n",
            "overlay/.well-known/security.txt": "Contact: x\n",
            "tests/redirects.toml": '[[case]]\npath = "/"\nstatus = 302\n',
            ".gitignore": "dist/\n",
        },
    )
    git(site, "init", "-q", "-b", "main")
    assert main(["--site-dir", str(site), "core"]) == 0
    git(site, "add", "-A")
    git(site, "commit", "-q", "-m", "init")
    return site


def upload_lines(out: str) -> list[str]:
    return re.findall(r"^\s+\[\d+/\d+\] (\S+)$", out, re.MULTILINE)


@pytest.mark.sftp
@needs_sshd
def test_deploy_flow(tmp_path, upstream, account, ssh_home, client_key, state_server, capsys):
    url, handler = state_server
    account.authorize(client_key.with_name("id_ed25519.pub").read_text())
    install_client_key(ssh_home, client_key)
    core = upstream("core")
    core.commit(core_files("1.0.0"), tag="1.0.0")
    target = account.target_toml(path='"www/go"', protect='[".well-known/"]')
    site = make_site(tmp_path, core.url, target, url)
    S = ["--site-dir", str(site)]
    web = account.real("www/go")
    state = account.real("www/.yourls-manager/test-links")

    # files ym never deployed: must survive every deploy
    account.write("www/go/unknown.php", "<?php // not ym's\n")
    account.write("www/go/.well-known/acme/token", "keep me")

    # ---- missing server-local config
    assert main([*S, "deploy", "prod"]) == 2
    err = capsys.readouterr().err
    assert "ym target config prod" in err and "yourls-config/test-links.php" in err
    account.write("www/yourls-config/test-links.php", "<?php // secrets\n", 0o600)

    # ---- dry run: plan only
    assert main([*S, "deploy", "prod", "--dry-run", "-v"]) == 0
    out = capsys.readouterr().out
    assert "no manifest at" in out and "deleting nothing" in out
    assert "dry run: stopped" in out and "upload yourls-loader.php (new)" in out
    assert not (web / "yourls-loader.php").exists() and not state.exists()

    # ---- first deploy: everything uploaded, nothing deleted
    (site / "overlay/custom.php").chmod(0o755)  # local modes don't leak to the server
    git(site, "commit", "-q", "-am", "exec bit")
    handler.mode = "install"
    assert main([*S, "deploy", "prod", "-v", "--skip-backup", "--skip-verify"]) == 0
    out, err = capsys.readouterr()
    dist = site / "dist"
    dist_files = sorted(p.relative_to(dist).as_posix() for p in dist.rglob("*") if p.is_file())
    order = upload_lines(out)
    assert sorted(order) == dist_files and order[-3:] == ENTRY  # entry points last
    assert "don't back up the database" in out and "--skip-backup is implied" in out
    assert f"open {url}/admin/install.php" in err
    assert f"{url}/admin/plugins.php" in out
    assert "make exactly these active: hyphens-in-urls, site-branding\n" in out
    assert (web / "unknown.php").exists() and (web / ".well-known/acme/token").exists()
    assert (web / "custom.php").read_text() == "<?php // v1\n"
    assert {stat.S_IMODE(p.stat().st_mode) for p in web.rglob("*.php")} == {0o644}
    assert (web / "yourls-loader.php").read_bytes() == (dist / "yourls-loader.php").read_bytes()
    assert not [p for p in account.home.rglob("*") if p.name.endswith(".ym-tmp")]
    manifest = json.loads((state / "manifest.json").read_text())
    assert sorted(manifest["files"]) == dist_files
    assert manifest["tool_version"] == __version__ and manifest["core_version"] == "1.0.0"
    assert manifest["git_commit"] == git(site, "rev-parse", "HEAD")
    release = json.loads((state / "release.json").read_text())
    assert release["target"] == "prod" and release["git_dirty"] is False
    assert release["active_plugins"] == ["hyphens-in-urls", "site-branding"]
    assert "files" not in release and len(release["lock_sha256"]) == 64
    for f in ("manifest.json", "release.json"):
        assert stat.S_IMODE((state / f).stat().st_mode) == 0o600
    assert stat.S_IMODE(state.stat().st_mode) == 0o700

    # ---- second deploy: only changes; removed files deleted; empty dirs cleaned up
    write_files(site, {"overlay/custom.php": "<?php // v2\n", "overlay/new/n.php": "<?php\n"})
    (site / "overlay/old/dir/x.php").unlink()
    (site / "overlay/.well-known/security.txt").unlink()
    git(site, "add", "-A")
    git(site, "commit", "-q", "-m", "change")
    handler.mode = "ok"
    assert main([*S, "deploy", "prod", "-v"]) == 0  # verify passes against the state server
    out = capsys.readouterr().out
    assert "upload 2 (1 new, 1 changed), delete 1," in out
    assert upload_lines(out) == ["custom.php", "new/n.php"]
    assert "kept (protect): 1" in out and "no install or upgrade needed" in out
    assert (web / "custom.php").read_text() == "<?php // v2\n"
    assert not (web / "old").exists()  # deleted file's dirs were emptied -> removed
    assert (web / ".well-known/security.txt").exists()  # protected: never deleted
    assert (web / "unknown.php").exists()  # never deployed by ym: never deleted
    assert "PASS" in out

    # ---- nothing changed
    assert main([*S, "deploy", "prod", "--skip-verify"]) == 0
    assert "upload 0 (0 new, 0 changed), delete 0" in capsys.readouterr().out

    # ---- --verify-remote notices a server-side edit and repairs it
    (web / "custom.php").write_text("<?php // hacked on the server, longer\n")
    assert main([*S, "deploy", "prod", "--dry-run"]) == 0
    assert "upload 0 " in capsys.readouterr().out  # manifest alone can't see it
    assert main([*S, "deploy", "prod", "--verify-remote", "--skip-verify"]) == 0
    out, err = capsys.readouterr()
    assert "custom.php was changed on the server" in err
    assert "1 changed on the server" in out
    assert (web / "custom.php").read_text() == "<?php // v2\n"

    # ---- upgrade needed
    handler.mode = "upgrade"
    assert main([*S, "deploy", "prod", "--skip-verify"]) == 0
    assert f"{url}/admin/upgrade.php" in capsys.readouterr().err
    # without --skip-verify a pending upgrade skips verify instead of failing the deploy
    assert main([*S, "deploy", "prod"]) == 0
    out = capsys.readouterr().out
    assert "verify (skipped: upgrade pending)" in out and "ym verify prod" in out

    # ---- plugins / options that don't apply
    assert main([*S, "plugins", "prod"]) == 2
    assert "admin/plugins.php" in capsys.readouterr().err


@pytest.mark.sftp
@needs_sshd
def test_deploy_with_password_env(tmp_path, upstream, sftp_only, ssh_home, state_server,
                                  monkeypatch, capsys):  # fmt: skip
    """SFTP-only account, password from YM_PROD_PASSWORD, absolute web root path."""
    url, _ = state_server
    core = upstream("core")
    core.commit(core_files("1.0.0"), tag="1.0.0")
    target = sftp_only.target_toml(path='"/home/ymsftponly/site"')
    site = make_site(tmp_path, core.url, target, url)
    sftp_only.write("yourls-config/test-links.php", "<?php\n", 0o600)
    S = ["--site-dir", str(site)]
    assert main([*S, "deploy", "prod", "--skip-verify"]) == 1
    assert "the password in $YM_PROD_PASSWORD" in capsys.readouterr().err
    monkeypatch.setenv("YM_PROD_PASSWORD", sftp_only.password)
    assert main([*S, "deploy", "prod", "--skip-verify"]) == 0
    assert (sftp_only.real("site") / "yourls-loader.php").exists()
    assert (sftp_only.real(".yourls-manager/test-links/manifest.json")).exists()
    capsys.readouterr()
    assert main([*S, "deploy", "prod", "--verify-remote", "--dry-run"]) == 0
    assert "checking" in capsys.readouterr().out


def test_verify_remote_rejected_for_rsync(tmp_path, capsys):
    write_files(
        tmp_path,
        {
            "site.toml": '[site]\nname="T"\nslug="t"\nurl="https://x.example"\n'
            '[core]\nversion="1.10.6"\n[deploy.targets.prod]\npath="www"\n'
        },
    )
    assert main(["--site-dir", str(tmp_path), "deploy", "prod", "--verify-remote"]) == 2
    assert "sftp targets only" in capsys.readouterr().err
