"""Rename hardening: probe, drain gate, idle override and redaction."""

import json
import time
from types import SimpleNamespace
import pytest
from support_workspace_rename_deploy import mutating
from plane_toolchain import workspace_rename as wr
from support_workspace_rename_hardening import drained_failure, mismatches, probe, snippet_call
from support_workspace_rename_hardening import host  # noqa: F401


def test_probe_passes_when_every_assumption_holds():
    from support_workspace_rename_backend import make_orm

    ok, events = probe(make_orm())
    assert ok and events == [{"event": "summary", "ok": True, "step": "probe"}]


def test_probe_passes_without_a_comment_binary_column():
    from support_workspace_rename_backend import make_orm

    orm = make_orm()
    orm.models["comment"].hidden.add("comment_binary")  # IssueComment in Plane CE v1.4.3
    ok, events = probe(orm)
    assert ok and events == [{"event": "summary", "ok": True, "step": "probe"}]


def test_probe_names_a_missing_field():
    from support_workspace_rename_backend import make_orm

    orm = make_orm()
    orm.models["comment"].hidden.add("comment_json")
    ok, events = probe(orm)
    assert not ok
    assert list(mismatches(events)) == [("comment", "comment_json", "missing")]
    assert events[-1] == {"event": "summary", "ok": False, "step": "probe"}


def test_probe_reports_every_mismatch_at_once():
    from support_workspace_rename_backend import make_orm

    orm = make_orm()
    orm.models["page"].hidden.add("workspace_id")
    orm.models["description"].hidden.add("description_stripped")
    orm.models["workspace"].hidden.add("deleted_at")
    ok, events = probe(orm)
    assert not ok
    assert set(mismatches(events)) == {
        ("page", "workspace_id", "missing"),
        ("description", "description_stripped", "missing"),
        ("workspace", "deleted_at", "missing"),
    }


def test_probe_checks_json_columns_are_jsonb_and_binary_columns_are_binary():
    from support_workspace_rename_backend import make_orm

    orm = make_orm()
    orm.models["notification"].types["data"] = ("json", "json")  # json, not jsonb
    orm.models["issue"].types["description_binary"] = ("text", "text")
    ok, events = probe(orm)
    found = mismatches(events)
    assert not ok
    assert found[("notification", "data", "db_type")]["expected"] == "jsonb"
    assert found[("notification", "data", "db_type")]["found"] == "json"
    assert found[("issue", "description_binary", "kind")]["expected"] == "binary"
    assert found[("issue", "description_binary", "kind")]["found"] == "text"


def test_probe_checks_table_names_and_the_unfiltered_manager():
    from support_workspace_rename_backend import make_orm

    orm = make_orm()
    orm.models["email_log"].db_table = "email_logs_renamed"
    orm.models["comment"].no_manager = True
    ok, events = probe(orm)
    found = mismatches(events)
    assert not ok
    assert found[("email_log", "", "table")]["found"] == "email_logs_renamed"
    assert ("comment", "", "manager") in found


def test_probe_emits_names_only_never_row_content():
    from support_workspace_rename_backend import PRIVATE, make_orm

    orm = make_orm()
    orm.models["issue"].hidden.add("description_json")
    _, events = probe(orm)
    assert PRIVATE not in json.dumps(events)


def test_check_refuses_on_a_mismatch_before_planning_anything():
    from support_workspace_rename_backend import make_orm, run_check

    orm = make_orm()
    orm.models["page"].hidden.add("workspace_id")
    ok, events = run_check(orm)
    assert not ok
    assert not any(e["event"] == "plan" for e in events)
    assert ("page", "workspace_id", "missing") in mismatches(events)
    assert events[-1] == {"event": "summary", "ok": False}


