import ast
import base64
import builtins
import inspect
import json
import pathlib
import symtable
import sys
import types

import pytest
from fake_orm import FakeOrm, clone

from plane_toolchain import workspace_rename_backend as backend
from plane_toolchain import workspace_rename_match as match

URL = "https://plane.example.com/old-slug/browse/X-1/"
PRIVATE = "private-sentence-xyz"


def snippet_source():
    return inspect.getsource(match) + "\n" + inspect.getsource(backend)


def make_orm(**extra):
    html = f'<p>{PRIVATE} <a href="{URL}">x</a></p>'
    tables = {
        "workspace": (
            "workspaces",
            {"slug": "text"},
            [
                {"id": "w1", "slug": "old-slug", "deleted_at": None},
                {"id": "w2", "slug": "other", "deleted_at": None},
            ],
        ),
        "issue": (
            "issues",
            {"description_html": "text", "description_json": "json"},
            [
                {
                    "id": "i1",
                    "workspace_id": "w1",
                    "description_html": html,
                    "description_json": {"type": "link", "href": URL},
                    "description_binary": b"\x00old-binary\xff",
                    "description_stripped": f"{PRIVATE} {URL}",
                    "updated_at": "2026-01-02",
                    "deleted_at": None,
                },
                {
                    "id": "i2",
                    "workspace_id": "w1",
                    "description_html": "<p>none</p>",
                    "description_json": {},
                    "deleted_at": None,
                },
                {
                    "id": "i3",
                    "workspace_id": "w2",
                    "description_html": html,
                    "description_json": {},
                    "deleted_at": None,
                },
            ],
        ),
        "page": (
            "pages",
            {"description_html": "text"},
            [
                {
                    "id": "p1",
                    "workspace_id": "w1",
                    "description_html": html,
                    "description_json": {"type": "doc"},
                    "description_binary": memoryview(b"\x01page-binary"),
                    "description_stripped": None,
                    "updated_at": "2026-01-03",
                    "deleted_at": None,
                    "archived_at": "2026-01-01",
                }
            ],
        ),
        "comment": (
            "issue_comments",
            {"comment_html": "text"},
            [
                {
                    "id": "c1",
                    "workspace_id": "w1",
                    "comment_html": '<a href="/old-slug/x">a</a>',
                    "comment_stripped": "a",
                    "comment_json": {"type": "link", "attrs": {"href": "/old-slug/x"}},
                    "deleted_at": None,
                }
            ],
        ),
        "notification": (
            "notifications",
            {"data": "json"},
            [
                {
                    "id": "n1",
                    "workspace_id": "w1",
                    "data": {"workspace_slug": "old-slug"},
                    "deleted_at": None,
                }
            ],
        ),
        "description": (
            "descriptions",
            {"description_html": "text", "description_json": "json"},
            [
                {
                    "id": "d1",
                    "workspace_id": "w1",
                    "description_html": f'<a href="{URL}">x</a>',
                    "description_json": {"type": "link", "href": URL},
                    "description_stripped": f"see {URL}",
                    "deleted_at": None,
                },
                {  # row of a deleted comment
                    "id": "d2",
                    "workspace_id": "w1",
                    "description_html": f'<a href="{URL}">x</a>',
                    "description_json": {},
                    "description_stripped": None,
                    "deleted_at": "2026-01-01",
                },
            ],
        ),
        "email_log": (
            "email_notification_logs",
            {"data": "json"},
            [
                {"id": "e1", "data": {"issue": {"workspace_slug": "old-slug"}}, "deleted_at": None},
                {"id": "e2", "data": {"workspace_slug": "other"}, "deleted_at": None},
            ],
        ),
        "webhook_log": (
            "webhook_logs",
            {"response_body": "text"},
            [{"id": "l1", "response_body": f"{URL} {PRIVATE}", "deleted_at": None}],
        ),
        "version": (
            "issue_versions",
            {"description_html": "text"},
            [{"id": "v1", "description_html": f'<a href="{URL}">x</a>', "deleted_at": None}],
        ),
    }
    tables.update(extra)
    return FakeOrm(tables)


def run_check(orm, old="old-slug", new="new-slug"):
    events = []
    ok = backend.run("check", {"old": old, "new": new}, orm, events.append, None)
    return ok, events


def namespace_run(orm, old="old-slug", new="new-slug"):
    """Exec the combined shipped source, as the container does, then call run."""
    ns = {}
    exec(compile(snippet_source(), "snippet", "exec"), ns)  # noqa: S102
    events = []
    ok = ns["run"]("check", {"old": old, "new": new}, orm, events.append, None)
    return ok, events


