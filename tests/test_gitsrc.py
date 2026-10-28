from __future__ import annotations

import pytest

from yourls_manager import gitsrc
from yourls_manager.util import YMError


def test_latest_stable_tag_skips_prereleases():
    tags = {"1.9.2": "a", "v1.10.0": "b", "1.10.6": "c", "1.11.0-rc1": "d", "1.10.10-beta": "e"}
    assert gitsrc.latest_stable_tag(tags) == ("1.10.6", "c")
    assert gitsrc.latest_stable_tag({"v2.0.0": "x", "2.0.0": "y"}) == ("2.0.0", "y")
    assert gitsrc.latest_stable_tag({"nightly": "x"}) is None


def test_parse_stable_semver():
    assert gitsrc.parse_stable_semver("v1.2.3") == (1, 2, 3)
    assert gitsrc.parse_stable_semver("1.2") is None
    assert gitsrc.parse_stable_semver("1.2.3-rc.1") is None


def test_resolve_and_fetch(upstream, tmp_path):
    up = upstream("plug")
    c1 = up.commit({"plugin.php": "v1"}, tag="1.0.0")
    c2 = up.commit({"plugin.php": "v2"}, tag="v1.1.0")
    up.commit({"plugin.php": "v3"}, tag="1.2.0-rc1")
    assert gitsrc.resolve_ref(up.url, "HEAD") == gitsrc.resolve_ref(up.url, "main")
    assert gitsrc.resolve_ref(up.url, "1.0.0") == c1  # annotated tag is peeled
    assert gitsrc.resolve_ref(up.url, c2) == c2
    assert gitsrc.resolve_core_tag(up.url, "latest") == ("v1.1.0", c2)
    assert gitsrc.resolve_core_tag(up.url, "1.1.0") == ("v1.1.0", c2)
    with pytest.raises(YMError):
        gitsrc.resolve_ref(up.url, "no-such-branch")
    with pytest.raises(YMError):
        gitsrc.resolve_core_tag(up.url, "9.9.9")

    co = gitsrc.fetch_commit(up.url, c1, tmp_path / "co")
    assert co.commit == c1
    assert (tmp_path / "co/plugin.php").read_text() == "v1"


def test_fetch_rejects_bad_input(tmp_path):
    with pytest.raises(YMError):
        gitsrc.fetch_commit("-oops", "a" * 40, tmp_path / "x")
    with pytest.raises(YMError):
        gitsrc.fetch_commit("file:///nope", "main", tmp_path / "y")
