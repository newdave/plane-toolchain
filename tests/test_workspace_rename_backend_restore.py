"""Workspace rename backend snippet: restore from the journal."""

import json
import pytest
from support_workspace_rename_backend import (
    FakeLive,
    PRIVATE,
    _row_record,
    apply_all,
    clone_row,
    journal,
    make_orm,
    normal_snapshot,
    restore,
    row,
    run_op,
    snippet_namespace,
)


def test_restore_writes_every_journalled_field_back_exactly():
    orm = make_orm()
    original = orm.snapshot()
    records = apply_all(orm)
    ok, events = restore(orm, records)
    assert ok, events
    assert normal_snapshot(orm.snapshot()) == normal_snapshot(original)
    assert row(orm, "issue", "i1")["description_binary"] == b"\x00old-binary\xff"
    assert bytes(row(orm, "page", "p1")["description_binary"]) == b"\x01page-binary"
    assert row(orm, "page", "p1")["description_stripped"] is None
    assert events[-1] == {
        "event": "summary",
        "ok": True,
        "step": "restore",
        "slug_restored": True,
        "restored": 7,  # c1, d1, d2, n1, e1, i1, p1
        "already_restored": 0,
        "skipped": 0,
        "workspace_id": "w1",
    }
    # queryset updates only: never save(), so updated_at stays as stored
    assert row(orm, "issue", "i1")["updated_at"] == "2026-01-02"


def test_restore_does_not_call_convert():
    orm = make_orm()
    records = apply_all(orm)
    live = FakeLive()
    ok, _ = restore(orm, records, live)
    assert ok and live.calls == []


def test_restore_partial_journal_leaves_other_rows_untouched():
    orm = make_orm()
    original = orm.snapshot()
    records = apply_all(orm)
    rewritten_page = clone_row(orm, "page", "p1")
    rewritten_note = clone_row(orm, "notification", "n1")
    partial = [r for r in records if r.get("table") not in ("pages", "notifications")]
    ok, events = restore(orm, partial)
    assert ok and events[-1]["restored"] == 5
    assert row(orm, "page", "p1") == rewritten_page  # no journal line: left as it is
    assert row(orm, "notification", "n1") == rewritten_note
    assert row(orm, "workspace", "w1")["slug"] == "old-slug"
    assert row(orm, "issue", "i1")["description_html"] == original["issue"][0]["description_html"]


@pytest.mark.parametrize("deleted_at", [None, "2026-02-01"])
def test_restore_refuses_when_old_slug_now_taken_by_other_workspace(deleted_at):
    orm = make_orm()
    records = apply_all(orm)
    # the unique index covers soft-deleted rows, so a deleted holder blocks too
    orm.models["workspace"].rows.append({"id": "w9", "slug": "old-slug", "deleted_at": deleted_at})
    before = orm.snapshot()
    start = len(orm.log)
    ok, events = restore(orm, records)
    assert not ok and orm.snapshot() == before
    assert not [e for e in orm.log[start:] if e[0] in ("begin", "update")]
    assert events[0] == {"event": "refuse", "reason": "old_slug_taken"}
    assert events[-1] == {"event": "summary", "ok": False, "step": "restore"}


def test_restore_is_idempotent():
    orm = make_orm()
    original = orm.snapshot()
    records = apply_all(orm)
    assert restore(orm, records)[0]
    restored = orm.snapshot()
    updates = sum(len(t.updates) for t in orm.models.values())
    ok, events = restore(orm, records)
    assert ok and orm.snapshot() == restored
    assert normal_snapshot(restored) == normal_snapshot(original)
    assert sum(len(t.updates) for t in orm.models.values()) == updates  # no write at all
    summary = events[-1]
    assert summary["slug_restored"] is False and summary["restored"] == 0
    assert summary["already_restored"] == 7 and summary["skipped"] == 0


def test_restore_after_kill_mid_apply_restores_journalled_rows_only():
    orm = make_orm()
    original = orm.snapshot()
    ns = snippet_namespace()
    _, slug_events = run_op("step_slug", orm, ns=ns)
    records = journal(slug_events)
    # step_rows journals i1 and dies before writing it: its record equals the stored row
    i1 = {"kind": "row", "table": "issues", "id": "i1"}
    i1["fields"] = {
        c: ns["_journal_value"](c, row(orm, "issue", "i1")[c]) for c in ns["ROW_FIELDS"]
    }
    ok, events = restore(orm, [*records, json.loads(json.dumps(i1))])
    assert ok and normal_snapshot(orm.snapshot()) == normal_snapshot(original)
    assert events[-1]["already_restored"] == 1 and events[-1]["restored"] == 5


