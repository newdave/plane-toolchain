"""Explicit compatibility tools for Plane CE's supported public relations API.

Run this file as the hosted MCP entry point. Official tools remain available;
CE tools use the same request authentication, SDK and backend permissions.
"""

# Dependencies below exist in the hosted Plane MCP image, not this toolchain's environment.
# pyright: reportMissingImports=false

import asyncio
import hashlib
import os
import re
from typing import Literal
from uuid import UUID

DEPENDENCIES = (
    "blocking",
    "blocked_by",
    "start_before",
    "start_after",
    "finish_before",
    "finish_after",
)


def canonical_uuid(value):
    """Return the canonical lowercase hyphenated form; UUID() also accepts braces, urn: and hex."""
    if not isinstance(value, str):
        raise ValueError("Expected a UUID string")
    return str(UUID(value))


def sdk_private(resource, name):
    """Fetch a private SDK method, failing clearly if an SDK upgrade removed it."""
    method = getattr(resource, name, None)
    if not callable(method):
        raise RuntimeError(
            f"Plane SDK {type(resource).__name__} has no {name}(); "
            "SDK changed, retest ce_tools before upgrading"
        )
    return method


def bind_host(env):
    """Bind host from FASTMCP_HOST; default 0.0.0.0 because the container must listen publicly."""
    host = env.get("FASTMCP_HOST", "").strip() or "0.0.0.0"
    if not re.fullmatch(r"[A-Za-z0-9._:\-]+", host):
        raise ValueError("FASTMCP_HOST must be a bare host name or address")
    return host


def relation_result(response):
    """Translate CE issue IDs into the MCP dependency response contract.

    CE relations are workspace-wide, so a row may name an issue in another project.
    Rows keep their own project_id and are not filtered: only ids are returned, never
    content, and reading the issue still needs that project's membership.
    """
    # SDK's legacy response model expects strings, while CE v1.4.3 returns
    # issue_id/project_id objects. Decode its public API payload directly.
    data = response if isinstance(response, dict) else response.model_dump()
    groups = {}
    for kind, rows in data.items():
        groups[kind] = [
            {"id": row.get("issue_id"), "project_id": row.get("project_id")} for row in rows
        ]
        if any(not row["id"] for row in groups[kind]):
            raise ValueError("Missing relation identity")
    return {
        "dependencies": {kind: groups.get(kind, []) for kind in DEPENDENCIES},
        "custom": {kind: groups.get(kind, []) for kind in ("duplicate", "relates_to")},
    }


CE_PAGE_DESCRIPTION = """Plane CE project Pages: list, retrieve, create, update.

Create and update validate HTML and use Plane Live to generate the editor document.
access: 0 public within project, 1 private. Project membership and private-page
permissions are enforced by the backend with the caller's API key.
Retrieve/list before creating to avoid duplicate pages after an uncertain write.

update replaces the whole body of page_id with description_html. Pass
expected_updated_at exactly as returned by retrieve; a changed page is refused (409).
Locked and archived pages are refused. update requires force=true because it
disconnects everyone editing the page in Live first; unsaved edits from the last
few seconds may be lost. The replaced body is kept as a page version.
After the write the tool waits the backend's late_store_check seconds (about 12), retrieves
the page and compares its body digest. The result carries late_store: "unchanged"; if an open
editor's late save replaced the update, the tool fails naming the page, so retrieve and retry.
"""


def page_request(action, project_id, page_id):
    """Keep Pages compatibility explicit and project scoped."""
    project_id = canonical_uuid(project_id)
    if action not in {"list", "retrieve", "create", "update"}:
        raise ValueError("CE Pages supports list, retrieve, create and update only")
    path = f"{project_id}/ce-pages"
    if action in {"retrieve", "update"}:
        path += "/" + canonical_uuid(page_id)
    return path


def page_update_body(description_html, expected_updated_at, force):
    """Request body for a whole-body Page update; the backend validates it again."""
    if not isinstance(expected_updated_at, str) or not expected_updated_at:
        raise ValueError("update requires expected_updated_at from retrieve")
    # An empty body would wipe the page; create may still be empty, update may not.
    if not isinstance(description_html, str) or not description_html.strip():
        raise ValueError("update requires a non-empty description_html")
    return {
        "description_html": description_html,
        "expected_updated_at": expected_updated_at,
        "force": force,
    }


