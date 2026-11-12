"""Activation remains reversible and does not overwrite unrelated local settings."""

import pytest

from plane_toolchain.config import set_enabled


def test_apply_revert_baseline_and_explicit_disable(tmp_path):
    path = tmp_path / "config/local.env"
    path.parent.mkdir()
    path.write_text("OTHER=kept\nPLANE_ENABLED=true\n")
    set_enabled(path, True)
    set_enabled(path, True)
    set_enabled(path, False)
    assert path.read_text() == "OTHER=kept\nPLANE_ENABLED=true\n"
    set_enabled(path, False, restore=False)
    assert path.read_text() == "OTHER=kept\nPLANE_ENABLED=false\n"


def test_symlinked_local_config_rejected(tmp_path):
    target = tmp_path / "target"
    target.write_text("preserve")
    path = tmp_path / "local.env"
    path.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        set_enabled(path, True)
    assert target.read_text() == "preserve"
