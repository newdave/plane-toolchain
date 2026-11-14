"""Workspace rename failure policies, each tested alone through the step tables (PTC-113)."""

import pytest
from plane_toolchain import workspace_rename as wr


@pytest.fixture
def calls(monkeypatch):
    """Record the effects a policy uses; every one is replaceable per test."""
    log = []
    monkeypatch.setattr(wr, "run_start", lambda run: log.append("start"))
    monkeypatch.setattr(wr, "reset_receipt", lambda *a: log.append("reset"))
    monkeypatch.setattr(wr, "advance", lambda directory, step, uid: log.append("advance " + step))
    monkeypatch.setattr(wr, "load_journal", lambda directory, uid: [])
    monkeypatch.setattr(wr, "journal_workspace", lambda records: "w1")
    monkeypatch.setattr(wr, "say", lambda text, *a: log.append("say " + text))
    return log


def make_run(**extra):
    run = {"directory": "d", "uid": 0, "clock": None, "crash": lambda label: None}
    run.update(extra)
    return run


def refusal(policy, *args):
    with pytest.raises(RuntimeError) as caught:
        policy(*args)
    return str(caught.value)


def test_step_tables_cover_every_step_after_the_first():
    assert tuple(wr.apply_steps()) == wr.step_order("apply")[1:]
    assert tuple(wr.revert_steps()) == wr.step_order("revert")


def test_apply_steps_name_one_policy_per_side_of_the_slug_change():
    policies = {step: row[1] for step, row in wr.apply_steps().items()}
    assert policies["stopped"] is wr.pre_slug_failure
    assert policies["backed_up"] is wr.pre_slug_failure
    assert policies["slug_done"] is wr.slug_unclear
    for step in ("rows_done", "cache_done", "started", "complete"):
        assert policies[step] is wr.post_slug_failure


def test_pre_slug_failure_restarts_services_and_resets_the_receipt(calls):
    text = refusal(wr.pre_slug_failure, make_run(), "pending", "stopped", "boom")
    assert calls == ["start", "reset"]
    assert "before the slug change (stopped): boom. Services restarted; no data changed" in text
    assert "Warning" not in text


def test_pre_slug_failure_reports_a_failed_restart_and_a_failed_reset(calls, monkeypatch):
    def fail(*args):
        raise RuntimeError("no way")

    monkeypatch.setattr(wr, "run_start", fail)
    monkeypatch.setattr(wr, "reset_receipt", fail)
    text = refusal(wr.pre_slug_failure, make_run(), "pending", "backed_up", "boom")
    assert "Services not all restarted" in text
    assert "Warning: no way; start the remaining services by hand." in text
    assert "Warning: receipt reset failed: no way." in text


def test_post_slug_failure_never_restarts_or_resets(calls):
    text = refusal(wr.post_slug_failure, make_run(), "rows_done", "cache_done", "boom")
    assert calls == []
    assert "stopped at rows_done (moving to cache_done): boom. Services stay stopped." in text
    assert "nothing is rolled back automatically" in text


@pytest.mark.parametrize(
    ("step", "target", "reason", "expected"),
    [
        ("started", "complete", "boom", "health check failed: boom. Receipt left at started"),
        ("cache_done", "started", "boom", "Some services did not start"),
        ("slug_done", "slug_done", "boom", "Some services may still be running; stop them by hand"),
        (
            "slug_done",
            "rows_done",
            "x and the workers are stopped",
            "Services were left as they are (workers stopped, jobs kept)",
        ),
    ],
)
def test_post_slug_failure_states(calls, step, target, reason, expected):
    assert expected in refusal(wr.post_slug_failure, make_run(), step, target, reason)


def test_slug_unclear_resets_when_the_slug_never_committed(calls, monkeypatch):
    monkeypatch.setattr(wr, "run_slug_state", lambda run, workspace_id: "original")
    text = refusal(wr.slug_unclear, make_run(), "backed_up", "slug_done", "boom")
    assert calls == ["start", "reset"]
    assert "before the slug change (slug_done)" in text


def test_slug_unclear_continues_when_new_is_held_by_the_journalled_workspace(calls, monkeypatch):
    monkeypatch.setattr(wr, "run_slug_state", lambda run, workspace_id: "renamed")
    run = make_run(workspace_id=None)
    assert wr.slug_unclear(run, "backed_up", "slug_done", "boom") is None
    assert run["workspace_id"] == "w1"
    assert calls[-1] == "advance slug_done"
    assert "start" not in calls and "reset" not in calls


