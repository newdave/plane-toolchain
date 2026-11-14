"""Shared helpers and fixtures for the split test_workspace_rename_backend files."""

import base64
import inspect
import json
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
                    "description_binary": None,
                    "description_stripped": None,
                    "deleted_at": None,
                },
                {
                    "id": "i3",
                    "workspace_id": "w2",
                    "description_html": html,
                    "description_json": {},
                    "description_binary": None,
                    "description_stripped": None,
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
                    "description_binary": None,
                    "deleted_at": None,
                },
                {  # row of a deleted comment
                    "id": "d2",
                    "workspace_id": "w1",
                    "description_html": f'<a href="{URL}">x</a>',
                    "description_json": {},
                    "description_stripped": None,
                    "description_binary": None,
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


def delete_on_reread(orm):
    """Delete i1 when step_rows next filters on its pk: a concurrent delete after the page
    read (the guarded update is that next filter)."""
    manager = orm.models["issue"].all_objects
    original_filter = manager.filter

    def spy(**kw):
        if kw.get("pk") == "i1":
            orm.models["issue"].rows[:] = [r for r in orm.models["issue"].rows if r["id"] != "i1"]
        return original_filter(**kw)

    manager.filter = spy


def fail_db_update(orm):
    orm.models["issue"].fail_update = True


def str_binary(orm):
    row(orm, "issue", "i1")["description_binary"] = "not bytes"


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


def _row_record(table, row_id, fields):
    return {"kind": "row", "table": table, "id": row_id, "fields": fields}


def run_health(orm, probe, old="old-slug", new="new-slug", **extra):
    events = []
    extra.setdefault("workspace_id", "w1")  # the journalled id, as T7 passes it
    args = {"old": old, "new": new, **{k: v for k, v in extra.items() if v is not None}}
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


def run_flush(orm, **extra):
    return run_op("flush_cache", orm, **extra)
