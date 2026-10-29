"""`ym artifact build`: reproducible release archives of the built web root.

For sites that are not deployed by ym: CI (or the user) builds the tree of a release tag
into ``.tar.gz`` / ``.zip`` files with a ``.sha256`` per archive and one ``<name>.json``
metadata file (:func:`metadata`). ym never uploads them; publishing is the site template's
CI job. A REF that is itself a release tag packages that tag; otherwise the commit's newest
release tag wins. A REF other than HEAD is built from a temporary ``git worktree``.

Archives are byte-identical for the same tag (and the same ym and zlib): entries sorted by
path, every mtime = the commit's committer time, owner 0/0 without names, mode 0644 for
files and 0755 for directories; the gzip header has mtime 0 and no file name.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import gzip
import hashlib
import json
import os
import tarfile
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .config import SiteConfig, load_site
from .lockfile import load_lock
from .release import ReleaseTag, commit_of, is_dirty, is_git_repo, parse_tag, tags_at
from .util import UsageError, YMError, console, iso_utc, run
from .vendor import rmtree

METADATA_SCHEMA = 1
FILE_MODE = 0o644
DIR_MODE = 0o755
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)  # zip can't store earlier times


@dataclass(frozen=True)
class Release:
    """What is being packaged: the commit and its release tag (None when untagged)."""

    commit: str
    committed: int  # committer time, seconds since the epoch
    tag: ReleaseTag | None

    @property
    def version(self) -> str:
        if self.tag is None:
            return f"0.0.0-dev+{self.commit[:7]}"
        return self.tag.name.removeprefix("v")


def _git(root: Path, *args: str) -> str:
    return run(["git", *args], cwd=root).stdout.decode().strip()


def resolve_release(root: Path, ref: str, *, allow_untagged: bool = False) -> Release:
    """REF's commit and release tag; refuses untagged commits and dirty trees (exit 2)."""
    if not is_git_repo(root):
        raise UsageError(f"{root} is not a git repository")
    commit = commit_of(root, ref)
    if commit is None:
        raise UsageError(f"--ref {ref}: no such commit, branch or tag")
    if is_dirty(root):
        raise UsageError("uncommitted changes: commit them first (`git status`)")
    named = parse_tag(ref.removeprefix("refs/tags/"))
    if named is not None and commit_of(root, f"refs/tags/{named.name}") == commit:
        tag: ReleaseTag | None = named  # REF is a release tag: package exactly that one
    else:
        at = tags_at(root, commit)
        tag = at[-1] if at else None  # several tags on one commit: the newest wins
    if tag is None and not allow_untagged:
        raise UsageError(
            f"{ref} ({commit[:7]}) has no release tag (v<yourls>-<rev>[-rc.<n>]): create one"
            " with `ym release`, or pass --allow-untagged for a local test build"
        )
    committed = int(_git(root, "show", "-s", "--format=%ct", commit))
    return Release(commit=commit, committed=committed, tag=tag)


@contextlib.contextmanager
def _source(cfg: SiteConfig, commit: str):
    """The site at ``commit``: the working tree when that is HEAD (clean, checked by the
    caller), else a temporary detached git worktree."""
    if commit_of(cfg.root, "HEAD") == commit:
        yield cfg
        return
    tmp = Path(tempfile.mkdtemp(prefix="ym-artifact-src-"))
    wt = tmp / "site"
    try:
        run(["git", "worktree", "add", "--detach", "--quiet", str(wt), commit], cwd=cfg.root)
        yield load_site(wt)
    finally:
        run(["git", "worktree", "remove", "--force", str(wt)], cwd=cfg.root, check=False)
        run(["git", "worktree", "prune"], cwd=cfg.root, check=False)
        rmtree(tmp)


# --------------------------------------------------------------------------- archives


def _entries(tree: Path) -> list[tuple[str, Path]]:
    """(posix relpath, path) of every directory and regular file under tree, sorted."""
    out: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(tree):
        base = Path(dirpath)
        for name in dirnames + filenames:
            p = base / name
            if p.is_symlink() or not (p.is_dir() or p.is_file()):
                raise YMError(f"unsupported file type in the build: {p.relative_to(tree)}")
            out.append((p.relative_to(tree).as_posix(), p))
    out.sort(key=lambda e: e[0])
    return out


def write_tar_gz(tree: Path, dest: Path, mtime: int) -> None:
    with dest.open("wb") as raw, gzip.GzipFile(
        filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0
    ) as gz, tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:  # fmt: skip
        for rel, path in _entries(tree):
            info = tarfile.TarInfo(rel)
            info.mtime = mtime
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            if path.is_dir():
                info.type = tarfile.DIRTYPE
                info.mode = DIR_MODE
                tar.addfile(info)
            else:
                info.mode = FILE_MODE
                info.size = path.stat().st_size
                with path.open("rb") as fh:
                    tar.addfile(info, fh)


