# Plane Toolchain

Use `mise run check` before committing. Work on a branch; review the full staged diff and preserve hooks and user changes.

Keep all agent configuration project-scoped. Never modify user-level settings. Never print, write or commit plaintext secrets. Credentials are environment-only. Do not deploy or change live hosts without explicit authority.

Plane owns approved scope, acceptance and status. Use the configured MCP server for task interactions, verify writes by rereading, and keep In Progress limited to active work. When blocked on a human decision, post a comment with the exact question and options, add the `awaiting-decision` label, and move the task out of In Progress unless other work on it continues; reread both writes to verify. Remove the label when answered and resume. Store planning narratives in project Pages. Write task descriptions with real headings and lists (Context, Scope, Acceptance, plus optional Files, Dependencies and Notes), never paragraphs with line-break dashes; the project Page "Task description layout" defines the layout. Keep private project identities and endpoints out of public files.

Preserve CE extension permissions. Legacy `terrainlink` names are read-only compatibility inputs: read them, write only neutral names, and keep them in `src/plane_toolchain/legacy.py`; change them only through `docs/decisions/0001-neutral-names.md`. Until its Pages and MCP migrations land, the helpers still write legacy names; do not rename them ahead of that migration. Existing Pages may be edited only through `ce_page` update (whole body, `expected_updated_at` precondition, `force=true` to disconnect open editors). Relation deletion is unsupported. Do not silently add other edit paths or deletion during a refactor.

Run examples only with explicit project/task identities. Workflow completion requires a successful deployment and health check. PR merge alone does not mean Done. Never pass secrets to code from fork PRs.
