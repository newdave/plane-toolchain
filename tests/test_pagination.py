"""Contracts for the shared MCP cursor pagination helper."""

import asyncio

import pytest
from plane_toolchain import github_evidence
from plane_toolchain.connection import paginated


def run(call, arguments=None):
    return asyncio.run(paginated(call, "workitem", arguments or {"action": "list"}))


def test_follows_cursors_across_pages():
    seen = []

    async def call(name, arguments):
        seen.append(arguments)
        if not arguments["cursor"]:
            return {"results": [{"id": 1}], "next_page_results": True, "next_cursor": "a"}
        return {"results": [{"id": 2}], "next_page_results": False}

    assert run(call) == [{"id": 1}, {"id": 2}]
    assert [s["cursor"] for s in seen] == ["", "a"]
    assert all(s["per_page"] == 100 and s["action"] == "list" for s in seen)


def test_rejects_repeated_cursor():
    async def call(name, arguments):
        return {"results": [{"id": 1}], "next_page_results": True, "next_cursor": "a"}

    with pytest.raises(ValueError, match="Invalid MCP pagination"):
        run(call)


def test_rejects_missing_cursor():
    async def call(name, arguments):
        return {"results": [], "next_page_results": True}

    with pytest.raises(ValueError, match="Invalid MCP pagination"):
        run(call)


def test_stops_at_page_limit():
    count = 0

    async def call(name, arguments):
        nonlocal count
        count += 1
        return {"results": [], "next_page_results": True, "next_cursor": f"c{count}"}

    with pytest.raises(ValueError, match="limit"):
        run(call)
    assert count == 100


def test_accepts_plain_list():
    async def call(name, arguments):
        return [{"id": 1}]

    assert run(call) == [{"id": 1}]


def test_github_evidence_reexports_shared_helper():
    assert github_evidence.paginated is paginated
