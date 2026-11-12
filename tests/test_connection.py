"""Contracts for hosted MCP's union wrapper and pacing."""

import asyncio
from types import SimpleNamespace

import pytest
from plane_toolchain.connection import decode, paced


def test_pacing_serialises_calls_and_does_not_retry_uncertain_writes():
    now = [0.0]
    starts = []

    async def sleep(delay):
        now[0] += delay

    async def call(name, args):
        starts.append(now[0])
        if args.get("fail"):
            raise ValueError("uncertain")
        return name

    async def run():
        wrapped = paced(call, clock=lambda: now[0], sleep=sleep)
        assert await asyncio.gather(wrapped("one", {}), wrapped("two", {})) == ["one", "two"]
        with pytest.raises(ValueError, match="uncertain"):
            await wrapped("write", {"fail": True})

    asyncio.run(run())
    assert starts == [0, 1, 2]


@pytest.mark.parametrize("data", [{"id": "project"}, [{"id": "task"}], None])
def test_union_wrapper(data):
    assert decode(SimpleNamespace(is_error=False, structured_content={"result": data})) == data


def test_error_result_is_never_treated_as_data():
    with pytest.raises(ValueError):
        decode(SimpleNamespace(is_error=True, structured_content={"result": "error"}))
    with pytest.raises(ValueError):
        decode(SimpleNamespace(is_error=False, structured_content={"result": "API error"}))