def test_restore_skips_and_reports_rows_edited_after_rewrite():
    orm = make_orm()
    records = apply_all(orm)
    row(orm, "page", "p1")["description_html"] = f"<p>{PRIVATE} newer user edit</p>"
    row(orm, "notification", "n1")["data"] = {"workspace_slug": "new-slug", "read": True}
    edited_page = clone_row(orm, "page", "p1")
    ok, events = restore(orm, records)
    assert not ok  # partial: not a full revert
    assert row(orm, "page", "p1") == edited_page  # never overwrite a newer edit
    assert row(orm, "notification", "n1")["data"] == {"workspace_slug": "new-slug", "read": True}
    skips = [e for e in events if e["event"] == "skip"]
    assert skips == [
        {"event": "skip", "table": "notifications", "id": "n1", "reason": "changed"},
        {"event": "skip", "table": "pages", "id": "p1", "reason": "changed"},
    ]
    assert events[-1]["skipped"] == 2 and events[-1]["restored"] == 5
    assert PRIVATE not in json.dumps(events)


def test_restore_skips_stale_record_left_by_step_rows_guard():
    orm = make_orm()
    _, slug_events = run_op("step_slug", orm)

    def edit_after_journal(event):  # step_rows journals i1, then its guard sees a changed row
        if event.get("record", {}).get("id") == "i1":
            row(orm, "issue", "i1")["description_html"] = "<p>edited meanwhile</p>"

    ok, events = run_op("step_rows", orm, FakeLive(), on_emit=edit_after_journal)
    assert not ok and events[-2]["reason"] == "row_changed"
    records = [json.loads(json.dumps(r)) for r in journal(slug_events) + journal(events)]
    ok, events = restore(orm, records)
    assert not ok and row(orm, "issue", "i1")["description_html"] == "<p>edited meanwhile</p>"
    assert [e for e in events if e["event"] == "skip"] == [
        {"event": "skip", "table": "issues", "id": "i1", "reason": "changed"}
    ]


def test_restore_skips_missing_rows():
    orm = make_orm()
    records = apply_all(orm)
    orm.models["comment"].rows.clear()  # hard-deleted since the rewrite
    ok, events = restore(orm, records)
    skip = {"event": "skip", "table": "issue_comments", "id": "c1", "reason": "missing"}
    assert not ok and skip in events and events[-1]["partial"] is True


def test_restore_with_skips_is_partial_and_keeps_restored_rows():
    orm = make_orm()
    original = orm.snapshot()
    records = apply_all(orm)
    row(orm, "page", "p1")["description_html"] = "<p>newer user edit</p>"
    ok, events = restore(orm, records)
    assert not ok
    assert events[-1] == {
        "event": "summary",
        "ok": False,
        "partial": True,
        "step": "restore",
        "slug_restored": True,
        "restored": 6,
        "already_restored": 0,
        "skipped": 1,
        "workspace_id": "w1",
    }
    assert ("commit",) in orm.log  # the restored rows stay written
    assert row(orm, "workspace", "w1")["slug"] == "old-slug"
    assert row(orm, "issue", "i1")["description_html"] == original["issue"][0]["description_html"]
    ok, again = restore(orm, records)  # a rerun stays partial and writes nothing more
    assert not ok and again[-1]["partial"] is True and again[-1]["already_restored"] == 6


