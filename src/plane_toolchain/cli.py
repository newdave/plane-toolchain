"""Plane command-line interface over the hosted MCP server."""

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from plane_toolchain import cli_commands, session
from plane_toolchain.cli_commands import ReadbackError, UsageError
from plane_toolchain.connection import ToolError, connection
from plane_toolchain.session import ROOT
from plane_toolchain.settings import Settings

__all__ = ["ReadbackError", "UsageError", "environment", "execute", "main", "parser"]

FAILED = "Plane MCP request failed; remote outcome must be checked"


WRITES = {
    ("task", "update"),
    ("comment", "add"),
    ("page", "create"),
    ("relation", "add"),
    ("relation", "plan"),
}
NOISY_LOGGERS = ("mcp", "httpx", "httpx2", "httpcore")
RELATION_TYPES = [
    "blocking",
    "blocked_by",
    "start_before",
    "start_after",
    "finish_before",
    "finish_after",
]
PRIORITIES = ["urgent", "high", "medium", "low", "none"]


def body(sub: argparse.ArgumentParser) -> None:
    group = sub.add_mutually_exclusive_group()
    group.add_argument("--html", default=None, help="HTML text")
    group.add_argument("--file", default=None, help="file with HTML text, - for stdin")


def parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", default=argparse.SUPPRESS, help="project UUID")
    root = argparse.ArgumentParser(prog="plane", description="Plane tasks over the MCP server")
    root.add_argument("--project", default=None, help="project UUID (default: PLANE_PROJECT)")
    groups = root.add_subparsers(dest="group", required=True)

    # Add a group or action here and its handler to cli_commands.HANDLERS.
    task = groups.add_parser("task", help="tasks").add_subparsers(dest="action", required=True)
    listing = task.add_parser("list", parents=[common], help="list tasks")
    listing.add_argument("--per-page", type=int, default=None)
    listing.add_argument("--cursor", default=None)
    show = task.add_parser("show", parents=[common], help="show one task")
    show.add_argument("ref", help="task UUID or identifier such as PTC-12")

    update = task.add_parser("update", parents=[common], help="update a task")
    update.add_argument("ref", help="task UUID or identifier such as PTC-12")
    update.add_argument("--name", default=None)
    update.add_argument("--state", default=None, help="state UUID")
    update.add_argument("--priority", choices=PRIORITIES, default=None)
    body(update)

    comment = groups.add_parser("comment", help="comments").add_subparsers(
        dest="action", required=True
    )
    comments = comment.add_parser("list", parents=[common], help="list comments")
    comments.add_argument("ref", help="task UUID or identifier such as PTC-12")
    comments.add_argument("--per-page", type=int, default=None)
    comments.add_argument("--cursor", default=None)
    added = comment.add_parser("add", parents=[common], help="add a comment")
    added.add_argument("ref", help="task UUID or identifier such as PTC-12")
    body(added)

    page = groups.add_parser("page", help="project Pages").add_subparsers(
        dest="action", required=True
    )
    page.add_parser("list", parents=[common], help="list Pages")
    page_show = page.add_parser("show", parents=[common], help="show one Page")
    page_show.add_argument("page_id", help="Page UUID")
    page_new = page.add_parser("create", parents=[common], help="create a Page")
    page_new.add_argument("--name", required=True)
    page_new.add_argument(
        "--public", action="store_true", help="public in project (default private)"
    )
    body(page_new)

    relation = groups.add_parser("relation", help="task relations").add_subparsers(
        dest="action", required=True
    )
    relation_list = relation.add_parser("list", parents=[common], help="list relations")
    relation_list.add_argument("ref", help="task UUID or identifier such as PTC-12")
    relation_add = relation.add_parser("add", parents=[common], help="add relations")
    relation_add.add_argument("ref", help="task UUID or identifier such as PTC-12")
    relation_add.add_argument("others", nargs="+", metavar="other_ref", help="related tasks")
    relation_add.add_argument("--type", choices=RELATION_TYPES, default="blocked_by")
    relation_plan = relation.add_parser(
        "plan", parents=[common], help="add blocked_by relations from a JSON plan (dry run)"
    )
    relation_plan.add_argument("file", help='JSON list of {"blocked": "ABC-1", "blocker": "ABC-2"}')
    relation_plan.add_argument("--apply", action="store_true", help="write (default: dry run)")
    relation_plan.add_argument(
        "--pause", type=float, default=0.5, help="seconds between writes (default 0.5)"
    )
    return root