def write_zip(tree: Path, dest: Path, mtime: int) -> None:
    stamp = max(tuple(time.gmtime(mtime)[:6]), _ZIP_EPOCH)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for rel, path in _entries(tree):
            is_dir = path.is_dir()
            info = zipfile.ZipInfo(rel + "/" if is_dir else rel, date_time=stamp)
            info.create_system = 3  # unix, on every platform, so the mode bits are read
            if is_dir:
                info.external_attr = ((0o040000 | DIR_MODE) << 16) | 0x10
                zf.writestr(info, b"")
            else:
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (0o100000 | FILE_MODE) << 16
                zf.writestr(info, path.read_bytes())


WRITERS = {"tar.gz": write_tar_gz, "zip": write_zip}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _replace(dest: Path, write) -> None:
    """Write ``dest`` via a temp file next to it, then rename (replaces an existing file)."""
    tmp = dest.with_name(f".{dest.name}.tmp-{os.getpid()}")
    try:
        write(tmp)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def metadata(cfg: SiteConfig, rel: Release, files: dict[str, str]) -> dict:
    """The ``<name>.json`` written next to the archives; consumers rely on these fields::

        {"schema": 1, "site": "<slug>", "version": "1.10.6-2", "tag": "v1.10.6-2",
         "rc": false, "commit": "<sha>", "committed": "<ISO 8601 UTC>",
         "core": {"version": "1.10.6", "commit": "<sha>"},
         "plugins": {"<name>": {"repo": "...", "commit": "<sha>"}},
         "manager": "<ym version>", "files": {"<archive file>": "<sha256>"}}

    ``--allow-untagged`` builds have ``"tag": null`` and ``"untagged": true``.
    """
    lock = load_lock(cfg.lock_file)
    data: dict = {
        "schema": METADATA_SCHEMA,
        "site": cfg.site.slug,
        "version": rel.version,
        "tag": rel.tag.name if rel.tag else None,
        "rc": bool(rel.tag and rel.tag.is_rc),
        "commit": rel.commit,
        "committed": iso_utc(_dt.datetime.fromtimestamp(rel.committed, _dt.UTC)),
        "core": {"version": lock.core.version, "commit": lock.core.commit} if lock.core else None,
        "plugins": {
            name: {"repo": p.repo, "commit": p.commit} for name, p in sorted(lock.plugins.items())
        },
        "manager": __version__,
        "files": dict(sorted(files.items())),
    }
    if rel.tag is None:
        data["untagged"] = True
    return data


# --------------------------------------------------------------------------- command


def artifact_build(
    cfg: SiteConfig,
    *,
    ref: str = "HEAD",
    out: Path | None = None,
    formats: list[str] | None = None,
    allow_untagged: bool = False,
) -> list[Path]:
    """Build the release at REF into archives + sidecars. Returns the written files."""
    from .build import build_site

    rel = resolve_release(cfg.root, ref, allow_untagged=allow_untagged)
    if rel.tag is None:
        console.warn(f"untagged build of {rel.commit[:7]}: version {rel.version} (testing only)")
    out_dir = (out or cfg.root / f"{cfg.build.out}-artifacts").resolve()
    written: list[Path] = []
    with _source(cfg, rel.commit) as src:
        spec = src.artifact
        fmts = list(dict.fromkeys(formats or spec.formats))
        name = spec.render_name(
            slug=src.site.slug, version=rel.version, tag=rel.tag.name if rel.tag else rel.version
        )
        console.step(f"artifact {name} ({rel.tag.name if rel.tag else 'untagged'}"
                     f" @ {rel.commit[:7]})")  # fmt: skip
        tmp = Path(tempfile.mkdtemp(prefix="ym-artifact-"))
        try:
            tree = build_site(src, tmp / "build")
            out_dir.mkdir(parents=True, exist_ok=True)
            files: dict[str, str] = {}
            for fmt in fmts:
                archive = out_dir / f"{name}.{fmt}"
                _replace(archive, lambda p, fmt=fmt: WRITERS[fmt](tree, p, rel.committed))
                digest = _sha256(archive)
                files[archive.name] = digest
                side = archive.with_name(archive.name + ".sha256")
                _replace(side, lambda p, d=digest, a=archive: p.write_text(
                    f"{d}  {a.name}\n", encoding="utf-8", newline="\n"))  # fmt: skip
                written += [archive, side]
                console.info(f"  {archive.name}  sha256 {digest}")
            meta = out_dir / f"{name}.json"
            text = json.dumps(metadata(src, rel, files), indent=2, sort_keys=True) + "\n"
            _replace(meta, lambda p: p.write_text(text, encoding="utf-8", newline="\n"))
            written.append(meta)
        finally:
            rmtree(tmp)
    console.info(f"artifacts: {out_dir} ({', '.join(p.name for p in written)})")
    return written
