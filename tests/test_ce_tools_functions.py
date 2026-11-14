"""ce_tools tool bodies are module-level functions driven by a fake client (no plane_mcp)."""

import asyncio
from types import SimpleNamespace

import pytest

from plane_toolchain import ce_tools

A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"
C = "33333333-3333-4333-8333-333333333333"


class FakeRelationModel:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _relation_client(log, reply=None):
    def record(name):
        return lambda *args: log.append((name, *args))

    def get(path):
        log.append(("get", path))
        return reply or {}

    relations = SimpleNamespace(create=record("create"), _get=get)
    work_items = SimpleNamespace(retrieve=record("retrieve"), relations=relations)
    return SimpleNamespace(work_items=work_items)


def _relate(client, action, **kw):
    args = dict(project_id=A, workitem_id=B, workitem_ids=None, relation_type="blocked_by")
    args.update(kw)
    return asyncio.run(
        ce_tools.run_relation_action(client, "ws", FakeRelationModel, action, **args)
    )


def test_relation_list_definitions_makes_no_call():
    log = []
    result = _relate(_relation_client(log), "list_definitions", project_id="", workitem_id="")
    assert result["custom_definitions"] == []
    assert "blocking" in result["built_in_dependencies"]
    assert log == []


def test_relation_list_checks_access_first_then_reads_relations():
    log = []
    client = _relation_client(log, {"blocked_by": [{"issue_id": C, "project_id": A}]})
    result = _relate(client, "list", project_id=A.upper())
    assert log == [("retrieve", "ws", A, B), ("get", f"ws/projects/{A}/work-items/{B}/relations")]
    assert result["dependencies"]["blocked_by"] == [{"id": C, "project_id": A}]


def test_relation_create_dedupes_checks_each_target_and_uses_the_model():
    log = []
    _relate(_relation_client(log), "create", workitem_ids=[C, C.upper()], relation_type="blocking")
    assert [entry[0] for entry in log] == ["retrieve", "retrieve", "create", "get"]
    assert log[1] == ("retrieve", "ws", A, C)
    _, workspace, project, item, model = log[2]
    assert (workspace, project, item) == ("ws", A, B)
    assert model.kwargs == {"relation_type": "blocking", "issues": [C]}


@pytest.mark.parametrize("targets", [None, [], [B], [str(i) for i in range(101)]])
def test_relation_create_refuses_bad_targets_before_writing(targets):
    log = []
    with pytest.raises(ValueError):
        _relate(_relation_client(log), "create", workitem_ids=targets)
    assert all(entry[0] != "create" for entry in log)


def _intake_client(statuses):
    log = []
    replies = iter(statuses)

    def get(path):
        log.append(("get", path))
        return {"status": next(replies)}

    intake = SimpleNamespace(_get=get, _patch=lambda path, body: log.append(("patch", path, body)))
    return SimpleNamespace(intake=intake), log


def test_run_intake_action_decides_and_rejects_other_actions():
    client, log = _intake_client([None, 1])
    result = asyncio.run(ce_tools.run_intake_action(client, "ws", "decide", A, B, 1, "", ""))
    assert result == {"status": 1}
    assert [entry[0] for entry in log] == ["get", "patch", "get"]
    assert log[1][2] == {"status": 1}
    with pytest.raises(ValueError, match="decide only"):
        asyncio.run(ce_tools.run_intake_action(client, "ws", "reject", A, B, 1, "", ""))
    client, _ = _intake_client([None, None])
    with pytest.raises(RuntimeError, match="not changed"):
        asyncio.run(ce_tools.run_intake_action(client, "ws", "decide", A, B, 1, "", ""))


def _page_client(log, patch_reply=None, reread_html="<p>new</p>"):
    def record(name, reply):
        def call(*args):
            log.append((name, *args))
            return reply

        return call

    pages = SimpleNamespace(
        _get=record("get", {"description_html": reread_html}),
        _post=record("post", {"id": "p"}),
        _patch=record("patch", patch_reply or {"id": "p"}),
    )
    return SimpleNamespace(projects=SimpleNamespace(retrieve=record("project", {})), pages=pages)


def _page(client, action, **kw):
    args = dict(
        page_id="",
        name="N",
        description_html="<p>new</p>",
        access=1,
        expected_updated_at="2026-11-13T01:02:03Z",
        force=True,
    )
    args.update(kw)
    return asyncio.run(ce_tools.run_ce_page(client, "ws", action, A, **args))


