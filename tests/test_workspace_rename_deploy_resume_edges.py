"""Workspace rename host program: resume edges and entry reporting."""

import signal
import subprocess
import sys
import time
import pytest
from fake_docker import Killed
from plane_toolchain import workspace_rename as wr
from support_workspace_rename_deploy import (
    Host,
    NAMES,
    OLD,
    clear,
    first_op,
    revert,
    run_remote,
    sabotage,
    snapshot,
    stop,
)
from support_workspace_rename_deploy import fake  # noqa: F401
from support_workspace_rename_deploy import host  # noqa: F401


def test_kill_after_slug_commit_with_services_running_keeps_the_backup(host):
    host.fake.crash_at("after_slug_done")
    with pytest.raises(Killed):
        host.deploy()
    host.fake.containers["plane"]["running"] = True
    assert "Rename complete" in host.deploy()
    assert "rotated_backups" not in host.receipt()  # the dump is the pre-change state
    assert sum("pg_dump" in " ".join(c) for c in host.fake.calls) == 1


def test_restop_failure_after_slug_commit_is_a_post_slug_failure(host):
    host.fake.crash_at("after_slug_done")
    with pytest.raises(Killed):
        host.deploy()
    host.fake.fail.append(("stop", "plane-space"))
    with pytest.raises(RuntimeError, match="Rerun apply to resume") as failure:
        host.deploy()
    assert "no data changed" not in str(failure.value)
    assert "(backed_up)" not in str(failure.value)  # no "stopped at X (X)" duplicate
    receipt = host.receipt()
    assert receipt["step"] == "backed_up" and "rotated_backups" not in receipt
    assert (host.directory / "backup.dump").exists()
    assert not [c for c in host.fake.calls if c[0] == "start"]
    assert "Rename complete" in host.deploy()


def test_restop_failure_with_unknown_slug_state_leaves_services_alone(host):
    host.fake.crash_at("after_slug_done")
    with pytest.raises(Killed):
        host.deploy()
    real = host.shell
    calls = {"n": 0}

    def flaky(text):  # the preflight probe works; every later snippet call fails
        calls["n"] += 1
        return real(text) if calls["n"] <= 2 else ("", 1)

    host.fake.on_manage_shell = flaky
    host.fake.fail.append(("stop", "plane-space"))
    mark = len(host.fake.calls)
    with pytest.raises(RuntimeError, match="slug state unknown"):
        host.deploy()
    assert not [c for c in host.fake.calls[mark:] if c[0] == "start"]
    assert host.receipt()["step"] == "backed_up"
    assert (host.directory / "backup.dump").exists()


def test_reset_rotates_the_journal_so_revert_restores_the_latest_originals(host):
    host.shell.die_at = ("step_slug", 3)  # journals workspace, c1, d1, then rolls back
    with pytest.raises(RuntimeError, match="Services restarted"):
        host.deploy()
    rotated = host.receipt()["rotated_journals"]
    assert not (host.directory / "records.jsonl").exists()
    assert '"id":"c1"' in (host.directory / rotated[0]).read_text()  # kept, never deleted
    comment = host.orm.models["comment"].rows[0]
    comment["comment_html"] = f'<a href="/{OLD}/x">edited by a user</a>'  # services were live
    edited = snapshot(host.orm)
    assert "Rename complete" in host.deploy()
    revert(host)  # reads only the current records.jsonl
    assert snapshot(host.orm) == edited


@pytest.mark.parametrize("step", ["slug_done", "rows_done", "cache_done"])
def test_resume_after_slug_done_restops_services(host, step):
    host.fake.crash_at("after_" + wr.step_order("apply")[wr.step_order("apply").index(step) + 1])
    with pytest.raises(Killed):
        host.deploy()
    assert host.receipt()["step"] == step
    host.fake.containers["plane"]["running"] = True
    mark = len(host.fake.calls)
    host.deploy()
    calls = host.fake.calls[mark:]
    stop = calls.index(("stop", "plane"))
    shells = [i for i, c in enumerate(calls) if "manage.py" in c]
    assert stop < shells[-1]


def test_step_slug_without_summary_but_renamed_continues_with_services_stopped(host):
    sabotage(host, "step_slug", "drop_summary")
    assert "Rename complete" in host.deploy()
    calls = host.fake.calls
    rows = [i for i, c in enumerate(calls) if "manage.py" in c][4]  # check, dry, slug, probe
    assert not [c for c in calls[:rows] if c[0] == "start"]
    assert "health" in host.ops()[: host.ops().index("step_rows")]  # the read-only probe


