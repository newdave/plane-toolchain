"""Rename hardening: snippet deadlines and the kill script."""

import os
import shutil
import signal
import time
import pytest
from support_workspace_rename_deploy import mutating
from plane_toolchain import workspace_rename as wr
from support_workspace_rename_hardening import (
    fake_proc,
    first_index,
    is_kill,
    is_start,
    run_killer,
    snippet_call,
)
from support_workspace_rename_hardening import host  # noqa: F401
from support_workspace_rename_hardening import quick  # noqa: F401


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
    mine = wr.run_id(host.directory) + ":"
    host.fake.procs = [mine + "left-by-a-killed-run"]
    host.deploy("check")
    assert host.fake.kill_calls == [] and host.fake.procs == [mine + "left-by-a-killed-run"]
    host.deploy()
    assert host.fake.kill_calls[0] == mine and host.fake.procs == []
    host.fake.procs = [mine + "another"]
    host.deploy("revert")
    assert host.fake.procs == []


def test_orphan_that_cannot_be_stopped_refuses_before_any_stop(host):
    host.fake.procs = [wr.run_id(host.directory) + ":orphan"]
    host.fake.immortal = True
    with pytest.raises(RuntimeError, match=r"Refused: .*earlier snippet"):
        host.deploy()
    assert not mutating(host.fake)


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


def test_kill_script_sweep_stops_only_its_directory_and_refuses_foreign_runs(
    tmp_path, monkeypatch, capsys
):
    fake_proc(tmp_path, 4001, [b"PLANE_RENAME_RUN=h1:a"])
    fake_proc(tmp_path, 4002, [b"PLANE_RENAME_RUN=h1:b"])
    fake_proc(tmp_path, 4003, [b"PLANE_RENAME_RUNNER=b"])
    monkeypatch.setattr(os, "kill", lambda pid, sig: shutil.rmtree(tmp_path / str(pid)))
    assert run_killer(tmp_path, "h1:", monkeypatch) == 0
    assert [p.name for p in tmp_path.iterdir()] == ["4003"]
    fake_proc(tmp_path, 4004, [b"PLANE_RENAME_RUN=h1:c"])
    fake_proc(tmp_path, 4005, [b"PLANE_RENAME_RUN=h2:live"])
    capsys.readouterr()
    assert run_killer(tmp_path, "h1:", monkeypatch) == 2  # a foreign run: kill nothing
    assert capsys.readouterr().out.strip() == "foreign"
    assert (tmp_path / "4004").exists() and (tmp_path / "4005").exists()


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
