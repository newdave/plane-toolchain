"""pyproject version must match the newest released CHANGELOG entry."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_pyproject_version_matches_changelog():
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    released = re.findall(
        r"^## \[(\d+\.\d+\.\d+)\] - \d{4}-\d{2}-\d{2}$",
        (ROOT / "CHANGELOG.md").read_text(),
        re.M,
    )
    assert released, "CHANGELOG.md has no released version"
    assert released[0] == version
