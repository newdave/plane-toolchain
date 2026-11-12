# Plane Toolchain

Use `mise run check` before committing. Work on a branch; review the full staged diff and preserve hooks and user changes.

Keep all agent configuration project-scoped. Never modify user-level settings. Never print, write or commit plaintext secrets. Credentials are environment-only. Do not deploy or change live hosts without explicit authority.

Plane owns approved scope, acceptance and status. Use the configured MCP server for task interactions, verify writes by rereading, and keep In Progress limited to active work. Store planning narratives in project Pages. Keep private project identities and endpoints out of public files.

Preserve CE extension permissions and legacy rollback receipt names. Existing Page edits and relation deletion are unsupported. Do not silently add them during a refactor.

Run examples only with explicit project/task identities. Workflow completion requires a successful deployment and health check. PR merge alone does not mean Done. Never pass secrets to code from fork PRs.