def test_check_reports_planned_rewrites_per_record_and_field():
    ok, events = namespace_run(make_orm())
    assert ok
    plans = {(e["table"], e["id"], e["field"]): e for e in events if e["event"] == "plan"}
    assert set(plans) == {
        ("issues", "i1", "description_html"),
        ("pages", "p1", "description_html"),
        ("issue_comments", "c1", "comment_html"),
        ("notifications", "n1", "data"),
        ("descriptions", "d1", "description_html"),
        ("descriptions", "d2", "description_html"),
        ("email_notification_logs", "e1", "data"),
    }
    assert plans[("issues", "i1", "description_html")]["count"] == 1
    assert "description_binary" in plans[("issues", "i1", "description_html")]["rebuild"]
    assert "rebuild" not in plans[("notifications", "n1", "data")]
    summary = events[-1]
    assert summary["event"] == "summary" and summary["ok"] and summary["planned"] == 7
    assert summary["rebuild"] == 4


def test_check_includes_deleted_and_archived_rows():
    orm = make_orm()
    orm.models["issue"].rows[0]["deleted_at"] = "2026-01-01"
    ok, events = run_check(orm)
    ids = {e["id"] for e in events if e["event"] == "plan"}
    assert ok and {"i1", "p1"} <= ids  # deleted issue and archived page


def test_check_plans_description_rows_and_does_not_report_them_unfixed():
    ok, events = run_check(make_orm())
    assert ok
    assert not any(e["event"] == "unfixed" and e["table"] == "descriptions" for e in events)


def test_check_does_not_report_rebuilt_columns_of_in_scope_rows_as_unfixed():
    # i1, p1-style rows carry a matching description_json that apply rebuilds
    ok, events = run_check(make_orm())
    assert ok
    unfixed = {(e["table"], e["column"]) for e in events if e["event"] == "unfixed"}
    assert ("issues", "description_json") not in unfixed
    assert ("descriptions", "description_json") not in unfixed


def test_check_streams_unfixed_scan_and_caps_ids():
    rows = [
        {"id": f"v{n}", "description_html": f'<a href="{URL}">x</a>', "deleted_at": None}
        for n in range(60)
    ]
    orm = make_orm(version=("issue_versions", {"description_html": "text"}, rows))
    ok, events = run_check(orm)
    event = next(e for e in events if e["event"] == "unfixed" and e["table"] == "issue_versions")
    assert event["count"] == 60 and len(event["ids"]) == 50 and event["truncated"] is True
    assert orm.models["version"].streamed >= 1


def test_check_refuses_new_slug_taken_by_soft_deleted_workspace():
    orm = make_orm()
    orm.models["workspace"].rows.append(
        {"id": "w3", "slug": "new-slug", "deleted_at": "2026-01-01"}
    )
    ok, events = run_check(orm)
    assert not ok
    assert any(e["event"] == "refuse" and e["reason"] == "new_slug_invalid" for e in events)
    assert not any(e["event"] == "plan" for e in events)


def test_check_refuses_old_missing_or_deleted():
    ok, events = run_check(make_orm(), old="nope")
    assert not ok and events[0] == {"event": "refuse", "reason": "old_missing_or_deleted"}
    orm = make_orm()
    orm.models["workspace"].rows[0]["deleted_at"] = "2026-01-01"
    ok, events = run_check(orm)
    assert not ok and events[0]["reason"] == "old_missing_or_deleted"


def test_check_reports_unfixed_hits_in_other_tables_without_changing_them():
    orm = make_orm()
    before = orm.snapshot()
    ok, events = run_check(orm)
    unfixed = {(e["table"], e["column"]): e for e in events if e["event"] == "unfixed"}
    assert set(unfixed) == {
        ("webhook_logs", "response_body"),
        ("issue_versions", "description_html"),
        ("issues", "description_html"),  # i3 lives in another workspace
    }
    assert unfixed[("webhook_logs", "response_body")]["ids"] == ["l1"]
    assert unfixed[("webhook_logs", "response_body")]["reason"] == "out_of_scope"
    assert events[-1]["unfixed"] == 3
    assert orm.snapshot() == before
    assert all(t.updates == [] for t in orm.models.values())