def test_check_and_apply_refuse_on_a_mismatch_and_say_which(host, capsys):
    host.orm.models["page"].hidden.add("workspace_id")
    with pytest.raises(RuntimeError, match="Refused"):
        host.deploy("check")
    assert "Model mismatch: page.workspace_id (missing)" in capsys.readouterr().out
    with pytest.raises(RuntimeError, match="Refused"):
        host.deploy()
    assert not mutating(host.fake)
    assert not (host.directory / "receipt.json").exists()


def test_mismatch_report_shows_expected_and_found(host, capsys):
    host.orm.models["notification"].types["data"] = ("json", "json")
    with pytest.raises(RuntimeError, match="Refused"):
        host.deploy("check")
    out = capsys.readouterr().out
    assert "Model mismatch: notification.data (db_type, expected jsonb, found json)" in out


def test_probe_through_the_assembled_snippet_uses_the_django_meta_api(host):
    summary, events = wr.run_snippet(
        "plane-api",
        wr.snippet_source(),
        "probe",
        {},
        wr.secure_receipt_dir(host.directory),
        host.fake.popen,
    )
    assert summary["ok"] is True
    host.orm.models["issue"].hidden.add("description_binary")
    summary, events = wr.run_snippet(
        "plane-api",
        wr.snippet_source(),
        "probe",
        {},
        host.directory,
        host.fake.popen,
    )
    assert summary["ok"] is False
    assert [(e["model"], e["field"]) for e in events if e["event"] == "mismatch"] == [
        ("issue", "description_binary")
    ]


def test_undrained_queue_refuses_and_names_the_override(host):
    host.fake.queue_depths = [4]
    with pytest.raises(RuntimeError, match=r"4 jobs still queued") as caught:
        wr.stop_services(
            wr.service_names({}),
            host.fake.docker,
            host.clock,
            host.clock.sleep,
            host.fake.checkpoint,
        )
    assert "PLANE_RENAME_ALLOW_UNDRAINED" in str(caught.value)
    assert host.running()["plane-beat-worker"] is False  # beat stopped, so the queue stops growing
    assert host.running()["plane-worker"] and host.running()["plane"]  # nothing else stopped
    assert not any("purge" in " ".join(call) for call in host.fake.calls)


def test_undrained_queue_proceeds_with_the_explicit_override(host, capsys):
    host.fake.queue_depths = [4]
    remaining = wr.stop_services(
        wr.service_names({}),
        host.fake.docker,
        host.clock,
        host.clock.sleep,
        host.fake.checkpoint,
        allow_undrained=True,
    )
    assert remaining == 4
    assert "4 jobs still queued; not purged" in capsys.readouterr().out
    assert not host.running()["plane-worker"]


def test_apply_with_an_undrained_queue_fails_before_the_slug_change_and_restarts(host):
    host.fake.queue_depths = [4]
    with pytest.raises(RuntimeError, match=r"Rename failed before the slug change.*4 jobs still"):
        host.deploy()
    assert "step_slug" not in host.ops()
    assert all(host.running().values())
    assert host.receipt()["step"] == "pending"


def test_apply_with_the_override_continues_past_an_undrained_queue(host):
    host.fake.queue_depths = [4]
    assert host.deploy(allow_undrained=True).startswith("Rename complete")


def test_revert_with_an_undrained_queue_refuses_unless_overridden(host):
    host.deploy()
    host.fake.queue_depths = [3]
    with pytest.raises(RuntimeError, match=r"Revert stopped at restored.*3 jobs still"):
        host.deploy("revert")
    assert host.deploy("revert", allow_undrained=True).startswith("Revert complete")


@pytest.mark.parametrize("operation", ["check", "apply", "revert"])
@pytest.mark.parametrize(
    "api",
    ["plane-worker", "plane", "plane-space", "plane-admin", "plane-beat-worker", "plane-live"],
)
def test_api_container_that_is_a_service_container_is_refused_before_anything(host, operation, api):
    if operation == "revert":
        host.deploy()
        host.fake.calls.clear()
        host.shell.requests.clear()
    with pytest.raises(RuntimeError, match=r"Refused: the API container is also .* service"):
        host.deploy(operation, api=api)
    assert not mutating(host.fake)
    assert not host.shell.requests


