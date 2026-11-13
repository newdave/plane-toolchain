"""Hardening of the workspace rename host program (PTC-59 to PTC-63)."""

import fcntl
import os
import shutil
import signal
import stat
import sys
import time

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


# --- PTC-60: deadline and verified stop for the Django snippet ---------------------------------


def first_index(calls, predicate):
    return next((i for i, call in enumerate(calls) if predicate(call)), None)


def is_kill(call):
    return call[:1] == ("exec",) and "-c" in call and "PLANE_RENAME_RUN" in call[-3]


def is_start(call):
    return call[0] in ("start", "restart") and call[1] != "plane-live"


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(wr, "SNIPPET_IDLE_SECONDS", 0.2)


def snippet_call(host, op="check"):
    return wr.run_snippet(
        "plane-api",
        wr.snippet_source(),
        op,
        {"old": "old-slug", "new": "new-slug"},
        wr.secure_receipt_dir(host.directory),
        host.fake.popen,
    )


def test_snippet_runs_with_a_per_run_marker_in_its_environment(host):
    snippet_call(host)
    argv = next(c for c in host.fake.calls if "manage.py" in c)
    assert argv[:3] == ("exec", "-i", "-e")
    name, _, token = argv[3].partition("=")
    assert name == "PLANE_RENAME_RUN" and len(token) >= 16


def test_hung_snippet_hits_the_deadline_and_is_stopped_in_the_container(host, quick):
    host.fake.hang_ops = {"check"}
    started = time.monotonic()
    with pytest.raises(RuntimeError, match=r"check timed out after 0\.2 s .* was stopped"):
        snippet_call(host)
    assert time.monotonic() - started < 5
    assert host.fake.procs == []
    assert len(host.fake.kill_calls) == 1 and host.fake.kill_calls[0]


def test_a_snippet_that_keeps_answering_is_not_cut_off(host, quick):
    summary, _ = snippet_call(host)
    assert summary["ok"] is True
    assert host.fake.kill_calls == []


def test_unconfirmed_stop_is_reported_distinctly(host, quick):
    host.fake.hang_ops = {"check"}
    host.fake.immortal = True
    with pytest.raises(RuntimeError, match="could not be confirmed stopped"):
        snippet_call(host)