def test_check_reports_cross_workspace_links_without_planning_them():
    ok, events = run_check(make_orm())
    plans = {e["id"] for e in events if e["event"] == "plan"}
    assert "i3" not in plans
    other = [e for e in events if e.get("reason") == "other_workspace"]
    assert other == [
        {
            "event": "unfixed",
            "table": "issues",
            "column": "description_html",
            "reason": "other_workspace",
            "count": 1,
            "ids": ["i3"],
        }
    ]


def test_check_caps_cross_workspace_ids():
    html = f'<a href="{URL}">x</a>'
    orm = make_orm()
    orm.models["issue"].rows.extend(
        {
            "id": f"x{n}",
            "workspace_id": "w2",
            "description_html": html,
            "description_json": {},
            "deleted_at": None,
        }
        for n in range(60)
    )
    ok, events = run_check(orm)
    event = next(e for e in events if e.get("reason") == "other_workspace")
    assert event["count"] == 61 and len(event["ids"]) == 50 and event["truncated"] is True


def test_check_queries_by_field_name_and_reports_column_name():
    rows = [{"id": "a1", "body_text": f'<a href="{URL}">x</a>', "deleted_at": None}]
    orm = make_orm(
        attn=("attn_table", {"body": "text"}, rows, {"body": "body_text"}),
    )
    ok, events = run_check(orm)
    event = next(e for e in events if e.get("table") == "attn_table")
    assert event["column"] == "body" and event["ids"] == ["a1"]


def test_check_never_emits_row_content():
    ok, events = run_check(make_orm())
    blob = json.dumps(events)
    assert PRIVATE not in blob and "plane.example.com" not in blob
    allowed = {
        "event",
        "table",
        "id",
        "field",
        "count",
        "rebuild",
        "column",
        "ids",
        "ok",
        "planned",
        "truncated",
        "unfixed",
        "reason",
        "detail",
    }
    assert all(set(e) <= allowed for e in events)


def test_snippet_source_has_no_disk_writes_or_prints_of_config():
    tree = ast.parse(snippet_source())
    live = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_live_convert"
    )
    live_imports = {
        id(n)
        for n in ast.walk(live)
        if isinstance(n, ast.Import) and [a.name for a in n.names] == ["os"]
    }
    assert len(live_imports) == 1
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                assert f.id != "open", "disk write"
            if isinstance(f, ast.Attribute):
                assert f.attr not in {
                    "write",
                    "write_text",
                    "write_bytes",
                    "open",
                    "dump",
                    "mkdir",
                    "getenv",
                    "environ",
                }, f.attr
        if isinstance(node, ast.Import | ast.ImportFrom) and id(node) not in live_imports:
            names = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
            assert not any(n in {"os", "pathlib", "shutil", "tempfile"} for n in names)
    # One exception: _live_convert imports os only to read CE_PLANE_LIVE_URL, never writes
    # files and never emits the URL.
    assert len([n for n in ast.walk(live) if isinstance(n, ast.Name) and n.id == "os"]) == 1
    reads = [
        ast.unparse(n)
        for n in ast.walk(live)
        if isinstance(n, ast.Call) and "os." in ast.unparse(n.func)
    ]
    assert reads == ["os.environ.get('CE_PLANE_LIVE_URL')"]
    # configuration is read only for WEB_URL, never emitted
    uses = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "settings"]
    assert all(isinstance(n.value, ast.Name) and n.value.id == "orm" for n in uses)
    assert pathlib.Path(backend.__file__).read_text().count("print(") == 1


# --- apply steps (step_slug, step_rows) and the journal, run through the assembled snippet ---

NEW_URL = "https://plane.example.com/new-slug/browse/X-1/"


def snippet_namespace():
    ns = {}
    exec(compile(snippet_source(), "snippet", "exec"), ns)  # noqa: S102
    return ns


class FakeLive:
    """Stand-in for Live /live/convert-document/: records (html, variant), never touches rows."""

    def __init__(self, fail_on=None, result=None):
        self.calls, self.fail_on, self.result = [], fail_on, result

    def __call__(self, html, variant):
        self.calls.append((html, variant))
        if self.fail_on is not None and self.fail_on(html, variant, len(self.calls)):
            raise RuntimeError("Live unavailable")
        if self.result is not None:
            return self.result
        n = len(self.calls)
        return {
            "description_json": {"type": "doc", "variant": variant, "n": n},
            "description_binary": base64.b64encode(f"bin-{variant}-{n}".encode()).decode(),
            "description_stripped": f"stripped-{n}",
        }


