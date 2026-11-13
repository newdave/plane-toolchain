"""Small Actions example: use MCP, explicit IDs and verified remote writes.

This is an automation example, not the general-purpose CLI (see docs/cli.md).
"""

import asyncio
import html
import os
import re
import sys
from uuid import UUID

from plane_toolchain.connection import connection

MAX_PAGES = 50
TRUSTED_EVENTS = {"workflow_dispatch", "push", "release", "schedule"}


def inputs(env):
    action = env.get("PLANE_EXAMPLE_ACTION", "check")
    if action not in {"check", "comment", "complete"}:
        raise ValueError("Unknown example action")
    project = str(UUID(env["PLANE_PROJECT"]))
    if action == "check":
        return action, project, "", "", ""
    # Never use task names or PR text to select a task.
    item = str(UUID(env["PLANE_WORKITEM_ID"]))
    if env.get("GITHUB_EVENT_NAME") not in TRUSTED_EVENTS:
        raise ValueError("Write examples require a trusted dispatch, push, release or schedule")
    if env.get("GITHUB_REF") != "refs/heads/" + env["PLANE_DEFAULT_BRANCH"]:
        raise ValueError("Write examples require the default branch")
    repo = env["GITHUB_REPOSITORY"]
    run = env["GITHUB_RUN_ID"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not run.isdigit():
        raise ValueError("Invalid GitHub run identity")
    evidence = f"https://github.com/{repo}/actions/runs/{run}"
    state = ""
    if action == "complete":
        if env.get("PLANE_DEPLOYMENT_VERIFIED") != "true":
            raise ValueError("Deployment and health verification required before completion")
        state = str(UUID(env["PLANE_DONE_STATE_ID"]))
    return action, project, item, state, evidence


async def publish(call, values):
    action, project, item, state, evidence = values
    if action == "check":
        await call("project", {"action": "retrieve", "project_id": project})
        return
    task = await call(
        "workitem", {"action": "retrieve", "project_id": project, "workitem_id": item}
    )
    if state:
        target = await call(
            "state", {"action": "retrieve", "project_id": project, "state_id": state}
        )
        if target.get("group") != "completed":
            raise ValueError("Configured state is not in the completed group")
    body = f'<p>GitHub {action} evidence: <a href="{html.escape(evidence)}">workflow run</a></p>'
    arguments = {"project_id": project, "workitem_id": item}
    # Idempotent retry: a rerun of the same workflow run finds its own comment and state
    # and does not repeat either write.
    if not await has_comment(call, arguments, evidence):
        # No automatic retries: a timeout can mean the write succeeded remotely.
        comment = await call(
            "workitem_comment", dict(arguments, action="create", comment_html=body)
        )
        saved = await call(
            "workitem_comment", dict(arguments, action="retrieve", comment_id=comment["id"])
        )
        if evidence not in saved.get("comment_html", ""):
            raise ValueError("Comment readback did not confirm evidence; inspect Plane")
    if state and (task or {}).get("state") != state:
        await call("workitem", dict(arguments, action="update", state=state))
        saved = await call("workitem", dict(arguments, action="retrieve"))
        if saved.get("state") != state:
            raise ValueError("Task state readback did not confirm completion; inspect Plane")


async def has_comment(call, arguments, evidence):
    """Return True if a comment on the task already links this workflow run."""
    # Match the whole quoted href so run 12 never matches run 123.
    needle = f'href="{html.escape(evidence)}"'
    cursor = None
    for _ in range(MAX_PAGES):
        request = dict(arguments, action="list", per_page=100)
        if cursor:
            request["cursor"] = cursor
        listing = await call("workitem_comment", request)
        rows = listing.get("results", []) if isinstance(listing, dict) else listing
        if any(
            needle in str(row.get("comment_html", ""))
            for row in rows or []
            if isinstance(row, dict)
        ):
            return True
        if not (isinstance(listing, dict) and listing.get("next_page_results")):
            return False
        cursor = listing.get("next_cursor")
        if not cursor:
            return False
    return False


async def main():
    values = inputs(os.environ)
    async with connection() as call:
        await publish(call, values)
    print("Plane example completed; remote results verified.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        # Do not print server response bodies, environment values or authentication.
        sys.exit("Plane example failed; inspect remote state before retrying a write.")
