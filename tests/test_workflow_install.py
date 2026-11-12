"""Owner tasks preserve existing or subsequently changed workflow files."""

import runpy
from pathlib import Path

import pytest

install = runpy.run_path(str(Path(__file__).parents[1] / "scripts/github_examples.py"))["install"]


def test_apply_idempotent_revert_and_preserve_unrelated(tmp_path):
    directory = tmp_path / ".github/workflows"
    directory.mkdir(parents=True)
    unrelated = directory / "ci.yml"
    unrelated.write_text("keep")
    install("apply", tmp_path)
    install("apply", tmp_path)
    assert len(list(directory.glob("plane-*.yml"))) == 3
    install("revert", tmp_path)
    assert not list(directory.glob("plane-*.yml"))
    assert unrelated.read_text() == "keep"


def test_existing_and_changed_files_never_overwritten_or_removed(tmp_path):
    directory = tmp_path / ".github/workflows"
    directory.mkdir(parents=True)
    path = directory / "plane-check.yml"
    path.write_text("user work")
    with pytest.raises(ValueError, match="exists"):
        install("apply", tmp_path)
    assert path.read_text() == "user work"
    path.unlink()
    install("apply", tmp_path)
    path.write_text("user edited")
    with pytest.raises(ValueError, match="changed"):
        install("revert", tmp_path)
    assert path.read_text() == "user edited"
    assert len(list(directory.glob("plane-*.yml"))) == 3
