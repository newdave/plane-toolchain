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


# --- PTC-61: validate the pg_dump backup before changing data ----------------------------------


@pytest.fixture
def fake():
    from fake_docker import FakeDocker
    from test_workspace_rename_deploy import ALL

    fake = FakeDocker()
    for name in [*ALL, "plane-api"]:
        fake.add(name)
    return fake


def backup_dir(tmp_path):
    directory = wr.secure_receipt_dir(tmp_path / "r")
    wr.new_receipt(directory, "a", "b")
    return directory


def test_backup_lists_the_dump_with_pg_restore_before_keeping_it(fake, tmp_path):
    directory = backup_dir(tmp_path)
    wr.backup_database("plane-db", directory, fake.popen)
    assert ("exec", "-i", "plane-db", "pg_restore", "-l") in fake.calls
    assert fake.restore_input == fake.pg_dump  # the whole dump went in on stdin
    assert (directory / "backup.dump").read_bytes() == fake.pg_dump
    assert not list(directory.glob("*.tmp"))


def test_dump_that_fails_pg_restore_list_is_refused_and_removed(fake, tmp_path):
    directory = backup_dir(tmp_path)
    fake.pg_restore_code = 1
    fake.pg_restore_stderr = b"pg_restore: error: unsupported version (1.99) in file header\n"
    with pytest.raises(RuntimeError, match=r"does not list with pg_restore -l") as caught:
        wr.backup_database("plane-db", directory, fake.popen)
    assert "unsupported version" in str(caught.value)
    assert not (directory / "backup.dump").exists()
    assert not list(directory.glob("*.tmp"))
    assert "backup_sha256" not in wr.load_receipt(directory, "a", "b")


def test_empty_pg_restore_listing_is_refused(fake, tmp_path):
    directory = backup_dir(tmp_path)
    fake.pg_restore_out = b""
    with pytest.raises(RuntimeError, match="does not list"):
        wr.backup_database("plane-db", directory, fake.popen)
    assert not (directory / "backup.dump").exists()


def test_failed_pg_dump_message_has_a_redacted_stderr_tail(fake, tmp_path):
    directory = backup_dir(tmp_path)
    fake.pg_dump_code = 1
    fake.pg_dump_stderr = (
        b"line one\n" * 50
        + b'pg_dump: error: connection to server at "db.internal" failed: '
        + b'FATAL: role "plane_secret_user" does not exist\n'
    )
    with pytest.raises(RuntimeError, match="pg_dump exited with an error") as caught:
        wr.backup_database("plane-db", directory, fake.popen)
    text = str(caught.value)
    assert "does not exist" in text and "<redacted>" in text
    assert "plane_secret_user" not in text and "db.internal" not in text
    assert len(text) < 500


def test_low_free_space_refuses_before_pg_dump(fake, tmp_path, monkeypatch):
    directory = backup_dir(tmp_path)
    fake.db_size = 5 << 30
    real = os.statvfs(directory)

    class Low:
        f_bavail, f_frsize = 1000, real.f_frsize

    monkeypatch.setattr(os, "statvfs", lambda path: Low)
    with pytest.raises(RuntimeError, match="not enough free space"):
        wr.backup_database("plane-db", directory, fake.popen)
    assert not any("pg_dump" in " ".join(call) for call in fake.calls)
    assert not list(directory.glob("backup*"))


def test_unknown_database_size_falls_back_to_the_minimum(fake, tmp_path, monkeypatch):
    directory = backup_dir(tmp_path)
    fake.db_size = None

    class Low:
        f_bavail, f_frsize = 1, 4096

    monkeypatch.setattr(os, "statvfs", lambda path: Low)
    with pytest.raises(RuntimeError, match="not enough free space"):
        wr.backup_database("plane-db", directory, fake.popen)


def test_invalid_dump_stops_apply_before_the_slug_change(host):
    host.fake.pg_restore_code = 1
    with pytest.raises(RuntimeError, match="Rename failed before the slug change"):
        host.deploy()
    assert "step_slug" not in host.ops()
    assert host.receipt()["step"] == "pending"
    assert all(host.running().values())  # services restarted