def test_restore_uses_first_original_of_rolled_back_generation():
    orm = make_orm()
    original = orm.snapshot()
    ns = snippet_namespace()
    orm.models["notification"].fail_update = True
    ok, failed = run_op("step_slug", orm, ns=ns)  # journals lines, then rolls back
    assert not ok and journal(failed)
    orm.models["notification"].fail_update = False
    records = [json.loads(json.dumps(r)) for r in journal(failed)]
    records += apply_all(orm, ns)
    keys = [(r.get("table"), r["id"]) for r in records]
    assert keys.count(("issue_comments", "c1")) == 2  # a duplicate generation
    # a later duplicate with a different original never wins over the first
    first = next(r for r in records if r.get("table") == "issue_comments")
    records.append({**first, "fields": {**first["fields"], "comment_html": "<p>later</p>"}})
    ok, events = restore(orm, records)
    assert ok and normal_snapshot(orm.snapshot()) == normal_snapshot(original)
    assert events[-1]["restored"] == 7 and events[-1]["skipped"] == 0


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda rs: [], "journal_empty"),
        (lambda rs: [r for r in rs if r["kind"] != "workspace"], "workspace_record_missing"),
        (lambda rs: [*rs, {"kind": "workspace", "id": "w2", "slug": "old-slug"}], "journal_invalid"),
        (lambda rs: [{**r, "slug": "other"} if r["kind"] == "workspace" else r for r in rs], "journal_mismatch"),
        (lambda rs: [*rs, _row_record("webhook_logs", "l1", {"response_body": "x"})], "journal_invalid"),
        (lambda rs: [*rs, _row_record("issues", "i2", {"name": "x"})], "journal_invalid"),
        (lambda rs: [*rs, _row_record("issues", "i2", {})], "journal_invalid"),
        (lambda rs: [*rs, _row_record("pages", "p1", {"description_binary": "!"})], "journal_invalid"),
        (lambda rs: [*rs, "not a record"], "journal_invalid"),
    ],
    ids=["empty", "no_workspace", "two_workspaces", "other_old", "table", "column", "no_fields",
         "base64", "type"],
)  # fmt: skip
def test_restore_refuses_bad_journal_before_any_write(mutate, reason):
    orm = make_orm()
    records = apply_all(orm)
    before = orm.snapshot()
    start = len(orm.log)
    ok, events = restore(orm, mutate(records))
    assert not ok and orm.snapshot() == before
    assert not [e for e in orm.log[start:] if e[0] in ("begin", "update")]
    assert events[0] == {"event": "refuse", "reason": reason}
    assert events[-1] == {"event": "summary", "ok": False, "step": "restore"}


def test_restore_refuses_without_a_journal_in_the_request():
    ok, events = run_op("restore", make_orm())
    assert not ok and events[0] == {"event": "refuse", "reason": "journal_invalid"}
    assert events[-1] == {"event": "summary", "ok": False, "step": "restore"}


@pytest.mark.parametrize("case", ["given_other_id", "journal_id_not_holder", "journal_id_gone"])
def test_restore_binds_to_the_journalled_workspace_id(case):
    orm = make_orm()
    records = apply_all(orm)
    extra = {}
    if case == "given_other_id":
        extra["workspace_id"] = "w2"
    elif case == "journal_id_not_holder":  # the journal names w2, which holds neither slug
        records = [{**r, "id": "w2"} if r["kind"] == "workspace" else r for r in records]
    else:
        orm.models["workspace"].rows[:] = [orm.models["workspace"].rows[1]]
    before = orm.snapshot()
    ok, events = restore(orm, records, **extra)
    assert not ok and orm.snapshot() == before
    assert events[0] == {"event": "refuse", "reason": "workspace_mismatch"}
    assert events[-1] == {"event": "summary", "ok": False, "step": "restore"}


def test_restore_rolls_back_every_write_on_error():
    orm = make_orm()
    records = apply_all(orm)
    before = orm.snapshot()
    orm.models["page"].fail_update = True
    ok, events = restore(orm, records)
    assert not ok and orm.snapshot() == before and ("rollback",) in orm.log
    assert events[-2] == {"event": "error", "reason": "write_failed", "detail": "RuntimeError"}
    assert events[-1] == {"event": "summary", "ok": False, "step": "restore"}


def test_restore_emits_only_ids_tables_and_counts():
    orm = make_orm()
    records = apply_all(orm)
    row(orm, "page", "p1")["description_html"] = f"<p>{PRIVATE}</p>"
    _, events = restore(orm, records)
    blob = json.dumps(events)
    assert PRIVATE not in blob and "plane.example.com" not in blob and "old-slug" not in blob
    allowed = {"event", "ok", "step", "reason", "table", "id", "detail", "workspace_id"}
    allowed |= {"slug_restored", "restored", "already_restored", "skipped", "partial"}
    assert all(set(e) <= allowed for e in events)


def test_snippet_main_carries_the_journal_inside_the_request():
    ns, orm, lines = snippet_namespace(), make_orm(), []
    original = orm.snapshot()
    records = apply_all(orm, ns)
    request = {"op": "restore", "args": {"old": "old-slug", "new": "new-slug", "journal": records}}
    ns["_main"](json.loads(json.dumps(request)), lambda: orm, lambda args: None, lines.append)
    assert json.loads(lines[-1])["ok"] is True
    assert normal_snapshot(orm.snapshot()) == normal_snapshot(original)
