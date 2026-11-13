"""Command handlers: each maps parsed arguments to existing Plane MCP tool calls."""

import argparse
import re
import sys
from html import unescape
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

Call = Callable[[str, dict], Awaitable[Any]]
PAGE_SIZE = 100
MAX_PAGES = 50
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


def canonical(value: str) -> str:
    return str(UUID(value))


def task_reference(ref: str) -> None:
    """Raise UsageError unless ref is a UUID or an identifier such as PTC-12."""
    if not (is_uuid(ref) or IDENTIFIER.fullmatch(ref)):
        raise UsageError("Task must be a UUID or an identifier such as PTC-12")


async def resolve_task(
    call: Call, project: str, ref: str, explicit: str | None = None
) -> tuple[str, str]:
    """Return (project_id, workitem_id) for a UUID or identifier reference.

    An identifier carries its own project. If --project was given (explicit) and
    names a different project, refuse instead of silently picking one.
    """
    task_reference(ref)
    if is_uuid(ref):
        return canonical(project), canonical(ref)
    found = await call(
        "workitem",
        {"action": "retrieve_by_identifier", "workitem_identifier": ref.upper()},
    )
    found_project = canonical(found["project"])
    if explicit and canonical(explicit) != found_project:
        raise UsageError("Task is not in the --project given")
    return found_project, canonical(found["id"])


def body_text(html: Any) -> str:
    """Return the visible text of an HTML fragment with whitespace collapsed."""
    text = re.sub(r"<[^>]*>", " ", str(html or ""))
    return " ".join(unescape(text).split())


def read_body(namespace: argparse.Namespace, option: str) -> str:
    """Return text from --html or --file (- is stdin); exactly one is required."""
    html, path = namespace.html, namespace.file
    if html is None and path is None:
        raise UsageError(f"{option} is required")
    if html is not None and path is not None:
        raise UsageError(f"Cannot read {option}")
    if html is not None:
        text = html
    else:
        try:
            if path == "-":
                text = sys.stdin.buffer.read().decode("utf-8")
            else:
                with open(path, "rb") as handle:
                    text = handle.read().decode("utf-8")
        except (OSError, UnicodeDecodeError, AttributeError):
            raise UsageError(f"Cannot read {option}") from None
    if not text.strip():
        raise UsageError("--html or --file must not be empty")
    return text


def validate(namespace: argparse.Namespace) -> None:
    """Check arguments that need no connection; raise UsageError."""
    if getattr(namespace, "project", None) and not is_uuid(namespace.project):
        raise UsageError("--project must be a UUID")
    if getattr(namespace, "project", None):
        namespace.project = canonical(namespace.project)
    key = (namespace.group, namespace.action)
    if key in {("task", "show"), ("task", "update"), ("comment", "list"), ("comment", "add")}:
        task_reference(namespace.ref)
    if key == ("task", "update"):
        if getattr(namespace, "state", None) and not is_uuid(namespace.state):
            raise UsageError("--state must be a UUID")
        if getattr(namespace, "state", None):
            namespace.state = canonical(namespace.state)
        has_body = namespace.html is not None or namespace.file is not None
        if not has_body and not any(
            value is not None for value in (namespace.name, namespace.state, namespace.priority)
        ):
            raise UsageError("Nothing to update")
        namespace.body = read_body(namespace, "--html or --file") if has_body else None
    if key == ("comment", "add"):
        namespace.body = read_body(namespace, "--html or --file")
    if key == ("page", "show") and not is_uuid(namespace.page_id):
        raise UsageError("Page must be a UUID")
    if key == ("page", "show"):
        namespace.page_id = canonical(namespace.page_id)
    if key == ("page", "create"):
        if not namespace.name.strip():
            raise UsageError("--name must not be empty")
        namespace.body = read_body(namespace, "--html or --file")
    if key == ("relation", "list"):
        task_reference(namespace.ref)
    if key == ("relation", "add"):
        refs = [namespace.ref, *namespace.others]
        for ref in refs:
            task_reference(ref)
        if len({ref.upper() for ref in refs}) != len(refs):
            raise UsageError("Relation tasks must be distinct")


async def task_list(call: Call, project: str, namespace: argparse.Namespace) -> Any:
    arguments: dict = {"action": "list", "project_id": project}
    if namespace.per_page is not None:
        arguments["per_page"] = namespace.per_page
    if namespace.cursor is not None:
        arguments["cursor"] = namespace.cursor
    return await call("workitem", arguments)


async def task_show(call: Call, project: str, namespace: argparse.Namespace) -> Any:
    project_id, workitem_id = await resolve_task(
        call, project, namespace.ref, getattr(namespace, "project", None)
    )
    return await call(
        "workitem",
        {"action": "retrieve", "project_id": project_id, "workitem_id": workitem_id},
    )