def test_ce_page_create_and_retrieve_paths():
    log = []
    client = _page_client(log)
    assert _page(client, "create") == {"id": "p"}
    body = {"name": "N", "description_html": "<p>new</p>", "access": 1}
    assert log[1] == ("post", f"ws/projects/{A}/ce-pages", body)
    _page(client, "retrieve", page_id=B)
    assert log[-1] == ("get", f"ws/projects/{A}/ce-pages/{B}")


def test_ce_page_update_sends_precondition_body_and_confirms_late_store(monkeypatch):
    import hashlib

    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(ce_tools.asyncio, "sleep", fake_sleep)
    digest = hashlib.sha256(b"<p>new</p>").hexdigest()
    reply = {"id": "p", "late_store_check": {"seconds": 12, "html_sha256": digest}}
    log = []
    result = _page(_page_client(log, reply), "update", page_id=B)
    assert log[1] == (
        "patch",
        f"ws/projects/{A}/ce-pages/{B}",
        {
            "description_html": "<p>new</p>",
            "expected_updated_at": "2026-11-13T01:02:03Z",
            "force": True,
        },
    )
    assert slept == [12]
    assert result["late_store"] == "unchanged"
    # Live replaced the update after the write: the tool fails naming the page.
    with pytest.raises(RuntimeError, match="overwritten by an open editor"):
        _page(_page_client([], reply, reread_html="<p>old</p>"), "update", page_id=B)


def test_ce_page_update_refuses_bad_requests_before_any_write():
    log = []
    client = _page_client(log)
    with pytest.raises(ValueError, match="expected_updated_at"):
        _page(client, "update", page_id=B, expected_updated_at="")
    with pytest.raises(ValueError, match="non-empty"):
        _page(client, "update", page_id=B, description_html=" ")
    with pytest.raises(ValueError):
        _page(client, "update", page_id="")
    with pytest.raises(ValueError, match="list, retrieve, create and update only"):
        _page(client, "delete")
    # Body checks run after the project access probe, as before; no page write is sent.
    assert {entry[0] for entry in log} == {"project"}


def test_sdk_calls_all_go_through_the_one_thread_helper(monkeypatch):
    calls = []

    async def spy(function, *args):
        calls.append(getattr(function, "__name__", "call"))
        return await asyncio.to_thread(function, *args)

    monkeypatch.setattr(ce_tools, "sdk_call", spy)
    _page(_page_client([]), "retrieve", page_id=B)
    _relate(_relation_client([]), "list")
    client, _ = _intake_client([None, 1])
    asyncio.run(ce_tools.run_intake_action(client, "ws", "decide", A, B, 1, "", ""))
    assert len(calls) == 4  # project + page get, relations, decide_intake
    assert "decide_intake" in calls and "_relations" in calls


def test_to_thread_is_called_in_one_place_only():
    import inspect

    text = inspect.getsource(ce_tools)
    assert text.count("asyncio.to_thread(") == 1


def test_tool_surface_is_unchanged_from_the_pre_refactor_registry():
    # Golden file captured from main before PTC-111: names, descriptions as the MCP client
    # sees them, argument schemas and return annotations of every registered tool.
    import json
    from pathlib import Path

    from support_ce_tools_surface import capture

    golden = json.loads((Path(__file__).parent / "fixtures" / "ce_tools_surface.json").read_text())
    assert capture(ce_tools.main) == golden
    assert sorted(golden) == ["ce_intake", "ce_page", "ce_workitem_relation"]


def test_ce_tools_imports_without_plane_mcp_and_stays_self_contained():
    import ast
    import sys
    from pathlib import Path

    assert "plane_mcp" not in sys.modules
    tree = ast.parse(Path(ce_tools.__file__).read_text())
    top = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            top.add(node.module.split(".")[0])
    assert top == {"asyncio", "hashlib", "os", "re", "typing", "uuid"}


def test_wrappers_resolve_the_client_context_through_sdk_call(monkeypatch):
    from support_ce_tools_surface import register

    resolved = []
    client = _page_client([])

    def context():
        resolved.append("context")
        return client, "ws"

    calls = []

    async def spy(function, *args):
        calls.append(function)
        return await asyncio.to_thread(function, *args)

    monkeypatch.setattr(ce_tools, "sdk_call", spy)
    tools = register(ce_tools.main, context).tools
    assert asyncio.run(tools["ce_page"][1](action="retrieve", project_id=A, page_id=B))
    assert context in calls and resolved == ["context"]
    calls.clear()
    with pytest.raises(ValueError):
        asyncio.run(tools["ce_intake"][1](action="nope", project_id=A, workitem_id=B, status=1))
    assert context in calls
    calls.clear()
    asyncio.run(tools["ce_workitem_relation"][1](action="list_definitions"))
    assert context in calls
