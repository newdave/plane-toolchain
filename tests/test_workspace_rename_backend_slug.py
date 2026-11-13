"""Workspace rename backend snippet: slug step."""

import pytest
from support_workspace_rename_backend import (
    FakeLive,
    NEW_URL,
    is_journal,
    journal,
    log_index,
    make_orm,
    other_workspace_holds_new,
    row,
    run_op,
)


def test_step_slug_journals_old_slug_then_updates_in_one_transaction():
    orm = make_orm()
    ok, events = run_op("step_slug", orm)
    assert ok and events[-1]["event"] == "summary" and events[-1]["ok"]
    assert row(orm, "workspace", "w1")["slug"] == "new-slug"
    assert journal(events)[0] == {"kind": "workspace", "id": "w1", "slug": "old-slug"}
    kinds = [entry[0] for entry in orm.log]
    assert kinds.count("begin") == 1 and kinds.count("commit") == 1
    begin, commit = kinds.index("begin"), kinds.index("commit")
    writes = log_index(orm, lambda e: e[0] == "update")
    lines = log_index(orm, is_journal)
    assert writes and all(begin < i < commit for i in writes + lines)
    assert lines[0] < log_index(orm, lambda e: e[:2] == ("update", "workspaces"))[0]
    # comments, their Description mirrors and notifications are rewritten in the same step
    assert row(orm, "comment", "c1")["comment_html"] == '<a href="/new-slug/x">a</a>'
    assert row(orm, "comment", "c1")["comment_json"]["attrs"]["href"] == "/new-slug/x"
    assert row(orm, "comment", "c1")["comment_stripped"] == "a"
    assert row(orm, "description", "d1")["description_html"] == f'<a href="{NEW_URL}">x</a>'
    assert row(orm, "description", "d1")["description_json"]["href"] == NEW_URL
    assert row(orm, "description", "d1")["description_stripped"] == f"see {NEW_URL}"
    assert row(orm, "notification", "n1")["data"] == {"workspace_slug": "new-slug"}
    assert row(orm, "email_log", "e1")["data"] == {"issue": {"workspace_slug": "new-slug"}}
    assert row(orm, "email_log", "e2")["data"] == {"workspace_slug": "other"}
    # only changed columns are journalled and written
    comment = next(r for r in journal(events) if r.get("table") == "issue_comments")
    assert set(comment["fields"]) == {"comment_html", "comment_json"}
    # work items and Pages belong to step_rows
    assert row(orm, "issue", "i1")["description_html"].count("old-slug") == 1


def test_step_slug_never_calls_live():
    live = FakeLive()
    ok, _ = run_op("step_slug", make_orm(), live)
    assert ok and live.calls == []


def test_step_slug_transaction_rolls_back_comments_and_notifications_on_error():
    orm = make_orm()
    before = orm.snapshot()
    orm.models["notification"].fail_update = True
    ok, events = run_op("step_slug", orm)
    assert not ok
    assert orm.snapshot() == before  # slug, comments and descriptions rolled back too
    assert ("rollback",) in orm.log
    error = next(e for e in events if e["event"] == "error")
    assert error == {"event": "error", "reason": "write_failed", "detail": "RuntimeError"}
    assert events[-1] == {"event": "summary", "ok": False, "step": "slug"}


def test_step_slug_refuses_like_check_before_writing():
    orm = make_orm()
    orm.models["workspace"].rows.append({"id": "w3", "slug": "new-slug", "deleted_at": "x"})
    before = orm.snapshot()
    ok, events = run_op("step_slug", orm)
    assert not ok and orm.snapshot() == before and not journal(events)
    assert any(e["event"] == "refuse" for e in events)


def test_step_slug_resume_after_commit_is_a_noop():
    orm = make_orm()
    _, first = run_op("step_slug", orm)
    assert first[-1]["workspace_id"] == "w1"
    after = orm.snapshot()
    ok, events = run_op("step_slug", orm, workspace_id="w1")
    assert ok and orm.snapshot() == after and not journal(events)
    assert events[-1] == {
        "event": "summary",
        "ok": True,
        "step": "slug",
        "already": True,
        "workspace_id": "w1",
    }
    ok, events = run_op("step_slug", orm)  # resume without the journalled id
    assert not ok and orm.snapshot() == after
    assert events[0] == {"event": "refuse", "reason": "old_missing_or_deleted"}


@pytest.mark.parametrize("op", ["step_slug", "step_rows"])
@pytest.mark.parametrize("given", [None, "w1"])
def test_apply_steps_refuse_when_new_slug_belongs_to_another_workspace(op, given):
    orm = other_workspace_holds_new()
    before = orm.snapshot()
    live = FakeLive()
    ok, events = run_op(op, orm, live, workspace_id=given)
    assert not ok and orm.snapshot() == before and not journal(events) and not live.calls
    # without an id, a first step_slug run validates OLD like check and finds it missing
    missing = "old_missing_or_deleted" if op == "step_slug" else "workspace_id_missing"
    expected = missing if given is None else "workspace_mismatch"
    assert events[0] == {"event": "refuse", "reason": expected}
    assert events[-1]["ok"] is False


def test_step_slug_refuses_an_id_that_is_not_the_old_workspace():
    orm = make_orm()
    before = orm.snapshot()
    ok, events = run_op("step_slug", orm, workspace_id="w2")
    assert not ok and orm.snapshot() == before
    assert events[0] == {"event": "refuse", "reason": "workspace_mismatch"}


def test_step_slug_guard_rolls_back_on_concurrent_edit():
    orm = make_orm()
    before = orm.snapshot()

    def edit_after_read(event):  # another writer changes c1 between read and update
        record = event.get("record", {})
        if record.get("table") == "issue_comments":
            row(orm, "comment", "c1")["comment_html"] = "<p>edited elsewhere</p>"

    ok, events = run_op("step_slug", orm, on_emit=edit_after_read)
    assert not ok and ("rollback",) in orm.log
    error = next(e for e in events if e["event"] == "error")
    assert error == {
        "event": "error",
        "reason": "row_changed",
        "table": "issue_comments",
        "id": "c1",
    }
    assert row(orm, "workspace", "w1")["slug"] == "old-slug"
    assert orm.snapshot() == before
    assert events[-1] == {"event": "summary", "ok": False, "step": "slug"}


def test_step_slug_guards_each_update_on_the_changed_originals():
    orm = make_orm()
    seen = []
    original_filter = orm.models["comment"].all_objects.filter

    def spy(**kw):
        seen.append(sorted(kw))
        return original_filter(**kw)

    orm.models["comment"].all_objects.filter = spy
    ok, _ = run_op("step_slug", orm)
    assert ok and ["comment_html", "comment_json", "pk"] in seen
