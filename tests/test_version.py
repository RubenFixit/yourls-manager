"""The version lives in two files; they must agree (no php CLI needed)."""

import re
from pathlib import Path

from yourls_manager import __version__
from yourls_manager.deploy.rsync import helper_source


def test_helper_version_matches_package():
    text = Path(str(helper_source())).read_text(encoding="utf-8")
    m = re.search(r"^const YM_HELPER_VERSION = '([^']+)';", text, re.M)
    assert m, "YM_HELPER_VERSION not found in ym-helper.php"
    assert m[1] == __version__, "bump YM_HELPER_VERSION in php/ym-helper.php with __version__"
