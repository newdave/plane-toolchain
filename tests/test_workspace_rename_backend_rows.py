"""Workspace rename backend snippet: row rewrite step and journal."""

import ast
import base64
import io
import builtins
import inspect
import json
import symtable
import sys
import types
import pytest
from plane_toolchain import workspace_rename_backend as backend
from support_workspace_rename_backend import (
    FakeLive,
    NEW_URL,
    PRIVATE,
    URL,
    delete_on_reread,
    fail_db_update,
    is_journal,
    journal,
    make_orm,
    row,
    run_op,
    snippet_namespace,
    snippet_source,
    str_binary,
)


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


def test_snippet_uses_requests_only_for_live_post_and_api_get():
    """`requests` appears only in _live_convert (post) and _api_probe (get), nowhere else."""
    tree = ast.parse(snippet_source())
    owners = {}
    for top in tree.body:
        for node in ast.walk(top):
            owners.setdefault(id(node), getattr(top, "name", "<module>"))
    uses = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "requests", "from requests import ..."
        if isinstance(node, ast.Import) and "requests" in [a.name for a in node.names]:
            uses.add((owners[id(node)], "import"))
            assert [a.asname for a in node.names] == [None], "aliased import of requests"
        if isinstance(node, ast.Attribute) and getattr(node.value, "id", None) == "requests":
            uses.add((owners[id(node)], node.attr))
    allowed = {("_live_convert", "import"), ("_live_convert", "post")}
    allowed |= {("_api_probe", "import"), ("_api_probe", "get")}
    assert uses == allowed
    names = {
        owners[id(n)] for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == "requests"
    }
    assert names == {"_live_convert", "_api_probe"}


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
        ns["_main"]({"op": op, "args": args}, lambda: orm, factory, lines.append, None, list)
    assert all(line == json.dumps(json.loads(line), sort_keys=True) for line in lines)
    summaries = [json.loads(x) for x in lines if json.loads(x)["event"] == "summary"]
    assert [s["ok"] for s in summaries] == [True, True] and summaries[-1]["rewritten"] == 2
    assert seen == [slug_args, rows_args]


@pytest.mark.parametrize(
    "inject,detail",
    [
        (fail_db_update, "RuntimeError"),
        (str_binary, "JournalValueError"),
    ],
    ids=["db_error", "str_binary"],
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


def test_step_rows_row_deleted_after_the_page_read_is_row_changed_and_unwritten():
    orm = make_orm()
    run_op("step_slug", orm)
    delete_on_reread(orm)
    ok, events = run_op("step_rows", orm, FakeLive())
    assert not ok
    assert {"event": "error", "reason": "row_changed", "table": "issues", "id": "i1"} in events


def test_step_rows_reads_each_row_once_and_journals_that_read():
    # PTC-94: `original` was a second query after the page read; now the page read is journalled.
    orm = make_orm()
    run_op("step_slug", orm)
    before = {r["id"]: dict(r) for r in orm.models["issue"].rows}
    manager = orm.models["issue"].all_objects
    real, lookups = manager.filter, []
    manager.filter = lambda **kw: lookups.append(sorted(kw)) or real(**kw)
    _, events = run_op("step_rows", orm, FakeLive())
    assert ["pk"] not in lookups  # no re-read by pk alone
    record = next(r for r in journal(events) if r.get("table") == "issues")
    assert set(record["fields"]) == set(backend.ROW_FIELDS)
    original = before[record["id"]]
    fields = backend.journal_values(record)
    assert fields == {c: original[c] for c in backend.ROW_FIELDS}


def run_main(orm, op, ack, convert=None):
    """Run the snippet tail's _main for one request, logging output lines and acks in orm.log."""
    ns = snippet_namespace()
    args = {"old": "old-slug", "new": "new-slug"}
    if op == "step_rows":
        args["workspace_id"] = "w1"

    def out(line):
        orm.log.append(("out", json.loads(line)))

    ns["_main"]({"op": op, "args": args}, lambda: orm, lambda a: convert, out, None, ack)
    return [entry[1] for entry in orm.log if entry[0] == "out"]


@pytest.mark.parametrize("op", ["step_slug", "step_rows"])
def test_each_journal_line_is_acked_before_any_write(op):
    orm = make_orm()
    if op == "step_rows":
        run_main(orm, "step_slug", lambda: orm.log.append(("ack",)))
        del orm.log[:]
    run_main(orm, op, lambda: orm.log.append(("ack",)), FakeLive())
    kinds = [
        "journal" if e[0] == "out" and e[1]["event"] == "journal" else e[0]
        for e in orm.log
        if e[0] in ("ack", "update") or (e[0] == "out" and e[1]["event"] == "journal")
    ]
    assert kinds.count("journal") == kinds.count("ack") > 0
    for index, kind in enumerate(kinds):
        if kind == "journal":
            assert kinds[index + 1] == "ack", kinds


def test_unacked_journal_line_writes_nothing(monkeypatch):
    # The host never acks (it died): stdin ends, the read fails, and the row is not written.
    orm = make_orm()
    before = orm.snapshot()
    ns = snippet_namespace()
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(b"")))
    out = []
    ns["_main"](
        {"op": "step_slug", "args": {"old": "old-slug", "new": "new-slug"}},
        lambda: orm,
        lambda a: None,
        out.append,
        None,
        ns["_read_ack"],
    )
    assert orm.snapshot() == before
    events = [json.loads(line) for line in out]
    assert events[-2] == {"event": "error", "reason": "write_failed", "detail": "JournalNotAcked"}
    run_main(orm, "step_slug", lambda: None)  # acked: the slug step commits
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(b"nope\n")))
    out.clear()
    ns["_main"](
        {"op": "step_rows", "args": {"old": "old-slug", "new": "new-slug", "workspace_id": "w1"}},
        lambda: orm,
        lambda a: FakeLive(),
        out.append,
        None,
        ns["_read_ack"],
    )
    events = [json.loads(line) for line in out]
    assert {"event": "error", "reason": "row_failed", "table": "issues", "id": "i1"}.items() <= (
        next(e for e in events if e["event"] == "error").items()
    )
    assert row(orm, "issue", "i1")["description_html"] == before["issue"][0]["description_html"]
