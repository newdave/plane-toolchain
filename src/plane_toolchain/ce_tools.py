"""Explicit compatibility tools for Plane CE's supported public relations API.

Run this file as the hosted MCP entry point. Official tools remain available;
CE tools use the same request authentication, SDK and backend permissions.
"""

# Dependencies below exist in the hosted Plane MCP image, not TerrainLink's environment.
# pyright: reportMissingImports=false

import os
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


def relation_result(response):
    """Translate CE issue IDs into the MCP dependency response contract."""
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


def page_request(action, project_id, page_id):
    """Keep Pages compatibility explicit and project scoped."""
    UUID(project_id)
    if action not in {"list", "retrieve", "create"}:
        raise ValueError("CE Pages supports list, retrieve and create only")
    path = f"{project_id}/ce-pages"
    if action == "retrieve":
        UUID(page_id)
        path += "/" + page_id
    return path


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
        UUID(project_id)
        UUID(workitem_id)
        # Verify project-scoped access before using CE's workspace-wide relation
        # query. Never use a supplied issue ID to bypass project membership.
        client.work_items.retrieve(workspace, project_id, workitem_id)
        if action == "create":
            targets = list(dict.fromkeys(workitem_ids or []))
            if not targets or len(targets) > 100 or workitem_id in targets:
                raise ValueError("Expected one to 100 distinct non-self dependencies")
            for target in targets:
                UUID(target)
                client.work_items.retrieve(workspace, project_id, target)
            client.work_items.relations.create(
                workspace,
                project_id,
                workitem_id,
                CreateWorkItemRelation(relation_type=relation_type, issues=targets),
            )
        return relation_result(
            client.work_items.relations._get(
                f"{workspace}/projects/{project_id}/work-items/{workitem_id}/relations"
            )
        )

    @server.tool(name="ce_page")
    def ce_page(
        action: Literal["list", "retrieve", "create"],
        project_id: str,
        page_id: str = "",
        name: str = "",
        description_html: str = "",
        access: int = 1,
    ) -> dict | list:
        """Plane CE project Pages: list, retrieve, create. Existing document edits are unsupported.

        Creation validates HTML and uses Plane Live to generate the editor document.
        access: 0 public within project, 1 private. Project membership and private-page
        permissions are enforced by the backend with the caller's API key.
        Retrieve/list before creating to avoid duplicate pages after an uncertain write.
        """
        path = page_request(action, project_id, page_id)
        client, workspace = get_plane_client_context()
        client.projects.retrieve(workspace, project_id)
        endpoint = f"{workspace}/projects/{path}"
        if action == "create":
            return client.pages._post(
                endpoint, {"name": name, "description_html": description_html, "access": access}
            )
        return client.pages._get(endpoint)

    server.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=int(os.environ.get("FASTMCP_PORT", "8211")),
        path="/http/api-key/mcp",
        stateless_http=True,
    )


if __name__ == "__main__":
    main()