def environment(env: Mapping[str, str], root: Path) -> dict[str, str]:
    """Merge file fallbacks under the environment; the key comes only from env."""
    merged = dict(env)
    merged.update(session.connection(root, env))
    if "PLANE_ENABLED" not in env:
        state = session.switch(root, env)
        merged["PLANE_ENABLED"] = {"enabled": "true", "disabled": "false"}.get(state, "invalid")
    return merged


def leaves(error: BaseException) -> list[BaseException]:
    """Flatten exception groups, which anyio task groups wrap around session errors."""
    if isinstance(error, BaseExceptionGroup):
        return [leaf for inner in error.exceptions for leaf in leaves(inner)]
    return [error]


def classify(error: BaseException) -> tuple[int, str | None]:
    """Map a raised error, possibly a group, to (exit code, message)."""
    found = leaves(error)
    for kind, code, message in (
        (ReadbackError, 5, "write not confirmed by readback; check the remote state"),
        (UsageError, 2, None),
        (ToolError, 4, None),
    ):
        hits = [leaf for leaf in found if isinstance(leaf, kind)]
        if hits:
            return code, message or str(hits[0])
    if any(isinstance(leaf, KeyboardInterrupt) for leaf in found):
        return 130, None
    return 4, FAILED


async def execute(namespace: argparse.Namespace, call, project: str) -> Any:
    return await cli_commands.HANDLERS[(namespace.group, namespace.action)](
        call, project, namespace
    )


async def run(namespace: argparse.Namespace, merged: dict[str, str], project: str, connect):
    async with connect(merged) as call:
        return await execute(namespace, call, project)


@contextmanager
def quiet_logging() -> Iterator[None]:
    """Keep library log records (tracebacks, response bodies) off stderr; restore on exit."""
    last_resort = logging.lastResort
    loggers = [logging.getLogger(name) for name in NOISY_LOGGERS]
    levels = [logger.level for logger in loggers]
    logging.lastResort = None
    for logger in loggers:
        logger.setLevel(logging.CRITICAL + 1)
    try:
        yield
    finally:
        logging.lastResort = last_resort
        for logger, level in zip(loggers, levels, strict=True):
            logger.setLevel(level)


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    root: Path = ROOT,
    connect=connection,
    out=sys.stdout,
    err=sys.stderr,
) -> int:
    with quiet_logging():
        return run_cli(argv, env=env, root=root, connect=connect, out=out, err=err)


def run_cli(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    root: Path = ROOT,
    connect=connection,
    out=sys.stdout,
    err=sys.stderr,
) -> int:
    env = os.environ if env is None else env

    def fail(code: int, message: str) -> int:
        err.write(f"plane: {message}\n")
        return code

    try:
        namespace = parser().parse_args(argv)
    except SystemExit as stop:
        return stop.code if isinstance(stop.code, int) else 2
    try:
        cli_commands.validate(namespace)
        merged = environment(env, root)
        if namespace.project:
            merged["PLANE_PROJECT"] = namespace.project
        if merged["PLANE_ENABLED"] not in {"true", "false"}:
            return fail(3, "Invalid PLANE_ENABLED")
        try:
            settings = Settings.from_env(merged)
        except ValueError as error:
            return fail(3, str(error))
        data = asyncio.run(run(namespace, merged, settings.project, connect))
    except SystemExit:
        raise
    except BaseException as error:  # noqa: BLE001 - map everything; never print a traceback
        code, message = classify(error)
        if code == 130:
            if (namespace.group, namespace.action) in WRITES:
                fail(130, "interrupted; remote outcome must be checked")
            return 130
        return fail(code, message or FAILED)
    out.write(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
