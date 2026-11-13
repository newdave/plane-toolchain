<!-- plane-toolchain:begin (managed by scripts/install_project.py; edit outside this block) -->
## Task management: Plane

Plane is the authoritative system for this project's scope, acceptance criteria, dependencies and status.

- Use the `plane` MCP server for every task read and write. Reread each write to verify it.
- Do not keep a parallel backlog in repository files, TODO comments or agent todo lists.
- Treat "tasks" or "the queue" as this project's Plane work items.
- Move a work item to In Progress only while it is actively worked on.
- Move it to Done only when its acceptance criteria are met. A merged PR alone is not Done.
- Store specs, plans and other planning narratives as project Pages, not repository files.
- When blocked on a human decision, post a comment with the exact question and options, add the `awaiting-decision` label, and move the task out of In Progress unless other work on it continues; reread both writes to verify. Remove the label when answered and resume.
- Keep Plane endpoints, project IDs and credentials out of committed files. Non-secret values go in ignored `.plane/local.env`. The API key comes from the environment or SOPS-encrypted `.plane/plane.enc.env`.
- Check the local switch with `.plane/plane.sh status`. Change it with `.plane/plane.sh enable` or `disable`.
<!-- plane-toolchain:end -->
