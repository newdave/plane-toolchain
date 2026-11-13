"""Hardening of the workspace rename host program (PTC-59 to PTC-63)."""

import fcntl
import os
import stat

import pytest
from test_workspace_rename_deploy import Host, mutating, run_remote

from plane_toolchain import workspace_rename as wr


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


def hold_lock(directory):
    """Hold the run lock the way another process would: a separate open file description."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(directory / "run.lock", os.O_RDWR | os.O_CREAT, 0o600)
    handle = os.fdopen(descriptor, "a")
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


# --- PTC-59: refuse concurrent apply and revert -----------------------------------------------


@pytest.mark.parametrize("operation", ["apply", "revert"])
def test_held_lock_refuses_before_any_stop_or_snippet(host, operation):
    if operation == "revert":
        host.deploy()  # a receipt to revert
        host.fake.calls.clear()
        host.shell.requests.clear()
    handle = hold_lock(host.directory)
    try:
        with pytest.raises(RuntimeError, match="Refused: another .* holds the lock"):
            host.deploy(operation)
    finally:
        handle.close()
    assert not mutating(host.fake)
    assert not host.shell.requests
    assert not any(call[0] == "inspect" for call in host.fake.calls)


def test_lock_released_after_the_run_so_a_second_run_proceeds(host):
    assert host.deploy().startswith("Rename complete")
    assert host.deploy().startswith("Rename already complete")
    assert host.deploy("revert").startswith("Revert complete")
    handle = hold_lock(host.directory)  # free again once the runs are over
    handle.close()


def test_lock_released_after_a_refused_run(host):
    host.deploy()
    host.deploy("revert")
    with pytest.raises(RuntimeError, match="reverted"):
        host.deploy()
    handle = hold_lock(host.directory)
    handle.close()


def test_check_takes_no_lock_and_writes_nothing(host):
    handle = hold_lock(host.directory)
    try:
        assert host.deploy("check").startswith("Check passed")
    finally:
        handle.close()


def test_lock_file_is_private(host):
    host.deploy()
    info = (host.directory / "run.lock").stat()
    assert stat.S_IMODE(info.st_mode) == 0o600


def test_lock_file_with_loose_mode_or_symlink_is_refused(host, tmp_path):
    wr.secure_receipt_dir(host.directory)
    lock = host.directory / "run.lock"
    lock.write_text("")
    lock.chmod(0o644)
    with pytest.raises(RuntimeError, match="loose permissions"):
        host.deploy()
    lock.unlink()
    other = tmp_path / "other"
    other.write_text("")
    other.chmod(0o600)
    lock.symlink_to(other)
    with pytest.raises(RuntimeError, match="not a plain file"):
        host.deploy()
    assert not mutating(host.fake)


def test_revert_without_a_receipt_directory_creates_nothing(host):
    with pytest.raises(RuntimeError, match="no receipt"):
        host.deploy("revert")
    assert not host.directory.exists()


def test_lock_through_the_assembled_program(host, monkeypatch):
    handle = hold_lock(host.directory)
    try:
        assert run_remote(wr.REMOTE, host.payload(), host, monkeypatch) == 1
    finally:
        handle.close()
    assert not mutating(host.fake)
    assert run_remote(wr.REMOTE, host.payload(), host, monkeypatch) == 0
    assert os.path.lexists(host.directory / "run.lock")
