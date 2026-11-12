"""Activation remains reversible and does not overwrite unrelated local settings."""

import pytest

from plane_toolchain import session
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


@pytest.mark.parametrize(
    "line", ['export PLANE_ENABLED="true"', "PLANE_ENABLED='true'", "PLANE_ENABLED=true # note"]
)
def test_enable_accepts_what_status_accepts(tmp_path, line):

    path = tmp_path / "config/local.env"
    path.parent.mkdir()
    path.write_text(line + "\n")
    assert session.switch(tmp_path, {}) == "enabled"
    set_enabled(path, True)
    set_enabled(path, False)
    assert session.switch(tmp_path, {}) == "enabled"


def test_enable_still_rejects_invalid_value(tmp_path):
    path = tmp_path / "config/local.env"
    path.parent.mkdir()
    path.write_text("PLANE_ENABLED=$(touch x)\n")
    with pytest.raises(ValueError, match="literal"):
        set_enabled(path, True)


def test_interrupted_backup_leaves_no_partial_file(tmp_path, monkeypatch):
    import os

    path = tmp_path / "config/local.env"
    path.parent.mkdir()
    path.write_text("PLANE_ENABLED=true\n")
    real = os.replace

    def boom(src, dst):
        if str(dst).endswith("enable-backup.json"):
            raise OSError("interrupted")
        return real(src, dst)

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        set_enabled(path, True)
    state = tmp_path / ".agent-state/plane"
    assert not (state / "enable-backup.json").exists()
    assert list(state.iterdir()) == []
    monkeypatch.setattr(os, "replace", real)
    set_enabled(path, True)
    set_enabled(path, False)
    assert session.switch(tmp_path, {}) == "enabled"
