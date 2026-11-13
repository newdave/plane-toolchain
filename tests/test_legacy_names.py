"""Guard: legacy names live in `plane_toolchain/legacy.py`; no new references elsewhere."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NEEDLE = "terrainlink"

PERMANENT = {
    "src/plane_toolchain/legacy.py",
    "docs/decisions/0001-neutral-names.md",
    "CHANGELOG.md",
    "tests/test_legacy_names.py",
    # Tripwire fixtures: the deploy helpers refuse these legacy-named states.
    "tests/test_pages_deploy.py",
    "tests/test_mcp_deploy.py",
}
ALLOWED = PERMANENT


def offenders(paths, reader, allowed=ALLOWED):
    """Return paths outside `allowed` whose name or text mentions the legacy name, any case."""
    return sorted(
        p
        for p in paths
        if p not in allowed and (NEEDLE in p.lower() or NEEDLE in reader(p).lower())
    )


def tracked():
    out = subprocess.run(  # noqa: S603 - fixed argv
        ["git", "ls-files", "-z"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout
    return [p for p in out.decode().split("\0") if p]


def read(path):
    try:
        return (ROOT / path).read_text(errors="ignore")
    except OSError:  # deleted in the working tree
        return ""


def test_no_new_legacy_references():
    assert offenders(tracked(), read) == []


def test_guard_fails_on_new_occurrence_and_passes_on_allowed():
    files = {"src/plane_toolchain/new.py": "x = 'TerrainLink'", "CHANGELOG.md": "terrainlink"}
    assert offenders(files, files.__getitem__) == ["src/plane_toolchain/new.py"]
    assert offenders(["CHANGELOG.md", "tests/test_pages_deploy.py"], lambda _: "terrainlink") == []
    assert offenders(["a.py"], lambda _: "clean") == []


def test_guard_fails_on_legacy_name_in_path():
    paths = ["tests/TerrainLink_fixture.json", "CHANGELOG.md"]
    assert offenders(paths, lambda _: "{}") == ["tests/TerrainLink_fixture.json"]