def run_op(op, orm, convert=None, ns=None, old="old-slug", new="new-slug", on_emit=None, **extra):
    """Run one op; step_rows gets the renamed workspace id, as T7 passes it from the journal."""
    ns = ns or snippet_namespace()
    events = []
    if op == "step_rows":
        extra.setdefault("workspace_id", "w1")

    def emit(event):
        orm.log.append(("emit", event))
        events.append(event)
        if on_emit is not None:
            on_emit(event)

    ok = ns["run"](op, {"old": old, "new": new, **extra}, orm, emit, convert)
    return ok, events


def journal(events):
    return [e["record"] for e in events if e["event"] == "journal"]


def row(orm, key, row_id):
    return next(r for r in orm.models[key].rows if r["id"] == row_id)


def log_index(orm, predicate):
    return [i for i, entry in enumerate(orm.log) if predicate(entry)]


def is_journal(entry):
    return entry[0] == "emit" and entry[1]["event"] == "journal"


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


def other_workspace_holds_new():
    """OLD never existed here, and NEW belongs to a workspace this rename never touched."""
    orm = make_orm()
    orm.models["workspace"].rows[:] = [
        {"id": "w1", "slug": "unrelated", "deleted_at": None},
        {"id": "w2", "slug": "new-slug", "deleted_at": None},
    ]
    for table in orm.models.values():
        for r in table.rows:
            if r.get("workspace_id") == "w1":
                r["workspace_id"] = "w2"  # NEW's rows carry OLD links
    return orm


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


def test_journal_line_precedes_each_row_write():
    orm = make_orm()
    run_op("step_slug", orm)
    run_op("step_rows", orm, FakeLive())
    journalled, writes = set(), 0
    for entry in orm.log:
        if is_journal(entry):
            record = entry[1]["record"]
            table = "workspaces" if record["kind"] == "workspace" else record["table"]
            journalled.add((table, record["id"]))
        if entry[0] == "update":
            for row_id in entry[2]:
                writes += 1
                assert (entry[1], row_id) in journalled, (entry[1], row_id)
    assert writes == 8  # step_slug: workspace, c1, d1, d2, n1, e1; step_rows: i1, p1