INTAKE_STATUSES = (-1, 0, 1, 2)


def intake_decide_body(status, snoozed_till, duplicate_to):
    """Top-level body for CE intake-issues/<id>/ PATCH (IntakeIssueUpdateSerializer fields).

    status: -1 rejected, 0 snoozed (needs snoozed_till), 1 accepted, 2 duplicate (needs
    duplicate_to, a work item UUID). Fields not needed by the status are refused, not dropped.
    """
    if isinstance(status, bool) or status not in INTAKE_STATUSES:
        raise ValueError(
            "status must be one of -1 (rejected), 0 (snoozed), 1 (accepted), 2 (duplicate)"
        )
    if status == 0 and not snoozed_till:
        raise ValueError("status 0 (snoozed) requires snoozed_till")
    if status != 0 and snoozed_till:
        raise ValueError("snoozed_till is only valid with status 0")
    if status == 2 and not duplicate_to:
        raise ValueError("status 2 (duplicate) requires duplicate_to")
    if status != 2 and duplicate_to:
        raise ValueError("duplicate_to is only valid with status 2")
    body = {"status": status}
    if status == 0:
        body["snoozed_till"] = snoozed_till
    if status == 2:
        body["duplicate_to"] = canonical_uuid(duplicate_to)
    return body


def intake_path(workspace, project_id, workitem_id):
    """Plain CE detail path; never the unrouted .../status suffix."""
    return (
        f"{workspace}/projects/{canonical_uuid(project_id)}"
        f"/intake-issues/{canonical_uuid(workitem_id)}"
    )


def decide_intake(intake, workspace, project_id, workitem_id, status, snoozed_till, duplicate_to):
    """Precheck access, PATCH the decision, reread, and fail if the status did not change.

    CE applies intake fields only for project admins and silently ignores them for
    others, so the reread is compared with the request rather than trusted.
    """
    body = intake_decide_body(status, snoozed_till, duplicate_to)
    path = intake_path(workspace, project_id, workitem_id)
    sdk_private(intake, "_get")(path)
    sdk_private(intake, "_patch")(path, body)
    record = sdk_private(intake, "_get")(path)
    if not isinstance(record, dict):
        record = record.model_dump()
    if record.get("status") != body["status"]:
        raise RuntimeError(
            "Intake status was not changed; CE needs project admin to decide intake items"
        )
    return record


MAX_LATE_STORE_SECONDS = 60


async def confirm_late_store(result, retrieve, sleep):
    """Wait out Live's store debounce, then check the update survived.

    result is the backend's update response; retrieve is an async callable returning the
    page; sleep is asyncio.sleep or a test stand-in. A backend without late_store_check
    (older payload) is returned unchanged. A body digest that differs from the hint means
    Live replaced the update; the error names the page id only.
    """
    hint = result.get("late_store_check") if isinstance(result, dict) else None
    if not isinstance(hint, dict):
        return result
    seconds = hint.get("seconds")
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise ValueError("Backend sent an invalid late_store_check")
    await sleep(min(max(seconds, 0), MAX_LATE_STORE_SECONDS))
    page = await retrieve()
    html = page.get("description_html") if isinstance(page, dict) else None
    if hashlib.sha256((html or "").encode()).hexdigest() != hint.get("html_sha256"):
        raise RuntimeError(
            f"Page {result.get('id')} was overwritten by an open editor after the update; "
            "retrieve it and retry"
        )
    return {**result, "late_store": "unchanged"}


async def run_page_action(client, workspace, path, action, project_id, fields):
    """Run a ce_page action; every blocking SDK call runs in a thread, off the event loop."""
    await asyncio.to_thread(client.projects.retrieve, workspace, project_id)
    endpoint = f"{workspace}/projects/{path}"
    if action == "create":
        return await asyncio.to_thread(
            sdk_private(client.pages, "_post"),
            endpoint,
            {
                "name": fields["name"],
                "description_html": fields["description_html"],
                "access": fields["access"],
            },
        )
    if action == "update":
        result = await asyncio.to_thread(
            sdk_private(client.pages, "_patch"),
            endpoint,
            page_update_body(
                fields["description_html"], fields["expected_updated_at"], fields["force"]
            ),
        )
        return await confirm_late_store(
            result,
            lambda: asyncio.to_thread(sdk_private(client.pages, "_get"), endpoint),
            asyncio.sleep,
        )
    return await asyncio.to_thread(sdk_private(client.pages, "_get"), endpoint)