def test_api_container_in_the_stop_list_via_an_override_is_refused(host):
    host.fake.add("custom-worker")
    with pytest.raises(RuntimeError, match=r"Refused: the API container is also"):
        host.deploy(api="custom-worker", services={"PLANE_WORKER_CONTAINER": "custom-worker"})
    assert not mutating(host.fake)


def test_completed_rename_runs_health_and_says_how_to_archive(host):
    host.deploy()
    host.fake.calls.clear()
    host.shell.requests.clear()
    message = host.deploy()
    assert message.startswith("Rename already complete")
    assert "archive" in message and "receipt directory" in message
    assert host.ops() == ["health"]
    assert not mutating(host.fake)


def test_completed_rename_with_a_failing_health_check_is_refused(host, capsys):
    host.deploy()
    host.shell.api["status"] = 500
    with pytest.raises(RuntimeError, match=r"Refused: health failed"):
        host.deploy()
    assert "Health api: FAILED" in capsys.readouterr().out


def test_check_on_a_completed_rename_also_runs_health(host):
    host.deploy()
    host.shell.requests.clear()
    assert host.deploy("check").startswith("Rename already complete")
    assert host.ops() == ["health"]


def test_opposite_rename_after_a_completed_one_says_how_to_archive(host):
    host.deploy()
    host.fake.calls.clear()
    with pytest.raises(RuntimeError, match=r"different rename.*archive") as caught:
        host.deploy(**{"from": "new-slug", "to": "old-slug"})
    assert "receipt directory" in str(caught.value)
    assert not mutating(host.fake)


def test_cli_override_flag_and_api_container_check(monkeypatch):
    from plane_toolchain import remote

    sent = []
    env = {
        "PLANE_RENAME_FROM": "old-slug",
        "PLANE_RENAME_TO": "new-slug",
        "PLANE_MCP_SSH": "owner@203.0.113.7",
        "PLANE_API_CONTAINER": "plane-api",
        "PLANE_LIVE_URL": "https://plane.example.com",
    }
    for name in (*env, "PLANE_RENAME_ALLOW_UNDRAINED", *wr.SERVICE_VARIABLES):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        remote.subprocess,
        "run",
        lambda argv, **kw: sent.append(kw["input"]) or SimpleNamespace(returncode=0),
    )

    def run(operation="apply"):
        with pytest.raises(SystemExit) as stop:
            wr.main([operation])
        return stop.value.code

    assert run() == 0
    assert "allow_undrained" not in json.loads(sent[-1])
    monkeypatch.setenv("PLANE_RENAME_ALLOW_UNDRAINED", "1")
    assert run() == 0
    assert json.loads(sent[-1])["allow_undrained"] is True
    monkeypatch.setenv("PLANE_RENAME_ALLOW_UNDRAINED", "yes")
    assert run() == "Invalid PLANE_RENAME_ALLOW_UNDRAINED (use 1)"
    monkeypatch.delenv("PLANE_RENAME_ALLOW_UNDRAINED")
    count = len(sent)
    monkeypatch.setenv("PLANE_API_CONTAINER", "plane-worker")
    assert "API container" in run()
    monkeypatch.setenv("PLANE_API_CONTAINER", "plane-api")
    monkeypatch.setenv("PLANE_WEB_CONTAINER", "plane-api")
    assert "API container" in run()
    assert len(sent) == count  # refused before any SSH


def test_revert_with_no_receipt_refuses_before_any_docker_call(host):
    with pytest.raises(RuntimeError, match="no receipt for this rename; nothing to revert"):
        host.deploy("revert")
    assert host.fake.calls == [] and not host.directory.exists()
    wr.secure_receipt_dir(host.directory)  # a directory without a receipt: same, still no call
    with pytest.raises(RuntimeError, match="nothing to revert"):
        host.deploy("revert")
    assert host.fake.calls == []