def test_step_rows_uses_queryset_update_not_save():
    tree = ast.parse(inspect.getsource(backend))
    calls = {
        n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "save" not in calls and "update" in calls
    orm = make_orm()
    run_op("step_slug", orm)
    ok, _ = run_op("step_rows", orm, FakeLive())
    assert ok
    fields = ["description_binary", "description_html", "description_json", "description_stripped"]
    assert orm.models["issue"].updates == [(fields, 1)]
    assert orm.models["page"].updates == [(fields, 1)]
    assert row(orm, "issue", "i1")["updated_at"] == "2026-01-02"
    assert row(orm, "page", "p1")["updated_at"] == "2026-01-03"


def test_step_rows_rebuilds_issue_with_variant_rich():
    orm = make_orm()
    run_op("step_slug", orm)
    live = FakeLive()
    ok, events = run_op("step_rows", orm, live)
    assert ok
    html, variant = live.calls[0]
    assert variant == "rich" and NEW_URL in html and "old-slug" not in html
    issue = row(orm, "issue", "i1")
    assert issue["description_html"] == html
    assert issue["description_json"] == {"type": "doc", "variant": "rich", "n": 1}
    assert issue["description_binary"] == b"bin-rich-1"  # base64 from Live, stored as bytes
    assert issue["description_stripped"] == "stripped-1"
    assert events[-1] == {"event": "summary", "ok": True, "step": "rows", "rewritten": 2}


def test_step_rows_rebuilds_page_with_variant_document():
    orm = make_orm()
    run_op("step_slug", orm)
    live = FakeLive()
    ok, _ = run_op("step_rows", orm, live)
    assert ok and [v for _, v in live.calls] == ["rich", "document"]
    page = row(orm, "page", "p1")
    assert page["description_json"]["variant"] == "document"
    assert page["description_binary"] == b"bin-document-2"


def test_step_rows_rewrites_original_stripped_when_live_omits_it():
    orm = make_orm()
    run_op("step_slug", orm)
    live = FakeLive(result={"description_json": {"type": "doc"}, "description_binary": "AAE="})
    ok, _ = run_op("step_rows", orm, live)
    assert ok
    assert row(orm, "issue", "i1")["description_stripped"] == f"{PRIVATE} {NEW_URL}"
    assert row(orm, "page", "p1")["description_stripped"] is None


def test_step_rows_skips_rows_without_match():
    orm = make_orm()
    run_op("step_slug", orm)
    before = {r["id"]: r for r in orm.snapshot()["issue"]}
    live = FakeLive()
    ok, events = run_op("step_rows", orm, live)
    assert ok and len(live.calls) == 2
    assert row(orm, "issue", "i2") == before["i2"]
    assert row(orm, "issue", "i3") == before["i3"]  # other workspace: never rewritten
    assert {(r["table"], r["id"]) for r in journal(events)} == {("issues", "i1"), ("pages", "p1")}


def test_step_rows_iterates_in_pages():
    rows = [
        {
            "id": f"i{n:03}",
            "workspace_id": "w1",
            "description_html": f'<a href="{URL}">x</a>' if n % 2 else "<p>none</p>",
            "description_json": {},
            "description_binary": b"",
            "description_stripped": "",
            "deleted_at": None,
        }
        for n in range(25)
    ]
    orm = make_orm(issue=("issues", {"description_html": "text"}, rows))
    ns = snippet_namespace()
    ns["PAGE_SIZE"] = 4
    run_op("step_slug", orm, ns=ns)
    ok, events = run_op("step_rows", orm, FakeLive(), ns=ns)
    assert ok and events[-1]["rewritten"] == 12 + 1  # 12 odd issues and the page
    pages = orm.models["issue"].pages
    assert len(pages) >= 7 and max(pages) <= 4


def test_step_rows_resume_rederives_remaining_after_partial():
    orm = make_orm()
    run_op("step_slug", orm)

    def kill_on_second(html, variant, n):
        if n == 2:
            raise KeyboardInterrupt  # the process dies mid-step: nothing catches it
        return False

    events = []
    ns = snippet_namespace()
    args = {"old": "old-slug", "new": "new-slug", "workspace_id": "w1"}
    with pytest.raises(KeyboardInterrupt):
        ns["run"]("step_rows", args, orm, events.append, FakeLive(kill_on_second))
    assert [(r["table"], r["id"]) for r in journal(events)] == [("issues", "i1")]
    assert "old-slug" in row(orm, "page", "p1")["description_html"]
    live = FakeLive()
    ok, resumed = run_op("step_rows", orm, live)
    assert ok and [v for _, v in live.calls] == ["document"]  # i1 is not converted again
    assert [(r["table"], r["id"]) for r in journal(resumed)] == [("pages", "p1")]
    assert "old-slug" not in row(orm, "page", "p1")["description_html"]
    ok, again = run_op("step_rows", orm, FakeLive())
    assert ok and not journal(again) and again[-1]["rewritten"] == 0


@pytest.mark.parametrize(
    "result,fail",
    [
        (None, True),
        ({"description_json": {"type": "doc"}}, False),  # no binary
        ({"description_json": {}, "description_binary": "not base64!"}, False),
        ({"description_json": "x", "description_binary": "AAE="}, False),
    ],
    ids=["raises", "missing_binary", "bad_base64", "bad_json"],
)
def test_step_rows_convert_failure_aborts_without_writing_that_row(result, fail):
    orm = make_orm()
    run_op("step_slug", orm)
    before = orm.snapshot()["page"]
    if result is not None:  # the issue converts fine; the page gets the bad answer
        good = FakeLive()

        def live(html, variant):
            return good(html, variant) if variant == "rich" else result
    else:
        live = FakeLive(fail_on=lambda html, variant, n: variant == "document")
    ok, events = run_op("step_rows", orm, live)
    assert not ok
    assert orm.snapshot()["page"] == before
    assert ("pages", "p1") not in {(r.get("table"), r["id"]) for r in journal(events)}
    error = next(e for e in events if e["event"] == "error")
    assert error == {"event": "error", "reason": "convert_failed", "table": "pages", "id": "p1"}
    assert events[-1] == {"event": "summary", "ok": False, "step": "rows", "rewritten": 1}


def test_step_rows_refuses_without_live_or_before_slug_step():
    orm = make_orm()
    before = orm.snapshot()
    ok, events = run_op("step_rows", orm, FakeLive())
    assert not ok and events[0] == {"event": "refuse", "reason": "slug_not_renamed"}
    run_op("step_slug", orm)
    ok, events = run_op("step_rows", orm, None)
    assert not ok and events[0] == {"event": "refuse", "reason": "live_url_missing"}
    assert orm.snapshot()["issue"] == before["issue"]


def test_journal_roundtrip_bytes_and_json_equal_originals(tmp_path):
    """Lines persisted by the T5 store decode to the exact originals, and writing them back by
    queryset update restores the original database byte for byte."""
    from plane_toolchain import workspace_rename as wr

    orm = make_orm()
    original = orm.snapshot()
    ns = snippet_namespace()
    directory = wr.secure_receipt_dir(tmp_path / "receipt")
    for op, live in (("step_slug", None), ("step_rows", FakeLive())):
        ok, events = run_op(op, orm, live, ns=ns)
        assert ok
        for line in [json.dumps(e, sort_keys=True) for e in events]:  # the snippet's stdout
            event = json.loads(line)
            if event["event"] == "journal":
                wr.append_journal(directory, event["record"])
    raw = (directory / "records.jsonl").read_text().splitlines()
    canonical = [json.dumps(json.loads(x), sort_keys=True, separators=(",", ":")) for x in raw]
    assert raw == canonical
    records = wr.load_journal(directory)
    page = next(r for r in records if r.get("table") == "pages")
    assert page["fields"]["description_binary"] == base64.b64encode(b"\x01page-binary").decode()
    assert ns["journal_values"](page)["description_binary"] == b"\x01page-binary"
    tables = {t.db_table: k for k, t in orm.models.items()}
    for record in records:  # what T4's restore does, by queryset update
        if record["kind"] == "workspace":
            manager = orm.models["workspace"].all_objects
            manager.filter(pk=record["id"]).update(slug=record["slug"])
            continue
        values = ns["journal_values"](record)
        orm.models[tables[record["table"]]].all_objects.filter(pk=record["id"]).update(**values)

    def normal(snapshot):
        encode = lambda v: {"bytes": base64.b64encode(bytes(v)).decode()}  # noqa: E731
        return json.loads(json.dumps(snapshot, sort_keys=True, default=encode))

    assert normal(orm.snapshot()) == normal(original)
    assert row(orm, "issue", "i1")["description_binary"] == b"\x00old-binary\xff"


def test_apply_steps_emit_row_content_only_in_journal_events():
    orm = make_orm()
    _, slug_events = run_op("step_slug", orm)
    _, row_events = run_op("step_rows", orm, FakeLive())
    others = [e for e in slug_events + row_events if e["event"] != "journal"]
    blob = json.dumps(others)
    assert PRIVATE not in blob and "plane.example.com" not in blob and "old-slug" not in blob
    allowed = {"event", "ok", "step", "rewritten", "already", "reason", "table", "id", "detail"}
    allowed |= {"not_rebuilt", "workspace_id"}
    assert all(set(e) <= allowed for e in others)


def test_snippet_live_convert_uses_payload_or_container_url(monkeypatch):
    posts = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"description_json": {}, "description_binary": "AAE="}

    def post(url, json, timeout):
        posts.append((url, json, timeout))
        return Response()

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(post=post))
    monkeypatch.setenv("CE_PLANE_LIVE_URL", "http://live.internal:3000/")
    ns = snippet_namespace()
    assert ns["_live_convert"]({})("<p>x</p>", "rich")["description_binary"] == "AAE="
    body = {"description_html": "<p>x</p>", "variant": "rich"}
    assert posts == [("http://live.internal:3000/live/convert-document/", body, 20)]
    ns["_live_convert"]({"live_url": "http://other:3000"})("<p/>", "document")
    assert posts[-1][0] == "http://other:3000/live/convert-document/"
    monkeypatch.delenv("CE_PLANE_LIVE_URL")
    assert ns["_live_convert"]({}) is None


