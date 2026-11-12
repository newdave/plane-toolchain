"""Small Actions example: use MCP, explicit IDs and verified remote writes.

This is an automation example, not the planned general-purpose CLI.
"""

import asyncio
import html
import os
import re
import sys
from uuid import UUID

from plane_toolchain.connection import connection


def inputs(env):
    action = env.get("PLANE_EXAMPLE_ACTION", "check")
    if action not in {"check", "comment", "complete"}:
        raise ValueError("Unknown example action")
    project = str(UUID(env["PLANE_PROJECT"]))
    if action == "check":
        return action, project, "", "", ""
    # Never use task names or PR text to select a task.
    item = str(UUID(env["PLANE_WORKITEM_ID"]))
    if env.get("GITHUB_EVENT_NAME") in {"pull_request", "pull_request_target"}:
        raise ValueError("Write examples require trusted dispatch or deployment jobs")
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
    await call("workitem", {"action": "retrieve", "project_id": project, "workitem_id": item})
    if state:
        target = await call(
            "state", {"action": "retrieve", "project_id": project, "state_id": state}
        )
        if target.get("group") != "completed":
            raise ValueError("Configured state is not in the completed group")
    body = f'<p>GitHub {action} evidence: <a href="{html.escape(evidence)}">workflow run</a></p>'
    arguments = {"project_id": project, "workitem_id": item}
    # No automatic retries: a timeout can mean the write succeeded remotely.
    comment = await call("workitem_comment", dict(arguments, action="create", comment_html=body))
    saved = await call(
        "workitem_comment", dict(arguments, action="retrieve", comment_id=comment["id"])
    )
    if evidence not in saved.get("comment_html", ""):
        raise ValueError("Comment readback did not confirm evidence; inspect Plane")
    if state:
        await call("workitem", dict(arguments, action="update", state=state))
        saved = await call("workitem", dict(arguments, action="retrieve"))
        if saved.get("state") != state:
            raise ValueError("Task state readback did not confirm completion; inspect Plane")


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
