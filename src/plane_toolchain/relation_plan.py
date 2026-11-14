"""Bulk blocked-by relation plans: validate offline, check the remote, add what is missing.

A plan is JSON: a list of {"blocked": "ABC-12", "blocker": "ABC-7"} pairs, or an object
with that list under "pairs". The applier only ever adds `blocked_by` relations through
`ce_workitem_relation`. It never deletes a relation, even to break a cycle.
"""

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from plane_toolchain.cli_commands import IDENTIFIER, ReadbackError, UsageError, canonical

Call = Callable[[str, dict], Awaitable[Any]]
Plan = list[tuple[str, str]]
DEFAULT_PAUSE = 0.5


def load(path: str | Path) -> list[dict]:
    """Read the plan file and return its raw pair objects."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        raise UsageError("Cannot read plan file as JSON") from None
    if isinstance(data, dict):
        data = data.get("pairs")
    if not isinstance(data, list) or not data:
        raise UsageError("Plan must be a non-empty list of {blocked, blocker} pairs")
    return data


def find_cycle(edges: set[tuple[str, str]]) -> list[str] | None:
    """Return one cycle as a node list when the directed graph has one."""
    graph: dict[str, list[str]] = {}
    for blocked, blocker in sorted(edges):
        graph.setdefault(blocked, []).append(blocker)
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        stack.append(node)
        for nxt in graph.get(node, []):
            if state.get(nxt) == 1:
                return [*stack[stack.index(nxt) :], nxt]
            if nxt not in state and (found := visit(nxt)):
                return found
        stack.pop()
        state[node] = 2
        return None

    for node in list(graph):
        if node not in state and (found := visit(node)):
            return found
    return None


def refuse_cycle(edges: set[tuple[str, str]]) -> None:
    cycle = find_cycle(edges)
    if cycle:
        raise UsageError("Plan would create a cycle: " + " -> ".join(cycle))


def validate(raw: list[dict], known: set[str] | None = None) -> Plan:
    """Normalise to upper-case identifiers, drop duplicates, refuse bad pairs and cycles.

    `known`, when given, is the set of identifiers allowed in the plan.
    """
    plan: Plan = []
    for item in raw:
        if not isinstance(item, dict):
            raise UsageError("Each plan entry must be an object with blocked and blocker")
        pair = []
        for key in ("blocked", "blocker"):
            value = item.get(key)
            if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
                raise UsageError(f"Plan entry needs an identifier such as ABC-12 for {key}")
            pair.append(value.upper())
        blocked, blocker = pair
        if blocked == blocker:
            raise UsageError(f"{blocked} cannot block itself")
        if known is not None and not {blocked, blocker} <= known:
            unknown = sorted({blocked, blocker} - known)
            raise UsageError("Plan names unknown items: " + ", ".join(unknown))
        if (blocked, blocker) not in plan:
            plan.append((blocked, blocker))
    refuse_cycle(set(plan))
    return plan


def rows(listing: Any, kind: str) -> list[str]:
    found = []
    if isinstance(listing, dict):
        for row in (listing.get("dependencies") or {}).get(kind) or []:
            if isinstance(row, dict) and row.get("id"):
                found.append(canonical(row["id"]))
    return found


async def inspect_remote(
    call: Call, project: str, plan: Plan
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Read-only checks. Return ({blocked_id: [missing blocker ids]}, {id: identifier}).

    Every item must exist and sit in `project`. Existing relations among plan items
    are read and combined with the plan for a cycle check. Any failed read propagates,
    so the run stops before the first write. Cycles that pass only through items
    outside the plan are not seen.
    """
    names: dict[str, str] = {}
    for ident in dict.fromkeys(i for pair in plan for i in pair):
        found = await call(
            "workitem", {"action": "retrieve_by_identifier", "workitem_identifier": ident}
        )
        if not isinstance(found, dict) or not found.get("id") or not found.get("project"):
            raise UsageError(f"Plan names unknown items: {ident}")
        if canonical(found["project"]) != canonical(project):
            raise UsageError(f"Relation tasks must be in one project: {ident} is not")
        names[canonical(found["id"])] = ident
    ids = {ident: uuid for uuid, ident in names.items()}
    existing: dict[str, set[str]] = {uuid: set() for uuid in names}
    edges: set[tuple[str, str]] = set()
    for uuid in names:
        listing = await call(
            "ce_workitem_relation",
            {"action": "list", "project_id": canonical(project), "workitem_id": uuid},
        )
        for blocker in rows(listing, "blocked_by"):
            existing[uuid].add(blocker)
            edges.add((uuid, blocker))
        for blocked in rows(listing, "blocking"):
            edges.add((blocked, uuid))
    edges |= {(ids[blocked], ids[blocker]) for blocked, blocker in plan}
    refuse_cycle(edges)
    missing: dict[str, list[str]] = {}
    for blocked, blocker in plan:
        if ids[blocker] not in existing[ids[blocked]]:
            missing.setdefault(ids[blocked], []).append(ids[blocker])
    return missing, names


async def validate_remote(call: Call, project: str, plan: Plan) -> dict[str, list[str]]:
    """Return the missing blocked_by links, {blocked_id: [blocker_id, ...]}."""
    return (await inspect_remote(call, project, plan))[0]


async def apply(
    call: Call,
    project: str,
    missing: dict[str, list[str]],
    pause: float = DEFAULT_PAUSE,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> int:
    """Add the missing links, one write per blocked item, rereading each. Return links added.

    Waits `pause` seconds between items. A reread that lacks a link raises ReadbackError.
    A rerun is safe: only links still missing are written.
    """
    added = 0
    for index, (blocked, blockers) in enumerate(missing.items()):
        if index and pause:
            waited = sleep(pause)
            if inspect.isawaitable(waited):
                await waited
        ids = {"project_id": canonical(project), "workitem_id": blocked}
        await call(
            "ce_workitem_relation",
            {"action": "create", **ids, "workitem_ids": blockers, "relation_type": "blocked_by"},
        )
        listing = await call("ce_workitem_relation", {"action": "list", **ids})
        if not set(blockers) <= set(rows(listing, "blocked_by")):
            raise ReadbackError
        added += len(blockers)
    return added
