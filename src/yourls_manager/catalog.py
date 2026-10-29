"""The awesome-yourls plugin catalog: fetch, cache, search, resolve."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .util import UsageError, YMError, console

CATALOG_URL = "https://raw.githubusercontent.com/YOURLS/awesome/main/data/plugins.yml"
CACHE_NAME = "plugins.yml"
MAX_AGE = 24 * 3600


@dataclass(frozen=True)
class Entry:
    name: str
    url: str
    description: str = ""
    category: str = ""
    tags: tuple[str, ...] = ()
    official: bool = False
    tested: bool = False


@dataclass
class Catalog:
    entries: list[Entry]
    categories: dict[str, str] = field(default_factory=dict)  # slug -> label


def catalog_url() -> str:
    return os.environ.get("YM_CATALOG_URL") or CATALOG_URL


def cache_file() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "yourls-manager" / CACHE_NAME


def _download(url: str) -> bytes:
    from .verify import USER_AGENT

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except (urllib.error.URLError, OSError) as err:
        raise YMError(f"could not download the plugin catalog {url}: "
                      f"{getattr(err, 'reason', err)}") from None  # fmt: skip


def load_catalog(*, refresh: bool = False) -> Catalog:
    """Cached for a day; ``refresh`` forces a download. A stale cache beats no catalog."""
    path = cache_file()
    fresh = path.is_file() and time.time() - path.stat().st_mtime < MAX_AGE
    if refresh or not fresh:
        try:
            data = _download(catalog_url())
            parse_catalog(data)  # don't cache garbage
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        except YMError as exc:
            if not path.is_file():
                raise
            console.warn(f"{exc}; using the cached copy from {time.ctime(path.stat().st_mtime)}")
    return parse_catalog(path.read_bytes())


def parse_catalog(data: bytes) -> Catalog:
    import yaml

    try:
        raw = yaml.safe_load(data.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise YMError(f"the plugin catalog is not valid YAML: {exc}") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("plugins"), list):
        raise YMError("the plugin catalog has no `plugins` list")
    cats: dict[str, str] = {}
    for c in raw.get("categories") or []:
        if isinstance(c, dict) and c.get("slug"):
            cats[str(c["slug"])] = str(c.get("label") or c["slug"])
    entries = []
    for p in raw["plugins"]:
        if not isinstance(p, dict) or not p.get("name") or not p.get("url"):
            continue
        tags = p.get("tags") or []
        entries.append(
            Entry(
                name=str(p["name"]).strip(),
                url=str(p["url"]).strip(),
                description=str(p.get("description") or "").strip(),
                category=str(p.get("category") or ""),
                tags=tuple(str(t).lower() for t in tags) if isinstance(tags, list) else (),
                official=p.get("official") is True,
                tested=p.get("tested") is True,
            )
        )
    return Catalog(entries, cats)


# --------------------------------------------------------------------------- search

_WORD = re.compile(r"[a-z0-9]+")


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def score(entry: Entry, terms: list[str]) -> int:
    """name > tags > description; every term has to match somewhere (0 = no match)."""
    name = entry.name.lower()
    name_words = _words(name)
    tag_words = [w for t in entry.tags for w in _words(t)]
    desc = entry.description.lower()
    total = 0
    for term in terms:
        if term in name_words:
            s = 30
        elif term in name:
            s = 20
        elif term in entry.tags or term in tag_words:
            s = 10
        elif any(t.startswith(term) for t in (*entry.tags, *tag_words)):
            s = 6
        elif term in desc:
            s = 2
        else:
            return 0
        total += s
    if terms and " ".join(terms) == " ".join(name_words):
        total += 50
    return total


def search(catalog: Catalog, query: str, category: str | None = None) -> list[Entry]:
    if category and category not in catalog.categories:
        known = ", ".join(sorted(catalog.categories))
        raise UsageError(f"unknown category {category!r}; categories: {known}")
    pool = [e for e in catalog.entries if not category or e.category == category]
    terms = _words(query)
    if not terms:
        if not category:
            raise UsageError("give a search QUERY, or --category to list a category")
        return sorted(pool, key=lambda e: e.name.lower())
    scored = [(score(e, terms), e) for e in pool]
    return [e for s, e in sorted(scored, key=lambda x: (-x[0], x[1].name.lower())) if s]


def find(catalog: Catalog, name: str) -> Entry:
    """Exact name, then case-insensitive, then unique (case-insensitive) prefix."""
    for e in catalog.entries:
        if e.name == name:
            return e
    low = name.lower().strip()
    hits = [e for e in catalog.entries if e.name.lower() == low]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        hits = [e for e in catalog.entries if e.name.lower().startswith(low)]
        if len(hits) == 1:
            return hits[0]
    if not hits:
        raise UsageError(
            f"no plugin named {name!r} in the catalog; try `ym plugin search {name}`"
            " or pass a git URL"
        )
    shown = "\n  ".join(e.name for e in hits[:15])
    raise UsageError(f"{name!r} matches several catalog plugins:\n  {shown}")


# --------------------------------------------------------------------------- site matching


def repo_key(url: str) -> tuple[str, str] | None:
    """Comparable (repo, path) for a catalog URL or a site.toml repo/path."""
    from .plugincmd import parse_plugin_url

    try:
        repo, _ref, path = parse_plugin_url(url)
    except UsageError:
        return None
    repo = re.sub(r"^(git@([^:]+):)", r"https://\2/", repo)
    repo = repo.lower().removesuffix("/").removesuffix(".git")
    return repo, (path or ".").strip("/") or "."


def site_matches(cfg, entries: list[Entry]) -> dict[str, str]:
    """{catalog name: site plugin name} for plugins already in site.toml."""
    have: dict[tuple[str, str], str] = {}
    for p in cfg.plugins:
        if p.repo:
            key = repo_key(p.repo)
            if key:
                have[(key[0], p.path.strip("/") or ".")] = p.name
    out = {}
    for e in entries:
        key = repo_key(e.url)
        if key and key in have:
            out[e.name] = have[key]
    return out


def print_results(catalog: Catalog, results: list[Entry], in_site: dict[str, str],
                  *, as_json: bool) -> None:  # fmt: skip
    if as_json:
        rows = []
        for e in results:
            d = asdict(e)
            d["tags"] = list(e.tags)
            d["in_site"] = in_site.get(e.name)
            rows.append(d)
        console.info(json.dumps(rows, indent=2))
        return
    if not results:
        console.info("no matching plugins")
        return
    for e in results:
        badges = [b for b, on in (("official", e.official), ("tested", e.tested)) if on]
        head = f"{e.name}  [{e.category or '-'}]"
        if badges:
            head += "  " + " ".join(f"({b})" for b in badges)
        if e.name in in_site:
            head += f"  * in site.toml as {in_site[e.name]}"
        console.info(head)
        console.info(f"    {e.url}")
        if e.description:
            console.info(f"    {e.description}")
    console.info(f'{len(results)} plugin(s); add one with `ym plugin add "NAME"`')


def plugin_search(cfg, query: str, *, category: str | None, refresh: bool,
                  as_json: bool) -> list[Entry]:  # fmt: skip
    catalog = load_catalog(refresh=refresh)
    results = search(catalog, query, category)
    in_site = site_matches(cfg, results) if cfg is not None else {}
    print_results(catalog, results, in_site, as_json=as_json)
    return results


__all__ = ["CATALOG_URL", "Catalog", "Entry", "find", "load_catalog", "plugin_search", "search"]
