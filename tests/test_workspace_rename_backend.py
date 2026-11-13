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
                    "description_json": {},
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
    }
    assert plans[("issues", "i1", "description_html")]["count"] == 1
    assert "description_binary" in plans[("issues", "i1", "description_html")]["rebuild"]
    assert "rebuild" not in plans[("notifications", "n1", "data")]
    summary = events[-1]
    assert summary["event"] == "summary" and summary["ok"] and summary["planned"] == 4
    assert summary["rebuild"] == 2


def test_check_includes_deleted_and_archived_rows():
    orm = make_orm()
    orm.models["issue"].rows[0]["deleted_at"] = "2026-01-01"
    ok, events = run_check(orm)
    ids = {e["id"] for e in events if e["event"] == "plan"}
    assert ok and {"i1", "p1"} <= ids  # deleted issue and archived page


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
    }
    assert unfixed[("webhook_logs", "response_body")]["ids"] == ["l1"]
    assert events[-1]["unfixed"] == 2
    assert orm.snapshot() == before
    assert all(t.updates == [] for t in orm.models.values())


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
