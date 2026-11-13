import ast
import inspect
import json
import pathlib

from fake_orm import FakeOrm

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
                    "deleted_at": None,
                },
                {  # row of a deleted comment
                    "id": "d2",
                    "workspace_id": "w1",
                    "description_html": f'<a href="{URL}">x</a>',
                    "description_json": {},
                    "deleted_at": "2026-01-01",
                },
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
    }
    assert plans[("issues", "i1", "description_html")]["count"] == 1
    assert "description_binary" in plans[("issues", "i1", "description_html")]["rebuild"]
    assert "rebuild" not in plans[("notifications", "n1", "data")]
    summary = events[-1]
    assert summary["event"] == "summary" and summary["ok"] and summary["planned"] == 6
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
        if isinstance(node, ast.Import | ast.ImportFrom):
            names = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
            assert not any(n in {"os", "pathlib", "shutil", "tempfile"} for n in names)
    # configuration is read only for WEB_URL, never emitted
    uses = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "settings"]
    assert all(isinstance(n.value, ast.Name) and n.value.id == "orm" for n in uses)
    assert pathlib.Path(backend.__file__).read_text().count("print(") == 1
