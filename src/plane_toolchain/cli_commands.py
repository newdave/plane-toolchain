"""Command handlers: each maps parsed arguments to existing Plane MCP tool calls."""

import argparse
import re
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

Call = Callable[[str, dict], Awaitable[Any]]
IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9]*-\d+")


class UsageError(Exception):
    """Invalid arguments; exit code 2."""


def is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def task_reference(ref: str) -> None:
    """Raise UsageError unless ref is a UUID or an identifier such as PTC-12."""
    if not (is_uuid(ref) or IDENTIFIER.fullmatch(ref)):
        raise UsageError("Task must be a UUID or an identifier such as PTC-12")


async def resolve_task(call: Call, project: str, ref: str) -> tuple[str, str]:
    """Return (project_id, workitem_id) for a UUID or identifier reference."""
    task_reference(ref)
    if is_uuid(ref):
        return project, ref
    found = await call(
        "workitem",
        {"action": "retrieve_by_identifier", "workitem_identifier": ref.upper()},
    )
    return found["project"], found["id"]


def validate(namespace: argparse.Namespace) -> None:
    """Check arguments that need no connection; raise UsageError."""
    if getattr(namespace, "project", None) and not is_uuid(namespace.project):
        raise UsageError("--project must be a UUID")
    if (namespace.group, namespace.action) == ("task", "show"):
        task_reference(namespace.ref)


async def task_list(call: Call, project: str, namespace: argparse.Namespace) -> Any:
    arguments: dict = {"action": "list", "project_id": project}
    if namespace.per_page is not None:
        arguments["per_page"] = namespace.per_page
    if namespace.cursor is not None:
        arguments["cursor"] = namespace.cursor
    return await call("workitem", arguments)


async def task_show(call: Call, project: str, namespace: argparse.Namespace) -> Any:
    project_id, workitem_id = await resolve_task(call, project, namespace.ref)
    return await call(
        "workitem",
        {"action": "retrieve", "project_id": project_id, "workitem_id": workitem_id},
    )


HANDLERS: dict[tuple[str, str], Callable[[Call, str, argparse.Namespace], Awaitable[Any]]] = {
    ("task", "list"): task_list,
    ("task", "show"): task_show,
}
