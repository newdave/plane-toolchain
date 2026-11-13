"""Plane command-line interface over the hosted MCP server."""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from plane_toolchain import cli_commands, session
from plane_toolchain.cli_commands import UsageError
from plane_toolchain.connection import connection
from plane_toolchain.session import ROOT
from plane_toolchain.settings import Settings

__all__ = ["ReadbackError", "UsageError", "environment", "execute", "main", "parser"]

FAILED = "Plane MCP request failed; remote outcome must be checked"


class ReadbackError(Exception):
    """A write was not confirmed by rereading; exit code 5."""


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
    return root


def environment(env: Mapping[str, str], root: Path) -> dict[str, str]:
    """Merge file fallbacks under the environment; the key comes only from env."""
    merged = dict(env)
    merged.update(session.connection(root, env))
    if "PLANE_ENABLED" not in env:
        merged["PLANE_ENABLED"] = "true" if session.switch(root, env) == "enabled" else "false"
    return merged


async def execute(namespace: argparse.Namespace, call, project: str) -> Any:
    return await cli_commands.HANDLERS[(namespace.group, namespace.action)](
        call, project, namespace
    )


async def run(namespace: argparse.Namespace, merged: dict[str, str], project: str, connect):
    async with connect(merged) as call:
        return await execute(namespace, call, project)


def main(
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
        try:
            settings = Settings.from_env(merged)
        except ValueError as error:
            return fail(3, str(error))
        data = asyncio.run(run(namespace, merged, settings.project, connect))
    except UsageError as error:
        return fail(2, str(error))
    except ReadbackError:
        return fail(5, "write not confirmed by readback; check the remote state")
    except KeyboardInterrupt:
        return 130
    except ValueError as error:
        return fail(4, str(error))
    except Exception:  # noqa: BLE001 - transport text may hold secrets; print fixed text only
        return fail(4, FAILED)
    out.write(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