def test_apply_cannot_start_while_a_revert_is_in_its_preflight(host, monkeypatch):
    host.deploy()
    seen = []
    real = wr.sweep_orphans

    def sweep(*args):
        with pytest.raises(RuntimeError, match="holds the lock") as caught:
            host.deploy()  # an apply starting in the window between lock and preflight
        seen.append(str(caught.value))
        return real(*args)

    monkeypatch.setattr(wr, "sweep_orphans", sweep)
    host.fake.calls.clear()
    assert host.deploy("revert").startswith("Revert complete")
    assert seen


@pytest.mark.parametrize("operation", ["revert", "apply"])
def test_stopped_workers_cannot_drain_so_the_gate_fails_fast_with_accurate_words(host, operation):
    drained_failure(host)
    host.shell.live.fail = False
    before = host.clock.now
    with pytest.raises(RuntimeError) as caught:
        host.deploy(operation)
    text = str(caught.value)
    assert host.clock.now == before  # no 60 s wait
    assert "3 jobs still queued and the workers are stopped" in text
    assert "kept (not purged)" in text and "PLANE_RENAME_ALLOW_UNDRAINED=1" in text
    assert "Let the workers finish" not in text and "stop them by hand" not in text
    assert "left as they are" in text
    assert not host.running()["plane-worker"]
    # the override carries on
    done = host.deploy(operation, allow_undrained=True)
    assert done.startswith("Rename complete" if operation == "apply" else "Revert complete")


def test_marker_carries_the_receipt_directory_id(host, tmp_path):
    snippet_call(host)
    argv = next(c for c in host.fake.calls if "manage.py" in c)
    assert argv[3].split("=", 1)[1].startswith(wr.run_id(host.directory) + ":")
    assert wr.run_id(host.directory) != wr.run_id(tmp_path / "elsewhere")
    assert len(wr.run_id(host.directory)) == 12


def test_sweep_refuses_and_kills_nothing_when_another_directory_has_a_live_run(host):
    host.fake.procs = ["0123456789ab:live-run-elsewhere"]
    with pytest.raises(
        RuntimeError, match="Refused: another rename run is active on this instance"
    ):
        host.deploy()
    assert host.fake.procs == ["0123456789ab:live-run-elsewhere"]
    assert not mutating(host.fake)


def test_sweep_kills_this_directory_only_when_no_foreign_run_exists(host):
    mine = wr.run_id(host.directory) + ":stale"
    host.fake.procs = [mine]
    assert host.deploy().startswith("Rename complete")
    assert host.fake.procs == []


def test_idle_deadline_override_in_the_payload_applies_and_is_restored(host):
    host.fake.hang_ops = {"step_slug"}
    with pytest.raises(RuntimeError, match=r"timed out after 0\.3 s"):
        host.deploy(snippet_idle_seconds=0.3)
    assert wr.SNIPPET_IDLE_SECONDS == 900


def test_invalid_idle_override_in_the_payload_is_ignored(host):
    host.deploy("check", snippet_idle_seconds=-5)
    host.deploy("check", snippet_idle_seconds=True)
    assert wr.SNIPPET_IDLE_SECONDS == 900


def test_cli_idle_override_is_validated_and_sent(monkeypatch):
    from plane_toolchain import remote

    sent = []
    env = {
        "PLANE_RENAME_FROM": "old-slug",
        "PLANE_RENAME_TO": "new-slug",
        "PLANE_MCP_SSH": "owner@203.0.113.7",
        "PLANE_API_CONTAINER": "plane-api",
        "PLANE_LIVE_URL": "https://plane.example.com",
    }
    for name in (*env, "PLANE_RENAME_SNIPPET_IDLE_SECONDS", "PLANE_RENAME_ALLOW_UNDRAINED"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        remote.subprocess,
        "run",
        lambda argv, **kw: sent.append(kw["input"]) or SimpleNamespace(returncode=0),
    )

    def run():
        with pytest.raises(SystemExit) as stop:
            wr.main(["check"])
        return stop.value.code

    monkeypatch.setenv("PLANE_RENAME_SNIPPET_IDLE_SECONDS", "3600")
    assert run() == 0 and json.loads(sent[-1])["snippet_idle_seconds"] == 3600
    for bad in ("0", "-1", "abc", "99999"):
        monkeypatch.setenv("PLANE_RENAME_SNIPPET_IDLE_SECONDS", bad)
        assert "Invalid PLANE_RENAME_SNIPPET_IDLE_SECONDS" in run()
    assert len(sent) == 1