async def task_update(call: Call, project: str, namespace: argparse.Namespace) -> Any:
    project_id, workitem_id = await resolve_task(
        call, project, namespace.ref, getattr(namespace, "project", None)
    )
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
            # Plane may normalise HTML (e.g. wrap in <p>), so compare the text.
            if body_text(item.get(field)) != body_text(value):
                raise ReadbackError
        elif field == "priority" and value == "none":
            # Plane stores "no priority" as null.
            if item.get(field) not in (None, "none"):
                raise ReadbackError
        elif field == "name":
            if str(item.get(field) or "").strip() != value.strip():
                raise ReadbackError
        elif item.get(field) != value:
            raise ReadbackError
    return item


async def comment_list(call: Call, project: str, namespace: argparse.Namespace) -> Any:
    project_id, workitem_id = await resolve_task(
        call, project, namespace.ref, getattr(namespace, "project", None)
    )
    arguments: dict = {"action": "list", "project_id": project_id, "workitem_id": workitem_id}
    if namespace.per_page is not None:
        arguments["per_page"] = namespace.per_page
    if namespace.cursor is not None:
        arguments["cursor"] = namespace.cursor
    return await call("workitem_comment", arguments)


async def comment_add(call: Call, project: str, namespace: argparse.Namespace) -> Any:
    project_id, workitem_id = await resolve_task(
        call, project, namespace.ref, getattr(namespace, "project", None)
    )
    ids = {"project_id": project_id, "workitem_id": workitem_id}
    created = await call(
        "workitem_comment", {"action": "create", **ids, "comment_html": namespace.body}
    )
    wanted = created.get("id") if isinstance(created, dict) else None
    if not wanted:
        raise ReadbackError
    cursor = None
    for _ in range(MAX_PAGES):
        arguments = {"action": "list", **ids, "per_page": PAGE_SIZE}
        if cursor:
            arguments["cursor"] = cursor
        listing = await call("workitem_comment", arguments)
        rows = listing.get("results", []) if isinstance(listing, dict) else listing
        if any(isinstance(row, dict) and row.get("id") == wanted for row in rows):
            return created
        if not (isinstance(listing, dict) and listing.get("next_page_results")):
            break
        cursor = listing.get("next_cursor")
        if not cursor:
            break
    raise ReadbackError


async def page_list(call: Call, project: str, namespace: argparse.Namespace) -> Any:
    return await call("ce_page", {"action": "list", "project_id": project})


async def page_show(call: Call, project: str, namespace: argparse.Namespace) -> Any:
    return await call(
        "ce_page", {"action": "retrieve", "project_id": project, "page_id": namespace.page_id}
    )


async def page_create(call: Call, project: str, namespace: argparse.Namespace) -> Any:
    access = 0 if namespace.public else 1
    created = await call(
        "ce_page",
        {
            "action": "create",
            "project_id": project,
            "name": namespace.name,
            "description_html": namespace.body,
            "access": access,
        },
    )
    page_id = created.get("id") if isinstance(created, dict) else None
    if not page_id:
        raise ReadbackError
    page = await call("ce_page", {"action": "retrieve", "project_id": project, "page_id": page_id})
    if not isinstance(page, dict) or str(page.get("name") or "").strip() != namespace.name.strip():
        raise ReadbackError
    if page.get("access") != access or not body_text(page.get("description_html")):
        raise ReadbackError
    return page


async def relation_list(call: Call, project: str, namespace: argparse.Namespace) -> Any:
    project_id, workitem_id = await resolve_task(
        call, project, namespace.ref, getattr(namespace, "project", None)
    )
    return await call(
        "ce_workitem_relation",
        {"action": "list", "project_id": project_id, "workitem_id": workitem_id},
    )


async def relation_add(call: Call, project: str, namespace: argparse.Namespace) -> Any:
    project_id, workitem_id = await resolve_task(
        call, project, namespace.ref, getattr(namespace, "project", None)
    )
    resolved = [
        await resolve_task(call, project, ref, getattr(namespace, "project", None))
        for ref in namespace.others
    ]
    targets = [target for _, target in resolved]
    if workitem_id in targets:
        raise UsageError("A task cannot be related to itself")
    if len(set(targets)) != len(targets):
        raise UsageError("Relation tasks must be distinct")
    if any(other != project_id for other, _ in resolved):
        raise UsageError("Relation tasks must be in one project")
    ids = {"project_id": project_id, "workitem_id": workitem_id}
    await call(
        "ce_workitem_relation",
        {
            "action": "create",
            **ids,
            "workitem_ids": targets,
            "relation_type": namespace.type,
        },
    )
    listing = await call("ce_workitem_relation", {"action": "list", **ids})
    rows = []
    if isinstance(listing, dict):
        rows = (listing.get("dependencies") or {}).get(namespace.type) or []
    found = {row.get("id") for row in rows if isinstance(row, dict)}
    if not set(targets) <= found:
        raise ReadbackError
    return listing


HANDLERS: dict[tuple[str, str], Callable[[Call, str, argparse.Namespace], Awaitable[Any]]] = {
    ("task", "list"): task_list,
    ("task", "show"): task_show,
    ("task", "update"): task_update,
    ("comment", "list"): comment_list,
    ("comment", "add"): comment_add,
    ("page", "list"): page_list,
    ("page", "show"): page_show,
    ("page", "create"): page_create,
    ("relation", "list"): relation_list,
    ("relation", "add"): relation_add,
}
