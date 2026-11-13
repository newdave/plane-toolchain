"""Workspace rename host program: apply flow and failure restarts."""

import hashlib
import json
import pytest
from fake_docker import Killed
from plane_toolchain import workspace_rename as wr
from support_workspace_rename_deploy import (
    NEW,
    OLD,
    PRIVATE,
    REFUSALS,
    make_refusal,
    mutating,
    run_remote,
    scripted,
    snapshot,
)
from support_workspace_rename_deploy import host  # noqa: F401


@pytest.mark.parametrize("case", REFUSALS)
def test_check_exits_nonzero_when_apply_would_refuse(host, case, monkeypatch, capsys):
    make_refusal(host, case)
    assert run_remote(wr.REMOTE, host.payload("check"), host, monkeypatch) == 1
    assert "Refused" in capsys.readouterr().err
    with pytest.raises(RuntimeError):
        host.deploy("apply")
    assert not mutating(host.fake)


def test_check_dry_run_end_to_end_with_fake_docker_and_fake_orm_writes_nothing(
    host, monkeypatch, capsys
):
    before = snapshot(host.orm)
    assert run_remote(wr.REMOTE, host.payload("check"), host, monkeypatch) == 0
    out = capsys.readouterr().out
    assert "Check passed" in out and "planned" in out
    assert snapshot(host.orm) == before
    assert host.orm.cache == {"session:x": "kept until flush"}
    assert not [e for e in host.orm.log if e[0] in ("update", "begin")]
    assert not mutating(host.fake)
    assert not host.directory.exists()  # not even the receipt directory
    assert host.ops() == ["check", "flush_cache"]
    assert host.request("flush_cache") == [{"dry_run": True}]
    assert not host.shell.live.calls
    assert PRIVATE not in out


def test_apply_happy_path_call_order_and_final_receipt_complete(host, monkeypatch, capsys):
    assert run_remote(wr.REMOTE, host.payload(), host, monkeypatch) == 0
    out = capsys.readouterr()
    assert "Rename complete" in out.out
    receipt = host.receipt()
    assert receipt["step"] == "complete" and receipt["phase"] == "apply"
    dump = (host.directory / "backup.dump").read_bytes()
    assert receipt["backup_sha256"] == hashlib.sha256(dump).hexdigest()
    assert host.ops() == [
        "check",
        "flush_cache",  # dry run, in the preflight
        "step_slug",
        "step_rows",
        "flush_cache",
        "health",
    ]
    order = []
    for call in host.fake.calls:
        text = " ".join(call)
        if call[0] in ("stop", "start", "restart"):
            order.append(call[0] + " " + call[1])
        elif "manage.py" in call:
            order.append("shell")
        elif "pg_dump" in text:
            order.append("pg_dump")
    shells = [i for i, item in enumerate(order) if item == "shell"]
    assert order[: shells[1] + 1] == ["shell", "shell"]  # read-only preflight before any stop
    assert order[shells[1] + 1 : shells[2]] == [
        "stop plane-beat-worker",
        "stop plane-worker",
        "stop plane",
        "stop plane-space",
        "stop plane-admin",
        "restart plane-live",
        "pg_dump",
    ]
    assert order[shells[2] :] == [
        "shell",  # step_slug
        "shell",  # step_rows
        "shell",  # flush_cache
        "start plane-admin",
        "start plane-space",
        "start plane",
        "start plane-worker",
        "start plane-beat-worker",
        "shell",  # health
    ]
    assert all(host.running().values())
    workspaces = {r["id"]: r["slug"] for r in host.orm.models["workspace"].rows}
    assert workspaces == {"w1": NEW, "w2": "other"}
    assert OLD not in json.dumps(snapshot(host.orm)["issue"])
    assert host.orm.cache == {}
    assert {"kind": "workspace", "id": "w1", "slug": OLD} in host.journal()
    assert PRIVATE not in out.out + out.err  # journal records are never printed
    # workspace_id from step_slug reaches step_rows and health
    assert host.request("step_rows")[0]["workspace_id"] == "w1"
    assert host.request("health")[0]["workspace_id"] == "w1"


def test_apply_refuses_other_rename_receipt(host):
    wr.new_receipt(wr.secure_receipt_dir(host.directory), "a", "b")
    with pytest.raises(RuntimeError, match="different rename"):
        host.deploy()
    assert not mutating(host.fake) and not host.ops()
    assert host.receipt()["from"] == "a"


