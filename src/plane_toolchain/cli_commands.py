"""Command handlers: each maps parsed arguments to existing Plane MCP tool calls."""

import argparse
import re
import sys
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

Call = Callable[[str, dict], Awaitable[Any]]
IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9]*-\d+")


class UsageError(Exception):
    """Invalid arguments; exit code 2."""


class ReadbackError(Exception):
    """A write was not confirmed by rereading; exit code 5."""


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


def read_body(namespace: argparse.Namespace, option: str) -> str:
    """Return text from --html or --file (- is stdin); exactly one is required."""
    html, path = namespace.html, namespace.file
    if (html is None) == (path is None):
        raise UsageError(f"Cannot read {option}")
    if html is not None:
        return html
    try:
        if path == "-":
            return sys.stdin.buffer.read().decode("utf-8")
        with open(path, "rb") as handle:
            return handle.read().decode("utf-8")
    except (OSError, UnicodeDecodeError, AttributeError):
        raise UsageError(f"Cannot read {option}") from None


def validate(namespace: argparse.Namespace) -> None:
    """Check arguments that need no connection; raise UsageError."""
    if getattr(namespace, "project", None) and not is_uuid(namespace.project):
        raise UsageError("--project must be a UUID")
    key = (namespace.group, namespace.action)
    if key in {("task", "show"), ("task", "update"), ("comment", "list"), ("comment", "add")}:
        task_reference(namespace.ref)
    if key == ("task", "update"):
        if getattr(namespace, "state", None) and not is_uuid(namespace.state):
            raise UsageError("--state must be a UUID")
        has_body = namespace.html is not None or namespace.file is not None
        if not has_body and not any(
            value is not None for value in (namespace.name, namespace.state, namespace.priority)
        ):
            raise UsageError("Nothing to update")
        namespace.body = read_body(namespace, "--html or --file") if has_body else None
    if key == ("comment", "add"):
        namespace.body = read_body(namespace, "--html or --file")


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


async def task_update(call: Call, project: str, namespace: argparse.Namespace) -> Any:
    project_id, workitem_id = await resolve_task(call, project, namespace.ref)
    sent: dict = {}
    for field in ("name", "state", "priority"):
        if getattr(namespace, field) is not None:
            sent[field] = getattr(namespace, field)
    if namespace.body is not None:
        sent["description_html"] = namespace.body
    if not sent:
        raise UsageError("Nothing to update")
    ids = {"project_id": project_id, "workitem_id": workitem_id}
    await call("workitem", {"action": "update", **ids, **sent})
    item = await call("workitem", {"action": "retrieve", **ids})
    for field, value in sent.items():
        if field == "description_html":
            # Plane may normalise HTML (e.g. wrap in <p>), so only require non-empty.
            if not item.get(field):
                raise ReadbackError
        elif item.get(field) != value:
            raise ReadbackError
    return item


async def comment_list(call: Call, project: str, namespace: argparse.Namespace) -> Any:
    project_id, workitem_id = await resolve_task(call, project, namespace.ref)
    return await call(
        "workitem_comment",
        {"action": "list", "project_id": project_id, "workitem_id": workitem_id},
    )


async def comment_add(call: Call, project: str, namespace: argparse.Namespace) -> Any:
    project_id, workitem_id = await resolve_task(call, project, namespace.ref)
    ids = {"project_id": project_id, "workitem_id": workitem_id}
    created = await call(
        "workitem_comment", {"action": "create", **ids, "comment_html": namespace.body}
    )
    listing = await call("workitem_comment", {"action": "list", **ids})
    rows = listing.get("results", []) if isinstance(listing, dict) else listing
    wanted = created.get("id") if isinstance(created, dict) else None
    if not wanted or not any(isinstance(row, dict) and row.get("id") == wanted for row in rows):
        raise ReadbackError
    return created


HANDLERS: dict[tuple[str, str], Callable[[Call, str, argparse.Namespace], Awaitable[Any]]] = {
    ("task", "list"): task_list,
    ("task", "show"): task_show,
    ("task", "update"): task_update,
    ("comment", "list"): comment_list,
    ("comment", "add"): comment_add,
}
