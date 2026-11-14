"""Bounded hosted MCP transport shared by task tooling and Actions."""

import asyncio
import json
import os
import time
from collections.abc import Mapping
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from plane_toolchain.settings import Settings


async def paginated(call, name: str, arguments: dict) -> list[dict]:
    """Follow explicit MCP cursors, rejecting incomplete or repeated pagination.

    Raises ValueError on a repeated or missing cursor and after 100 pages.
    """
    rows = []
    seen = set()
    cursor = ""
    for _ in range(100):
        data = await call(name, dict(arguments, cursor=cursor, per_page=100))
        if isinstance(data, list):
            return rows + data
        rows.extend(data["results"])
        if not data.get("next_page_results"):
            return rows
        cursor = data.get("next_cursor")
        if not cursor or cursor in seen:
            raise ValueError("Invalid MCP pagination")
        seen.add(cursor)
    raise ValueError("MCP pagination limit reached")


def paced(call, *, clock=time.monotonic, sleep=asyncio.sleep):
    """Space tool starts by one second without retrying uncertain remote writes.

    Tools may make multiple API requests. Leave capacity for their pagination and
    other clients sharing the key; do not change the server's rate limit.
    """
    lock = asyncio.Lock()
    next_start = 0.0

    async def wrapped(name, arguments):
        nonlocal next_start
        async with lock:
            delay = next_start - clock()
            if delay > 0:
                await sleep(delay)
            next_start = clock() + 1.0
            return await call(name, arguments)

    return wrapped


class ToolError(ValueError):
    """The code's own MCP tool failure; its message is fixed text, safe to print."""


def decode(result) -> Any:
    """Unwrap FastMCP union results without mistaking an API error for data."""
    if result.is_error:
        raise ToolError("Plane MCP tool failed; remote outcome must be checked")
    data = result.structured_content
    if data is None:
        blocks = [block.text for block in result.content if block.type == "text"]
        if len(blocks) != 1:
            raise ToolError("Unexpected MCP result")
        data = json.loads(blocks[0])
    elif isinstance(data, dict) and set(data) == {"result"}:
        data = data["result"]
    if not isinstance(data, (dict, list)) and data is not None:
        raise ToolError("Unexpected MCP result")
    return data


def configuration(env: Mapping[str, str] | None = None) -> Settings:
    """Validate connection values without exposing credentials."""
    settings = Settings.from_env(os.environ if env is None else env)
    return settings


@asynccontextmanager
async def connection(env: Mapping[str, str] | None = None):
    """Yield an MCP tool caller; never log transport bodies or authentication."""
    env = os.environ if env is None else env
    configuration(env)
    headers = {
        "Authorization": "Bearer " + env["PLANE_API_KEY"],
        "x-workspace-slug": env["PLANE_WORKSPACE"],
    }
    async with (
        httpx2.AsyncClient(headers=headers, timeout=60) as http,
        streamable_http_client(env["PLANE_MCP_URL"], http_client=http) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        await session.initialize()
        listing = await session.list_tools()
        names = {tool.name for tool in listing.tools}

        async def call(name: str, arguments: dict) -> Any:
            if name not in names:
                raise ToolError("Required MCP tool unavailable")
            result = await session.call_tool(name, arguments)
            return decode(result)

        yield paced(call)