@pytest.mark.parametrize("case", [c for c in REFUSALS if c != "other_receipt"])
def test_apply_refuses_on_any_check_failure_without_stopping_services(host, case):
    make_refusal(host, case)
    before = snapshot(host.orm)
    with pytest.raises(RuntimeError, match="Refused"):
        host.deploy()
    assert not mutating(host.fake)
    assert not (host.directory / "receipt.json").exists()
    assert snapshot(host.orm) == before
    assert "step_slug" not in host.ops()


def test_health_failure_leaves_receipt_started_not_complete_and_exits_nonzero(
    host, monkeypatch, capsys
):
    host.shell.api["status"] = 503
    assert run_remote(wr.REMOTE, host.payload(), host, monkeypatch) == 1
    err = capsys.readouterr().err
    assert "health" in err.lower() and "revert" in err
    assert host.receipt()["step"] == "started"
    assert all(host.running().values())
    host.shell.api["status"] = 200
    assert "Rename complete" in host.deploy()  # a rerun rechecks health only
    assert host.ops()[-3:] == ["health", "flush_cache", "health"]
    assert host.request("flush_cache")[-1] == {"dry_run": True}
    assert host.receipt()["step"] == "complete"


def test_resume_does_not_rebackup_when_backup_sha_matches(host, capsys):
    host.fake.crash_at("after_backed_up")
    with pytest.raises(Killed):
        host.deploy()
    digest = host.receipt()["backup_sha256"]
    host.fake.pg_dump = b"a different dump"
    assert "Rename complete" in host.deploy()
    assert sum("pg_dump" in " ".join(c) for c in host.fake.calls) == 1
    assert host.receipt()["backup_sha256"] == digest
    assert "SHA-256 matches" in capsys.readouterr().out


def test_resume_refuses_backup_sha_mismatch(host):
    host.fake.crash_at("after_backed_up")
    with pytest.raises(Killed):
        host.deploy()
    (host.directory / "backup.dump").write_bytes(b"tampered")
    host.fake.calls.clear()
    with pytest.raises(RuntimeError, match="does not match the recorded SHA-256"):
        host.deploy()
    assert not mutating(host.fake)
    assert "step_slug" not in host.ops()
    assert host.receipt()["step"] == "stopped"


def test_journal_events_fsynced_before_next_line_and_never_printed(tmp_path, capsys):
    directory = wr.secure_receipt_dir(tmp_path / "r")
    record = {"kind": "row", "table": "issues", "id": "i1", "fields": {"x": PRIVATE}}
    lines = [
        "noise that is not JSON " + PRIVATE,
        json.dumps({"event": "journal", "record": {"kind": "workspace", "id": "w1", "slug": OLD}}),
        json.dumps({"event": "journal", "record": record}),
        json.dumps({"event": "summary", "ok": True, "step": "slug", "workspace_id": "w1"}),
    ]
    summary = wr.snippet_ok("api", "", "step_slug", {}, directory, scripted(lines, directory))
    assert summary["workspace_id"] == "w1"
    assert wr.load_journal(directory)[1] == record
    out = capsys.readouterr()
    assert PRIVATE not in out.out + out.err


@pytest.mark.parametrize(
    "lines",
    [
        [],
        [json.dumps({"event": "summary", "ok": False, "step": "rows"})],
        [json.dumps({"event": "error", "reason": "row_failed"})],
    ],
)
def test_missing_summary_or_ok_false_is_failure(tmp_path, lines):
    directory = wr.secure_receipt_dir(tmp_path / "r")
    with pytest.raises(RuntimeError):
        wr.snippet_ok("api", "", "step_rows", {}, directory, scripted(lines, directory))


def test_journal_event_outside_an_apply_step_is_refused(tmp_path):
    directory = tmp_path / "r"
    lines = [json.dumps({"event": "journal", "record": {"kind": "workspace"}})]
    with pytest.raises(RuntimeError, match="unexpected journal"):
        wr.run_snippet("api", "", "check", {}, directory, scripted(lines, directory))
    assert not directory.exists()