def test_slug_unclear_stops_with_services_down_when_the_state_is_unknown(calls, monkeypatch):
    monkeypatch.setattr(wr, "run_slug_state", lambda run, workspace_id: "unknown")
    text = refusal(wr.slug_unclear, make_run(), "backed_up", "slug_done", "boom")
    assert calls == []
    assert "slug step outcome unknown: boom. Services stay stopped" in text


def test_apply_recover_routes_by_step_and_stuck_snippets_win(calls):
    stuck = RuntimeError(wr.STUCK)
    text = refusal(wr.apply_recover, make_run(), "stopped", "backed_up", stuck)
    assert "Rename stopped at stopped: " in text and "Services were not restarted" in text
    assert calls == []
    assert "before the slug change (backed_up)" in refusal(
        wr.apply_recover, make_run(), "stopped", "backed_up", RuntimeError("boom")
    )
    assert "Some services did not start" in refusal(
        wr.apply_recover, make_run(), "cache_done", "started", RuntimeError("boom")
    )


def test_resume_failed_after_the_slug_change_is_a_post_slug_failure(calls):
    text = refusal(wr.resume_failed, make_run(workspace_id="w1"), "rows_done", "boom")
    assert "Rename stopped at rows_done: boom. Some services may still be running" in text
    assert calls == []


def test_resume_failed_resets_only_when_no_workspace_may_have_committed(calls):
    text = refusal(wr.resume_failed, make_run(workspace_id=None), "stopped", "boom")
    assert calls == ["start", "reset"]
    assert "before the slug change (stopped)" in text


@pytest.mark.parametrize(
    ("state", "expected", "effects"),
    [
        ("renamed", "Rename stopped at backed_up: boom", []),
        ("unknown", "boom; slug state unknown. Services were left as they are", []),
        ("original", "before the slug change (backed_up)", ["start", "reset"]),
    ],
)
def test_resume_failed_probes_a_possible_slug_commit(calls, monkeypatch, state, expected, effects):
    monkeypatch.setattr(wr, "run_slug_state", lambda run, workspace_id: state)
    text = refusal(wr.resume_failed, make_run(workspace_id="w1"), "backed_up", "boom")
    assert expected in text
    assert calls == effects


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("reverting", "Nothing was changed by the revert; services were left as they are"),
        ("restored", "The restore is all-or-nothing and safe to repeat"),
        ("started", "The data is restored, but some services did not start"),
        ("reverted", "Services are running; the receipt is left at started"),
    ],
)
def test_revert_failure_states_per_step(target, expected):
    text = refusal(wr.revert_failure, target, "boom")
    assert f"Revert stopped at {target}: boom. " in text
    assert expected in text
    assert "restoring backup.dump by hand is the last resort" in text


def test_revert_failure_drain_and_stuck_override_the_step_state():
    drained = refusal(wr.revert_failure, "restored", "x and the workers are stopped")
    assert "Services were left as they are (workers stopped, jobs kept)" in drained
    stuck = refusal(wr.revert_failure, "restored", wr.STUCK)
    assert "Services were not restarted and the receipt was not changed" in stuck


def test_revert_recover_reraises_the_partial_report_untouched():
    first = RuntimeError("Revert incomplete: ...")
    with pytest.raises(RuntimeError) as caught:
        wr.revert_recover({"finished": True}, "restored", "restored", first)
    assert caught.value is first
    assert "Revert stopped at restored: boom" in refusal(
        wr.revert_recover, {"finished": False}, "reverting", "restored", RuntimeError("boom")
    )


def test_revert_settle_is_a_no_op_without_a_partial_restore():
    wr.revert_settle({"partial": None})


def test_revert_settle_restarts_rechecks_and_reports_a_partial_restore(calls, monkeypatch):
    def fail(run):
        raise RuntimeError("unhealthy")

    monkeypatch.setattr(wr, "revert_verify", fail)
    run = {"partial": 3, "notes": [], "finished": False}
    text = refusal(wr.revert_settle, run)
    assert calls == ["start"]
    assert run["finished"] is True
    assert "the restore was partial (3 rows skipped" in text
    assert "Warning: health check failed: unhealthy." in text