def test_snippet_that_outlives_a_killed_client_is_stopped_before_the_error_propagates(
    host, monkeypatch
):
    host.fake.snippet_outlives_client = True

    def failing_append(*args, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(wr, "append_journal", failing_append)
    with pytest.raises(OSError, match="disk full"):
        snippet_call(host, "step_slug")
    assert host.fake.procs == []
    assert len(host.fake.kill_calls) == 1


def test_apply_hung_slug_step_restarts_services_only_after_the_process_is_gone(host, quick):
    host.fake.hang_ops = {"step_slug"}
    with pytest.raises(RuntimeError, match=r"Rename failed before the slug change.*timed out"):
        host.deploy()
    calls = host.fake.calls
    kill = first_index(calls, is_kill)  # the sweep at the start of apply
    last_kill = len(calls) - 1 - first_index(calls[::-1], is_kill)  # the timeout kill
    assert kill is not None and last_kill > kill
    assert first_index(calls, is_start) > last_kill
    assert host.fake.procs == []
    assert host.receipt()["step"] == "pending"


def test_apply_unconfirmed_stop_leaves_services_down_and_receipt_alone(host, quick):
    host.fake.hang_ops = {"step_slug"}
    # The sweep at the start of apply finds nothing; the hung step is what cannot be stopped.
    original = host.fake.execute

    def stubborn(*args, input=None):
        if host.fake.procs:
            host.fake.immortal = True
        return original(*args, input=input)

    host.fake.execute = stubborn
    with pytest.raises(RuntimeError, match=r"could not be confirmed stopped") as caught:
        host.deploy()
    assert "not restarted" in str(caught.value) and "not reset" in str(caught.value)
    assert first_index(host.fake.calls, is_start) is None
    assert host.receipt()["step"] == "backed_up"
    assert not host.running()["plane"]


def test_revert_unconfirmed_stop_keeps_services_down(host, quick):
    host.deploy()
    host.fake.hang_ops = {"restore"}
    original = host.fake.execute

    def stubborn(*args, input=None):
        if host.fake.procs:
            host.fake.immortal = True
        return original(*args, input=input)

    host.fake.execute = stubborn
    with pytest.raises(RuntimeError, match=r"could not be confirmed stopped") as caught:
        host.deploy("revert")
    assert "not restarted" in str(caught.value)
    assert not host.running()["plane"]


def test_apply_and_revert_sweep_orphans_but_check_does_not(host):
    host.fake.procs = ["left-by-a-killed-run"]
    host.deploy("check")
    assert host.fake.kill_calls == [] and host.fake.procs == ["left-by-a-killed-run"]
    host.deploy()
    assert host.fake.kill_calls[0] == "" and host.fake.procs == []
    host.fake.procs = ["another"]
    host.deploy("revert")
    assert host.fake.procs == []


def test_orphan_that_cannot_be_stopped_refuses_before_any_stop(host):
    host.fake.procs = ["orphan"]
    host.fake.immortal = True
    with pytest.raises(RuntimeError, match=r"Refused: .*earlier snippet"):
        host.deploy()
    assert not mutating(host.fake)


def run_killer(root, token, monkeypatch, wait="0"):
    monkeypatch.setattr(sys, "argv", ["-c", token, wait, str(root)])
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    with pytest.raises(SystemExit) as done:
        exec(compile(wr.KILLER, "<killer>", "exec"), {"__name__": "__main__"})  # noqa: S102
    return done.value.code


def fake_proc(root, pid, environ):
    (root / str(pid)).mkdir()
    (root / str(pid) / "environ").write_bytes(b"\0".join(environ) + b"\0")


def test_kill_script_stops_only_the_marked_processes(tmp_path, monkeypatch, capsys):
    fake_proc(tmp_path, 4001, [b"PATH=/bin", b"PLANE_RENAME_RUN=abc"])
    fake_proc(tmp_path, 4002, [b"PLANE_RENAME_RUN=other"])
    fake_proc(tmp_path, 4003, [b"PATH=/bin"])
    sent = []

    def kill(pid, sig):
        sent.append((pid, sig))
        shutil.rmtree(tmp_path / str(pid))

    monkeypatch.setattr(os, "kill", kill)
    assert run_killer(tmp_path, "abc", monkeypatch) == 0
    assert sent == [(4001, signal.SIGTERM)]
    assert capsys.readouterr().out.strip() == "gone"
    assert (tmp_path / "4002").exists() and (tmp_path / "4003").exists()


def test_kill_script_without_a_token_stops_every_marked_process(tmp_path, monkeypatch):
    fake_proc(tmp_path, 4001, [b"PLANE_RENAME_RUN=a"])
    fake_proc(tmp_path, 4002, [b"PLANE_RENAME_RUN=b"])
    fake_proc(tmp_path, 4003, [b"PLANE_RENAME_RUNNER=b"])
    monkeypatch.setattr(os, "kill", lambda pid, sig: shutil.rmtree(tmp_path / str(pid)))
    assert run_killer(tmp_path, "", monkeypatch) == 0
    assert [p.name for p in tmp_path.iterdir()] == ["4003"]


def test_kill_script_escalates_to_sigkill_and_reports_a_survivor(tmp_path, monkeypatch, capsys):
    fake_proc(tmp_path, 4001, [b"PLANE_RENAME_RUN=abc"])
    sent = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append(sig))  # never dies
    assert run_killer(tmp_path, "abc", monkeypatch, wait="2") == 1
    assert sent[0] == signal.SIGTERM and set(sent[1:]) == {signal.SIGKILL}
    assert capsys.readouterr().out.strip() == "alive"


def test_kill_script_ignores_an_unreadable_environ(tmp_path, monkeypatch):
    (tmp_path / "4001").mkdir()  # a process that exited: no environ file
    assert run_killer(tmp_path, "abc", monkeypatch) == 0