def test_step_slug_workspace_mismatch_probes_and_continues_when_renamed(host):
    sabotage(host, "step_slug", "mismatch")
    assert "Rename complete" in host.deploy()
    assert host.request("step_rows")[0]["workspace_id"] == "w1"


def test_step_slug_unrun_with_old_slug_held_is_a_pre_slug_failure(host):
    sabotage(host, "step_slug", "unrun")
    with pytest.raises(RuntimeError, match="Services restarted"):
        host.deploy()
    assert host.receipt()["step"] == "pending" and all(host.running().values())


def test_step_slug_ambiguous_outcome_stops_with_services_stopped(host):
    sabotage(host, "step_slug", "dead")
    with pytest.raises(RuntimeError, match="outcome unknown") as failure:
        host.deploy()
    assert "no data changed" not in str(failure.value)
    assert "Services stay stopped" in str(failure.value)
    assert not host.running()["plane"]
    assert not [c for c in host.fake.calls if c[0] == "start"]
    assert host.receipt()["step"] == "backed_up"
    clear(host)
    assert "Rename complete" in host.deploy()


def test_start_services_tries_every_service_and_names_failures(fake):
    stop(fake)
    fake.fail.append(("start", "plane-space"))
    fake.fail.append(("start", "plane-worker"))
    with pytest.raises(RuntimeError, match="could not start: space, worker"):
        wr.start_services(NAMES, fake.docker)
    started = [c[1] for c in fake.calls if c[0] == "start"]
    assert started == ["plane-admin", "plane-space", "plane", "plane-worker", "plane-beat-worker"]
    assert fake.containers["plane-beat-worker"]["running"]


def test_entry_reports_oserror_with_guidance_and_no_traceback(host, monkeypatch, capsys):
    blocker = host.directory.parent / "not-a-directory"
    blocker.write_text("")
    payload = host.payload(receipt_dir=str(blocker / "receipts"))
    assert run_remote(wr.REMOTE, payload, host, monkeypatch) == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err and "rerun apply" in err
    assert "Rename interrupted (FileExistsError)" in err


def test_entry_reports_interrupt_with_guidance_and_no_traceback(host, monkeypatch, capsys):
    def interrupted(text):
        if first_op(text) == "step_rows":
            raise KeyboardInterrupt
        return host.shell(text)

    host.fake.on_manage_shell = interrupted
    assert run_remote(wr.REMOTE, host.payload(), host, monkeypatch) == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err and "interrupted" in err.lower() and "revert" in err
    assert host.receipt()["step"] == "slug_done"


def test_entry_turns_a_real_sigterm_into_guidance_without_traceback():
    # The program blocks reading its payload, before any docker call; then SIGTERM arrives.
    process = subprocess.Popen(  # noqa: S603 - this interpreter, the assembled program
        [sys.executable, "-I", "-c", wr.REMOTE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(1)
    process.send_signal(signal.SIGTERM)
    out, err = process.communicate(timeout=10)
    assert process.returncode == 1
    assert b"Rename interrupted (KeyboardInterrupt)" in err and b"Traceback" not in err


def test_resume_runs_full_check_unless_at_or_past_slug_done(host, tmp_path):
    host.orm.models["workspace"].fail_update = True  # journals the workspace, then rolls back
    with pytest.raises(RuntimeError):
        host.deploy()
    journal = host.directory / host.receipt()["rotated_journals"][0]  # set aside by the reset
    assert '"kind":"workspace"' in journal.read_text()
    clear(host)
    mark = len(host.shell.requests)
    host.deploy()
    assert [r["op"] for r in host.shell.requests[mark:]][0] == "check"
    other = Host(tmp_path / "other")
    other.fake.crash_at("after_rows_done")
    with pytest.raises(Killed):
        other.deploy()
    mark = len(other.shell.requests)
    other.deploy()
    assert "check" not in [r["op"] for r in other.shell.requests[mark:]]


def test_apply_preflight_runs_cache_dry_run_and_saltbox_before_any_stop(host, capsys):
    host.deploy()
    first_stop = next(i for i, c in enumerate(host.fake.calls) if c[0] == "stop")
    before = host.fake.calls[:first_stop]
    assert ("inspect", "plane-db") in before
    assert host.shell.requests[1] == {"op": "flush_cache", "args": {"dry_run": True}}
    assert sum("manage.py" in c for c in before) == 2
    assert "Saltbox-managed host" in capsys.readouterr().out