def test_snippet_main_emits_sorted_json_lines_and_passes_convert():
    ns, orm, lines, seen = snippet_namespace(), make_orm(), [], []

    def factory(args):
        seen.append(args)
        return FakeLive()

    slug_args = {"old": "old-slug", "new": "new-slug"}
    rows_args = {**slug_args, "workspace_id": "w1"}
    for op, args in (("step_slug", slug_args), ("step_rows", rows_args)):
        ns["_main"]({"op": op, "args": args}, lambda: orm, factory, lines.append)
    assert all(line == json.dumps(json.loads(line), sort_keys=True) for line in lines)
    summaries = [json.loads(x) for x in lines if json.loads(x)["event"] == "summary"]
    assert [s["ok"] for s in summaries] == [True, True] and summaries[-1]["rewritten"] == 2
    assert seen == [slug_args, rows_args]


def delete_on_reread(orm):
    """Delete i1 when step_rows re-reads it by pk: a concurrent delete after the page read."""
    manager = orm.models["issue"].all_objects
    original_filter = manager.filter

    def spy(**kw):
        if kw == {"pk": "i1"}:
            orm.models["issue"].rows[:] = [r for r in orm.models["issue"].rows if r["id"] != "i1"]
        return original_filter(**kw)

    manager.filter = spy