def test_check_scans_emit_progress_that_the_host_does_not_keep(host, monkeypatch):
    from plane_toolchain import workspace_rename_backend as backend
    from support_workspace_rename_backend import make_orm, run_check

    monkeypatch.setattr(backend, "PROGRESS_EVERY", 1)
    ok, events = run_check(make_orm())
    assert ok and sum(e["event"] == "progress" for e in events) > 10
    source = wr.snippet_source().replace("PROGRESS_EVERY = 500", "PROGRESS_EVERY = 1")
    summary, kept = wr.run_snippet(
        "plane-api",
        source,
        "check",
        {"old": "old-slug", "new": "new-slug"},
        wr.secure_receipt_dir(host.directory),
        host.fake.popen,
    )
    assert summary["ok"] is True
    assert not any(e["event"] == "progress" for e in kept)


def test_container_names_are_hidden_in_backup_failure_text(host):
    host.fake.pg_dump_code = 1
    host.fake.pg_dump_stderr = b"could not translate host name plane-db for plane-api\n"
    with pytest.raises(RuntimeError, match="pg_dump exited with an error") as caught:
        host.deploy()
    text = str(caught.value)
    assert "<container>" in text and "plane-db" not in text and "plane-api" not in text


def test_killer_wait_is_bounded_and_unknown_is_not_confirmed(monkeypatch):
    import threading

    from fake_docker import Hung

    monkeypatch.setattr(wr, "KILL_WAIT", 0.2)
    dead = threading.Event()

    class Stuck:
        stdout = Hung(dead)

        def kill(self):
            dead.set()

        def wait(self):
            return -9

    started = time.monotonic()
    assert wr.stop_snippet("plane-api", "h:t", lambda argv, **kw: Stuck()) is False
    assert time.monotonic() - started < 5
    assert dead.is_set()


def test_interrupt_with_an_unconfirmed_stop_warns_on_stderr(host, monkeypatch, capsys):
    host.fake.snippet_outlives_client = True
    host.fake.immortal = True

    def interrupted(*args, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(wr, "append_journal", interrupted)
    with pytest.raises(KeyboardInterrupt):
        snippet_call(host, "step_slug")
    assert "snippet may still be running" in capsys.readouterr().err


@pytest.mark.parametrize("operation", ["revert", "apply"])
def test_sixty_second_timeout_wording_when_the_worker_is_running(host, operation):
    drained_failure(host)
    host.shell.live.fail = False
    for name in ("plane-worker", "plane", "plane-space", "plane-admin"):
        host.fake.docker("start", name)  # the operator restarted them
    with pytest.raises(RuntimeError) as caught:
        host.deploy(operation)
    text = str(caught.value)
    assert "3 jobs still queued after 60 s" in text
    assert "PLANE_RENAME_ALLOW_UNDRAINED=1" in text
    assert "Beat is stopped and the other services were left as they are" in text
    assert "workers stopped" not in text and "stop them by hand" not in text


def test_hidden_container_names_match_whole_names_only(host):
    host.fake.pg_dump_code = 1
    host.fake.pg_dump_stderr = b"role plane_db missing on plane\n"
    with pytest.raises(RuntimeError) as caught:
        host.deploy()
    text = str(caught.value)
    assert "plane_db" in text and "<container>" in text
    assert "on plane" not in text
