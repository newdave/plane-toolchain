# Plane Toolchain

Use `mise run check` before committing. Work on a branch; review the full staged diff and preserve hooks and user changes.

Keep all agent configuration project-scoped. Never modify user-level settings. Never print, write or commit plaintext secrets. Credentials are environment-only. Do not deploy or change live hosts without explicit authority.

Plane owns approved scope, acceptance and status. Use the configured MCP server for task interactions, verify writes by rereading, and keep In Progress limited to active work. When blocked on a human decision, post a comment with the exact question and options, add the `awaiting-decision` label, and move the task out of In Progress unless other work on it continues; reread both writes to verify. Remove the label when answered and resume. Store planning narratives in project Pages. Set a priority (`urgent`, `high`, `medium` or `low`) on every task you create, never `none`. Write task descriptions with real headings and lists (Context, Scope, Acceptance, plus optional Files, Dependencies and Notes), never paragraphs with line-break dashes; the project Page "Task description layout" defines the layout. Keep private project identities and endpoints out of public files.

Preserve CE extension permissions. Legacy host names are no longer read; `src/plane_toolchain/legacy.py` keeps only the names the deploy helpers refuse, and changes go through `docs/decisions/0001-neutral-names.md`. Existing Pages may be edited only through `ce_page` update (whole body, `expected_updated_at` precondition, `force=true` to disconnect open editors). Relation deletion is unsupported. Do not silently add other edit paths or deletion during a refactor.

Run examples only with explicit project/task identities. Workflow completion requires a successful deployment and health check. PR merge alone does not mean Done. Never pass secrets to code from fork PRs.

## graphify

`mise run graph:update` builds a knowledge graph in `graphify-out/` (gitignored, AST only, no API cost). When `graphify-out/graph.json` exists, answer codebase questions with `uv run --group graph graphify query "<question>"`, `path "<A>" "<B>"` or `explain "<concept>"` before grepping; read `graphify-out/GRAPH_REPORT.md` only for broad architecture review. Rerun `mise run graph:update` after changing code. The graph is a navigation aid; source and Plane stay authoritative.