def fail_db_update(orm):
    orm.models["issue"].fail_update = True


def str_binary(orm):
    row(orm, "issue", "i1")["description_binary"] = "not bytes"


@pytest.mark.parametrize(
    "inject,detail",
    [
        (delete_on_reread, "IndexError"),
        (fail_db_update, "RuntimeError"),
        (str_binary, "JournalValueError"),
    ],
    ids=["concurrent_delete", "db_error", "str_binary"],
)
def test_step_rows_reports_unexpected_errors_with_summary(inject, detail):
    orm = make_orm()
    run_op("step_slug", orm)
    inject(orm)
    ok, events = run_op("step_rows", orm, FakeLive())
    assert not ok
    error = next(e for e in events if e["event"] == "error")
    assert error == {
        "event": "error",
        "reason": "row_failed",
        "table": "issues",
        "id": "i1",
        "detail": detail,
    }
    assert events[-1] == {"event": "summary", "ok": False, "step": "rows", "rewritten": 0}
    assert PRIVATE not in json.dumps([e for e in events if e["event"] != "journal"])
    assert "old-slug" in row(orm, "page", "p1")["description_html"]  # stopped at i1


def test_journal_value_refuses_str_for_binary_columns():
    ns = snippet_namespace()
    with pytest.raises(ns["JournalValueError"]):
        ns["_journal_value"]("description_binary", "text")
    assert ns["_journal_value"]("description_binary", bytearray(b"\x00")) == "AA=="
    assert ns["_journal_value"]("description_html", "text") == "text"


def test_snippet_functions_reference_only_defined_globals():
    """The assembled snippet must define every global its functions use (no missing import)."""
    table = symtable.symtable(snippet_source(), "<snippet>", "exec")
    defined = {s.get_name() for s in table.get_symbols() if s.is_assigned() or s.is_imported()}
    defined |= {s.get_name() for s in table.get_symbols() if s.is_namespace()}
    defined |= set(dir(builtins))
    missing = {}

    def walk(scope):
        for child in scope.get_children():
            for symbol in child.get_symbols():
                if symbol.is_global() and symbol.get_name() not in defined:
                    missing.setdefault(child.get_name(), set()).add(symbol.get_name())
            walk(child)

    walk(table)
    assert not missing, f"snippet functions use undefined globals: {missing}"


# --- restore (T4): the journal arrives inside the request, never on stdin ---


def normal_snapshot(snapshot):
    """Snapshot as JSON, binary as base64, so memoryview and bytes originals compare equal."""
    encode = lambda v: {"bytes": base64.b64encode(bytes(v)).decode()}  # noqa: E731
    return json.loads(json.dumps(snapshot, sort_keys=True, default=encode))


def apply_all(orm, ns=None):
    """Run step_slug and step_rows; return the journal as the host persisted it (JSON lines)."""
    ns = ns or snippet_namespace()
    records = []
    for op, live in (("step_slug", None), ("step_rows", FakeLive())):
        ok, events = run_op(op, orm, live, ns=ns)
        assert ok
        records += [json.loads(json.dumps(r, sort_keys=True)) for r in journal(events)]
    return records


def restore(orm, records, convert=None, **extra):
    return run_op("restore", orm, convert, journal=records, **extra)


def clone_row(orm, key, row_id):
    return clone(row(orm, key, row_id))


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


def _row_record(table, row_id, fields):
    return {"kind": "row", "table": table, "id": row_id, "fields": fields}


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


# --- health and flush_cache (T4) ---


def run_health(orm, probe, old="old-slug", new="new-slug", **extra):
    events = []
    args = {"old": old, "new": new, **extra}
    ok = snippet_namespace()["run"]("health", args, orm, events.append, None, probe)
    return ok, events


def checks(events):
    return {e["check"]: e["ok"] for e in events if e["event"] == "health"}


def renamed_orm():
    orm = make_orm()
    assert run_op("step_slug", orm)[0]
    return orm


def raise_oserror():
    raise OSError("connection refused")


