"""Copy, hash and atomically swap vendored trees."""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from .util import YMError, console

# Files kept in core even when a wildcard exclude would match them (design §2).
PROTECTED_CORE_FILES = frozenset({"LICENSE", "LICENSE.md", "LICENSE.txt", "readme.html"})

PathFilter = Callable[[str], bool]


# --------------------------------------------------------------------------- matching


def _match_segments(pat: list[str], path: list[str]) -> bool:
    if not pat:
        return not path
    head = pat[0]
    if head == "**":
        return any(_match_segments(pat[1:], path[i:]) for i in range(len(path) + 1))
    return bool(path) and fnmatchcase(path[0], head) and _match_segments(pat[1:], path[1:])


def glob_match(pattern: str, relpath: str) -> bool:
    """Anchored glob match against ``relpath`` or any of its ancestor directories.

    ``*`` and ``?`` never cross ``/``; a ``**`` segment matches any number of segments.
    So ``.github`` excludes the whole root ``.github/`` dir and ``*.md`` matches only
    root-level Markdown files (use ``**/*.md`` for every level).
    """
    pat = [s for s in pattern.strip("/").split("/") if s]
    parts = relpath.split("/")
    return any(_match_segments(pat, parts[:n]) for n in range(1, len(parts) + 1))


def make_exclude(patterns: Iterable[str]) -> PathFilter:
    pats = list(patterns)

    def excluded(relpath: str) -> bool:
        hit = [p for p in pats if glob_match(p, relpath)]
        if not hit:
            return False
        if relpath in PROTECTED_CORE_FILES:
            return any(p.strip("/") == relpath for p in hit)
        return True

    return excluded


def _never(_: str) -> bool:
    return False


# --------------------------------------------------------------------------- walking


def iter_files(root: Path, skip: PathFilter = _never) -> Iterator[tuple[str, Path]]:
    """Yield (posix relpath, path) for regular files under root, sorted by relpath.

    ``.git`` directories are always skipped. Symlinks are resolved only when they point
    to a regular file inside ``root``; anything else is an error (never follow out).
    """
    root = root.resolve()
    results: list[tuple[str, Path]] = []

    def walk(directory: Path, rel: PurePosixPath | None) -> None:
        with os.scandir(directory) as it:
            entries = sorted(it, key=lambda e: e.name)
        for entry in entries:
            relpath = str(rel / entry.name) if rel else entry.name
            if entry.name == ".git":
                continue
            if skip(relpath):
                continue
            p = Path(entry.path)
            if entry.is_symlink():
                target = p.resolve()
                try:
                    target.relative_to(root)
                except ValueError:
                    raise YMError(f"symlink escapes source tree: {relpath} -> {target}") from None
                if not target.is_file():
                    raise YMError(f"unsupported symlink (not a regular file): {relpath}")
                results.append((relpath, target))
            elif entry.is_dir(follow_symlinks=False):
                walk(p, PurePosixPath(relpath))
            elif entry.is_file(follow_symlinks=False):
                results.append((relpath, p))

    walk(root, None)
    results.sort(key=lambda t: t[0])
    return iter(results)


def tree_sha256(root: Path, skip: PathFilter = _never) -> str:
    """sha256 over sorted ``relpath\\0bytes\\0`` of every file under root."""
    h = hashlib.sha256()
    for relpath, path in iter_files(root, skip):
        h.update(relpath.encode("utf-8"))
        h.update(b"\0")
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        h.update(b"\0")
    return h.hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_tree(
    src: Path,
    dst: Path,
    skip: PathFilter = _never,
    *,
    on_copy: Callable[[str, bool], None] | None = None,
) -> int:
    """Copy regular files from src into dst (merging). Returns the number of files copied.

    ``on_copy(relpath, overwritten)`` is called for each file.
    """
    count = 0
    for relpath, path in iter_files(src, skip):
        target = dst.joinpath(*relpath.split("/"))
        existed = target.exists()
        if target.is_dir() and not target.is_symlink():
            raise YMError(f"cannot overwrite directory with file: {relpath}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if existed or target.is_symlink():
            target.unlink()
        shutil.copy2(path, target)
        count += 1
        if on_copy:
            on_copy(relpath, existed)
    return count


def prefix_filter(prefixes: Iterable[str]) -> PathFilter:
    """Filter that skips the given relative directory prefixes (and their contents)."""
    pres = tuple(p.strip("/") for p in prefixes)

    def skip(relpath: str) -> bool:
        return any(relpath == p or relpath.startswith(p + "/") for p in pres)

    return skip


# --------------------------------------------------------------------------- staging


@contextlib.contextmanager
def staging_dir(parent: Path, prefix: str = ".ym-stage-") -> Iterator[Path]:
    """A temp dir on the same filesystem as ``parent`` (so renames are atomic)."""
    parent.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    try:
        yield path
    finally:
        rmtree(path)


def rmtree(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return

    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    # Make read-only dirs (e.g. git pack files) removable before deleting.
    for dirpath, dirnames, _ in os.walk(path):
        for d in dirnames:
            p = Path(dirpath, d)
            if not p.is_symlink():
                with contextlib.suppress(OSError):
                    p.chmod(0o700)
    shutil.rmtree(path)


def atomic_replace_dir(target: Path, new: Path) -> None:
    """Swap ``new`` into place at ``target``.

    The old tree is renamed to ``.<name>.old-<ts>`` first, the new one moved in, then the
    old one deleted; if moving the new tree in fails, the old one is restored.
    """
    if new.resolve() == target.resolve():
        raise YMError("atomic_replace_dir: source and target are the same")
    target.parent.mkdir(parents=True, exist_ok=True)
    old: Path | None = None
    if target.exists() or target.is_symlink():
        old = target.with_name(f".{target.name}.old-{time.strftime('%Y%m%d%H%M%S')}-{os.getpid()}")
        os.replace(target, old)
    try:
        os.replace(new, target)
    except BaseException:
        if old is not None:
            if target.exists():
                rmtree(target)
            os.replace(old, target)
        raise
    if old is not None:
        try:
            rmtree(old)
        except OSError as exc:  # pragma: no cover
            console.warn(f"could not remove old tree {old}: {exc}")
