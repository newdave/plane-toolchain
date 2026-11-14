"""Rename hardening: run lock and backup verification."""

import os
import stat
import pytest
from support_workspace_rename_deploy import mutating, run_remote
from plane_toolchain import workspace_rename as wr
from support_workspace_rename_hardening import backup_dir, hold_lock
from support_workspace_rename_hardening import fake  # noqa: F401
from support_workspace_rename_hardening import host  # noqa: F401


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


def test_backup_reads_the_whole_dump_with_pg_restore_before_keeping_it(fake, tmp_path):
    directory = backup_dir(tmp_path)
    wr.backup_database("plane-db", directory, fake.popen)
    assert ("exec", "-i", "plane-db", "pg_restore", "-f", "/dev/null") in fake.calls
    assert fake.restore_input == fake.pg_dump  # the whole dump went in on stdin
    assert (directory / "backup.dump").read_bytes() == fake.pg_dump
    assert not list(directory.glob("*.tmp"))


def test_dump_that_fails_pg_restore_is_refused_and_removed(fake, tmp_path):
    directory = backup_dir(tmp_path)
    fake.pg_restore_code = 1
    fake.pg_restore_stderr = b"pg_restore: error: unsupported version (1.99) in file header\n"
    with pytest.raises(RuntimeError, match=r"pg_restore cannot read the dump") as caught:
        wr.backup_database("plane-db", directory, fake.popen)
    assert "unsupported version" in str(caught.value)
    assert not (directory / "backup.dump").exists()
    assert not list(directory.glob("*.tmp"))
    assert "backup_sha256" not in wr.load_receipt(directory, "a", "b")


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


def fake_free(monkeypatch, free_bytes):
    class Free:
        f_bavail, f_frsize = free_bytes // 4096, 4096

    monkeypatch.setattr(os, "statvfs", lambda path: Free)


def test_free_space_leaves_room_for_the_dump_and_the_journal(fake, tmp_path, monkeypatch):
    directory = backup_dir(tmp_path)
    fake.db_size = 200 << 20
    fake_free(monkeypatch, 300 << 20)  # a dump fits, dump plus journal does not
    with pytest.raises(RuntimeError, match="not enough free space"):
        wr.check_free_space(directory, "plane-db", fake.popen)
    fake_free(monkeypatch, 450 << 20)
    wr.check_free_space(directory, "plane-db", fake.popen)