@pytest.mark.parametrize(
    "setup,probe,failed",
    [
        (renamed_orm, lambda: 200, set()),
        (make_orm, lambda: 200, {"new_present", "old_absent"}),  # not renamed yet
        (renamed_orm, lambda: 503, {"api"}),
        (renamed_orm, raise_oserror, {"api"}),
        (renamed_orm, None, {"api"}),
    ],
    ids=["healthy", "not_renamed", "api_503", "api_down", "no_probe"],
)
def test_health_requires_new_present_and_old_absent(setup, probe, failed):
    orm = setup()
    before, writes = orm.snapshot(), len(orm.log)
    ok, events = run_health(orm, probe)
    assert ok is (not failed)
    assert checks(events) == {
        name: name not in failed for name in ("api", "new_present", "old_absent")
    }
    assert events[-1] == {"event": "summary", "ok": not failed, "step": "health"}
    assert orm.snapshot() == before and len(orm.log) == writes  # read-only: no update logged


def test_health_fails_when_another_workspace_holds_old():
    orm = renamed_orm()
    orm.models["workspace"].rows.append({"id": "w9", "slug": "old-slug", "deleted_at": None})
    ok, events = run_health(orm, lambda: 200)
    assert not ok and checks(events)["old_absent"] is False


def test_health_binds_new_to_the_given_workspace_id():
    orm = renamed_orm()
    ok, events = run_health(orm, lambda: 200, workspace_id="w1")
    assert ok and checks(events)["workspace"] is True
    ok, events = run_health(orm, lambda: 200, workspace_id="w2")
    assert not ok and checks(events)["workspace"] is False


def test_health_checks_a_revert_with_the_slugs_swapped():
    orm = renamed_orm()
    records = [json.loads(json.dumps(r)) for r in journal(run_op("step_slug", make_orm())[1])]
    assert restore(orm, records)[0]
    ok, _ = run_health(orm, lambda: 200, old="new-slug", new="old-slug", workspace_id="w1")
    assert ok


def test_snippet_api_probe_gets_instances_endpoint(monkeypatch):
    gets = []

    def get(url, timeout):
        gets.append((url, timeout))
        return types.SimpleNamespace(status_code=200)

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(get=get))
    ns = snippet_namespace()
    assert ns["_api_probe"]({})() == 200
    assert gets == [("http://127.0.0.1:8000/api/instances/", 10)]
    ns["_api_probe"]({"api_url": "http://api:8000/"})()
    assert gets[-1][0] == "http://api:8000/api/instances/"


def test_snippet_main_passes_the_api_probe_to_health():
    ns, orm, lines, seen = snippet_namespace(), renamed_orm(), [], []

    def probe_factory(args):
        seen.append(args)
        return lambda: 200

    request = {"op": "health", "args": {"old": "old-slug", "new": "new-slug"}}
    ns["_main"](request, lambda: orm, lambda args: None, lines.append, probe_factory)
    assert json.loads(lines[-1]) == {"event": "summary", "ok": True, "step": "health"}
    assert seen == [request["args"]]


def run_flush(orm, **extra):
    return run_op("flush_cache", orm, **extra)


def test_flush_cache_clears_cache():
    orm = make_orm()
    orm.cache.update({"/api/workspaces/": "old-slug", "/api/users/me/workspaces/": "x"})
    ok, events = run_flush(orm)
    assert ok and orm.cache == {}
    assert events == [
        {
            "event": "summary",
            "ok": True,
            "step": "cache",
            "cleared": True,
            "sessions_in_cache": False,
        }
    ]


def test_flush_cache_dry_run_reports_session_cache_without_clearing():
    orm = make_orm()
    orm.cache["k"] = "v"
    orm.settings.SESSION_ENGINE = "django.contrib.sessions.backends.cache"
    ok, events = run_flush(orm, dry_run=True)
    assert ok and orm.cache == {"k": "v"}
    assert events[-1]["cleared"] is False and events[-1]["sessions_in_cache"] is True
    orm.settings.SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"  # db survives
    assert run_flush(orm, dry_run=True)[1][-1]["sessions_in_cache"] is False


def test_flush_cache_failure_ends_with_summary_false():
    class Broken:
        def clear(self):
            raise ConnectionError("redis down")

    orm = make_orm()
    orm.cache = Broken()
    ok, events = run_flush(orm)
    assert not ok
    assert events == [
        {"event": "error", "reason": "cache_clear_failed", "detail": "ConnectionError"},
        {"event": "summary", "ok": False, "step": "cache"},
    ]