def test_request_is_one_json_argument_line_before_the_snippet(host):
    host.deploy("check")
    assert host.shell.requests[0] == {"op": "check", "args": {"old": OLD, "new": NEW}}
    piped = [c for c in host.fake.calls if "manage.py" in c][0]
    assert piped[:3] == ("exec", "-i", "-e") and piped[3].startswith("PLANE_RENAME_RUN=")
    assert piped[4:] == ("plane-api", "python", "manage.py", "shell")


@pytest.mark.parametrize("failure", ["celery_missing", "probe_fails", "stop_fails"])
def test_stop_failure_after_beat_restarts_services(host, failure):
    if failure == "celery_missing":
        host.fake.celery_listed = False
    elif failure == "probe_fails":
        host.fake.fail.append(("exec", "plane-mq"))
    else:
        host.fake.fail.append(("stop", "plane-space"))
    with pytest.raises(RuntimeError, match="Services restarted"):
        host.deploy()
    assert all(host.running().values())
    assert host.receipt()["step"] == "pending"
    assert "step_slug" not in host.ops()


def test_backup_failure_restarts_services_and_changes_no_data(host):
    before = snapshot(host.orm)
    host.fake.pg_dump_code = 1
    with pytest.raises(RuntimeError, match="Services restarted"):
        host.deploy()
    assert all(host.running().values())
    assert host.receipt()["step"] == "pending" and snapshot(host.orm) == before


def test_slug_step_failure_restarts_services(host):
    host.orm.models["workspace"].fail_update = True
    with pytest.raises(RuntimeError, match="Services restarted"):
        host.deploy()
    assert all(host.running().values())
    assert host.receipt()["step"] == "pending"
    assert host.orm.models["workspace"].rows[0]["slug"] == OLD


def test_dump_without_recorded_sha_is_adopted_on_resume(host, capsys):
    host.fake.crash_at("after_backup_file")
    with pytest.raises(Killed):
        host.deploy()
    assert "backup_sha256" not in host.receipt()
    assert "Rename complete" in host.deploy()
    dump = (host.directory / "backup.dump").read_bytes()
    assert host.receipt()["backup_sha256"] == hashlib.sha256(dump).hexdigest()
    assert sum("pg_dump" in " ".join(c) for c in host.fake.calls) == 1
    assert "no recorded SHA-256" in capsys.readouterr().out


def test_dump_without_recorded_sha_outside_the_crash_window_refused(host):
    wr.new_receipt(wr.secure_receipt_dir(host.directory), OLD, NEW)
    (host.directory / "backup.dump").write_bytes(b"stray")
    (host.directory / "backup.dump").chmod(0o600)
    with pytest.raises(RuntimeError, match="no recorded SHA-256"):
        host.deploy()
    assert not mutating(host.fake)


def test_rows_failure_after_slug_done_stops_with_services_down_then_resumes(host):
    host.shell.live.fail = True
    with pytest.raises(RuntimeError, match="Services stay stopped") as failure:
        host.deploy()
    assert "revert" in str(failure.value)
    assert host.receipt()["step"] == "slug_done"
    assert not host.running()["plane"] and not host.running()["plane-beat-worker"]
    host.shell.live.fail = False
    assert "Rename complete" in host.deploy()
    assert host.request("step_rows")[-1]["workspace_id"] == "w1"


def test_partial_restore_and_every_skip_reach_the_operator(host, capsys):
    host.deploy()
    host.orm.models["issue"].rows[0]["description_html"] = "<p>edited after the rename</p>"
    summary, events = wr.run_snippet(
        "plane-api",
        wr.snippet_source(),
        "restore",
        {"old": OLD, "new": NEW, "journal": host.journal()},
        host.directory,
        host.fake.popen,
    )
    wr.report("restore", summary, events)
    out = capsys.readouterr().out
    assert summary["partial"] is True
    assert "Skipped issues i1 (changed)" in out
    assert "Partial restore" in out and "not a full revert" in out
    assert PRIVATE not in out


def test_revert_receipt_phase_refused_by_apply(host):
    wr.new_receipt(wr.secure_receipt_dir(host.directory), OLD, NEW)
    wr.advance(host.directory, "reverting")
    with pytest.raises(RuntimeError, match="revert"):
        host.deploy()
    assert not mutating(host.fake)


def test_complete_receipt_is_a_no_op(host):
    host.deploy()
    host.fake.calls.clear()
    assert "already complete" in host.deploy()
    assert not mutating(host.fake)