def main():
    from plane.models.work_items import CreateWorkItemRelation
    from plane_mcp.client import get_plane_client_context
    from plane_mcp.server import get_header_mcp

    server = get_header_mcp()

    @server.tool(name="ce_workitem_relation")
    def ce_workitem_relation(
        action: Literal["list_definitions", "list", "create"],
        project_id: str = "",
        workitem_id: str = "",
        workitem_ids: list[str] | None = None,
        relation_type: Literal[
            "blocking", "blocked_by", "start_before", "start_after", "finish_before", "finish_after"
        ] = "blocked_by",
    ) -> dict:
        """Plane CE public relations API.

        Additive dependencies only; no custom definitions or deletion.

        list_definitions returns built-in types. list returns dependencies and legacy
        duplicate/relates_to groups. create sets direction from workitem_id's perspective.
        All operations retain official MCP API-key authentication and backend permissions.
        """
        client, workspace = get_plane_client_context()
        if action == "list_definitions":
            return {"built_in_dependencies": list(DEPENDENCIES), "custom_definitions": []}
        project_id = canonical_uuid(project_id)
        workitem_id = canonical_uuid(workitem_id)
        # Verify project-scoped access before using CE's workspace-wide relation
        # query. Never use a supplied issue ID to bypass project membership.
        client.work_items.retrieve(workspace, project_id, workitem_id)
        if action == "create":
            targets = list(dict.fromkeys(canonical_uuid(t) for t in workitem_ids or []))
            if not targets or len(targets) > 100 or workitem_id in targets:
                raise ValueError("Expected one to 100 distinct non-self dependencies")
            for target in targets:
                client.work_items.retrieve(workspace, project_id, target)
            client.work_items.relations.create(
                workspace,
                project_id,
                workitem_id,
                CreateWorkItemRelation(relation_type=relation_type, issues=targets),
            )
        return relation_result(
            sdk_private(client.work_items.relations, "_get")(
                f"{workspace}/projects/{project_id}/work-items/{workitem_id}/relations"
            )
        )

    @server.tool(name="ce_intake")
    def ce_intake(
        action: Literal["decide"],
        project_id: str,
        workitem_id: str,
        status: int,
        snoozed_till: str = "",
        duplicate_to: str = "",
    ) -> dict:
        """Plane CE intake decision (the official intake update calls an unrouted /status path).

        decide sets status on an intake record: -1 rejected, 0 snoozed (needs snoozed_till,
        ISO 8601 datetime), 1 accepted, 2 duplicate (needs duplicate_to, a work item UUID).
        workitem_id is the intake record's issue id (its work item id), as listed by intake.
        Sends a plain PATCH to intake-issues/<id>/ with the caller's API key; CE allows it
        for project admins only. Returns the reread intake record.
        """
        if action != "decide":
            raise ValueError("ce_intake supports decide only")
        client, workspace = get_plane_client_context()
        return decide_intake(
            client.intake, workspace, project_id, workitem_id, status, snoozed_till, duplicate_to
        )

    @server.tool(name="ce_page", description=CE_PAGE_DESCRIPTION)
    async def ce_page(
        action: Literal["list", "retrieve", "create", "update"],
        project_id: str,
        page_id: str = "",
        name: str = "",
        description_html: str = "",
        access: int = 1,
        expected_updated_at: str = "",
        force: bool = False,
    ) -> dict | list:
        path = page_request(action, project_id, page_id)
        project_id = canonical_uuid(project_id)
        client, workspace = get_plane_client_context()
        return await run_page_action(
            client,
            workspace,
            path,
            action,
            project_id,
            dict(
                name=name,
                description_html=description_html,
                access=access,
                expected_updated_at=expected_updated_at,
                force=force,
            ),
        )

    server.run(
        transport="streamable-http",
        host=bind_host(os.environ),
        port=int(os.environ.get("FASTMCP_PORT", "8211")),
        path="/http/api-key/mcp",
        stateless_http=True,
    )


if __name__ == "__main__":
    main()
