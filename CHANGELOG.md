# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/) (0.x: minor releases may change behaviour).

## [Unreleased]

### Fixed

- MCP deploy writes the backup atomically (temporary file, fsync, `os.replace`, directory fsync, entry-point mode) before it touches `server.py`. `server.py` is still written in place so the bind-mounted inode survives, but a kill mid-write now recovers: an empty file or a prefix of the new bytes (receipt pending) or of the backup is accepted by `apply` and `revert`, which carry on from the backup, and a failed recovery rolls back to the backup. Foreign content is still refused. Kill-injection tests cover each write step (PTC-65).
- The Pages and MCP remote programs install a SIGTERM handler (`remote.guarded`): SIGTERM raises `SystemExit(143)` so the existing `BaseException` rollbacks restore the container swap or the entry-point bytes and receipt, prints one line, and later SIGTERMs are ignored. Real-process tests send SIGTERM during Pages apply and MCP apply (PTC-66).
- Pages deploy diagnostics and housekeeping: container creation has a socket timeout, always closes the connection and names the HTTP status or error kind; derive and build share one `build_image` helper that keeps stdout quiet and reports the exit code and a bounded stderr tail when the build fails; apply and revert results list retained container names; `owner:plane-pages:prune` removes stopped `-pages-reverted-*` and `-pages-failed-*` containers (never the live API, the rollback container or a running one); a failed restart-policy update warns on stderr; the health wait stops after 120 seconds as well as 45 attempts (PTC-68).
- Pages deploy and Docker Compose, documented decision (PTC-67): checked on a scratch Compose project that `docker compose up` removes the derived container and starts the retained original (Pages lost), and `down` removes the retained original. Labels are immutable, so the retained original cannot lose its `com.docker.compose.*` labels; the derived container keeps them too, which makes `up` converge on one container instead of two. `docs/extensions.md` says to revert before Compose updates; tests pin the label sets of the original, derived and retired containers.

## [0.2.1] - 2026-11-14

### Fixed

- `mcp_deploy`, `pages_deploy` and `workspace_rename` no longer print a local traceback when the remote program fails: the SSH `user@host` and the whole remote program source stayed out of the `CalledProcessError` argv dump. A failure prints the remote's own message (streamed unchanged), then one fixed line, and exits with the remote exit code (1 if unknown). A failed SSH connection (255) prints a fixed message and `LogLevel=QUIET` stops ssh naming the host; Ctrl-C exits 130. An uncaught exception on the host prints `Type: message` instead of a traceback (PTC-57).

## [0.2.0] - 2026-11-14

### Added

- Apply pre-approves the `plane` MCP server by adding it to `enabledMcpjsonServers` in the target's `.claude/settings.json` (created if absent, other keys kept), so Claude connects without an approval prompt. Revert removes the entry only when apply added it, keeps every other edit to the file, and leaves an already-listed `plane` alone. Apply refuses invalid JSON or a non-list value without writing. Receipts from before this change still revert. Repeating apply on an older installation names the outdated shim or missing pre-approval and changes nothing; revert, then apply again to update it (PTC-55, PTC-56).
- `plane` command-line interface over the Plane MCP server (`mise run plane:cli`, `python -m plane_toolchain.cli`): `task list`, `task show`, `task update`, `comment list`, `comment add`, `page list`, `page show`, `page create`, `relation list` and `relation add` print JSON (writes are verified by readback; no Page editing, relation deletion or retries; see `docs/cli.md`), `--project` overrides `PLANE_PROJECT`, and exit codes separate usage (2), configuration (3), MCP failure (4), readback mismatch (5) and interrupt (130). The API key is read from the environment only and never printed. `connection()` accepts an optional environment (PTC-2).
- Workspace slug rename owner runbook, `docs/workspace-rename.md`: prerequisites and environment, the unverified Plane CE v1.4.3 assumptions to check before a live run, `check` refusals, the cache flush dry run, apply states with resume-or-revert guidance per failure, revert and partial restore, known risks, the manual `pg_restore` last resort and the post-run steps. Linked from the README and `docs/extensions.md`; a docs test checks that it names every environment variable and owner task (PTC-42).
- `owner:plane-workspace-rename:check`, `:apply` and `:revert` tasks (`python -m plane_toolchain.workspace_rename`). Slugs and destination come from the environment (`PLANE_RENAME_FROM`, `PLANE_RENAME_TO`, `PLANE_MCP_SSH`, `PLANE_API_CONTAINER`, `PLANE_LIVE_URL`, optional receipt directory, `PLANE_API_URL` and service overrides), are validated before SSH and never printed. The payload and snippet travel on stdin, never in argv, and the remote exit code passes through (PTC-41).
- Installed projects used from a linked git worktree read the main checkout's git-ignored `.plane/local.env` (merged per key, the worktree's own file wins) and `.plane/plane.enc.env`, in place and never copied. The shim also finds `PLANE_TOOLCHAIN` there. The bridge reuses the main checkout's `mcp-remote` only when `tools/mcp-remote/package-lock.json` has the same SHA-256 in both and the bin stays inside the main `node_modules`; otherwise it fails with the `mise run setup` hint. Symlinked main files and a common dir not named `.git` get no fallback. Installs made before this keep working: revert accepts the earlier shim, and repeat apply reports it as outdated without rewriting it (PTC-47).
- `owner:project:label` ensures the `awaiting-decision` label exists in an installed project's Plane project over the REST API (key from the environment only, no network call when settings are missing or Plane is disabled), verifies it by rereading and records its id in the receipt. Installed `AGENTS.md` blocks gain the awaiting-decision rule. Revert never deletes the label. Installations from before this change keep their older block: revert restores them exactly, and apply and label report the block as outdated (PTC-52).
- The workspace rename Django snippet (`workspace_rename_backend.py`) has a read-only `check` that validates the slugs and reports every planned link rewrite per record and field, plus hits it will not fix, as JSON lines with ids, tables, columns and counts only (PTC-34).
- Agents flag a task blocked on a human decision with a comment holding the question and options plus the `awaiting-decision` label, and remove the label when answered (PTC-51).
- `owner:plane-pages:build` builds the derived Pages image from the upstream backend image (or `PLANE_BACKEND_IMAGE`) and prints its tag and source digest, without touching any container. Use it for Saltbox-managed hosts where the running API image is local (PTC-45).
- `ce_page` update replaces an existing Page body. It needs an `expected_updated_at` precondition and `force=true`, disconnects open Live editors first and keeps the old body as a page version (capped at 20). It refuses only if the body changed while editors closed, and reports a late Live overwrite with 409. Both server payloads changed; redeploy them (PTC-44).
- Workspace rename host operations: stop services (beat, queue drain up to 60 s without purging, worker, web, space, admin), restart Live, start in reverse order, and back up the database with `pg_dump` to an owner-only `backup.dump` whose SHA-256 is recorded in the receipt. Only `docker stop`, `start`, `restart` and `exec` are used. Library functions only; no task or CLI yet (PTC-38).
- Workspace rename apply state machine (`workspace_rename.deploy` and the host program `REMOTE`). `check` is read-only. `apply` runs every refusal before the first stop, then steps through `pending`, `stopped`, `backed_up`, `slug_done`, `rows_done`, `cache_done`, `started` and `complete`, each as effect then advance. Kills are resumable: a resume stops services again first and reuses a backup whose SHA-256 matches. If a service is running again before the slug change, the backup is set aside instead. A failure before the slug change restarts the services and resets the receipt to `pending`. It moves the backup and the journal aside under timestamped names (kept, and listed in the receipt), so the next apply takes a fresh dump and journals current originals. An unclear slug step is probed read-only before anything is decided. After the slug change, a failure stops with services down, for resume or revert. Known risk: a kill between the snippet printing a journal line and the host fsyncing it can leave a written row without a journal entry, and only the `pg_dump` backup recovers that row. No task or CLI yet (PTC-39).
- Workspace rename revert (`operation: revert` in the host program `REMOTE`) restores the slug and every journalled field from the receipt, from any partial or complete apply. It steps through `reverting`, `restored`, `started` and `reverted`, each as effect then advance, and a kill at any point is resumable by rerunning revert. Every refusal comes before the first stop: no receipt, a receipt for another rename, an unknown receipt state, a journal without a workspace record, an empty journal past the slug change, or an unclear slug state. The restore reads only the current `records.jsonl` and runs with services stopped. It is skipped when nothing was journalled, or when the slug change never committed. Then the cache is flushed, services start and health runs with the slugs swapped and the journalled workspace id. A partial restore prints every skipped row by id and restarts services, but it is not reported as complete: the receipt stays at `reverting` and the exit code is non-zero. The journal and `backup.dump` are kept; restoring the `pg_dump` backup stays a manual last resort. With nothing journalled, revert still probes the slugs read-only and refuses unless OLD is live and NEW is free. A partial restore restarts services even if recording it in the receipt fails. Apply and check now refuse after a finished revert (archive or move the receipt directory first), and refuse a journal or backup, current or rotated, that has no receipt. No task or CLI yet (PTC-40).
- Project installer (`scripts/install_project.py`, `owner:project:apply`/`revert`) that connects another project to the Plane MCP bridge, registers Codex and Claude session hooks, and adds an AGENTS.md block making Plane the authoritative task system (PTC-48).

### Removed

- **Breaking:** the Pages and MCP deploy helpers no longer read legacy `terrainlink` names, and the migration paths are gone. Pages `check`, `apply` and `revert` and MCP `check`, `apply` and `revert` refuse before any change (Pages `build` only creates an image and does not check) when they find `<api>-before-terrainlink-pages`, the `terrainlink.pages.source` container label on the API container or the backup, `server.py.before-terrainlink-ce`, `terrainlink-ce-receipt.json` or its `.tmp`. The message names what was found. The tripwire fails closed: a failed `docker ps` or `docker inspect` of a listed container refuses the run instead of reading as clean. To upgrade a host that has this state, first run the owner tasks at commit `1cb6153`: Pages `revert` (it consumes the legacy backup and parks the legacy derived container as an inert `<api>-pages-reverted-<ts>`; run `apply` there only if `check` passes there, since it refuses when the deployed digest differs) and MCP `apply` (MCP `revert` there does not migrate). Then run Pages `apply` with this release. Retired `-pages-reverted-*` containers keep the legacy label, are not checked and are left for owner cleanup. A Saltbox host's next build, from this release or the Saltbox role, must write the neutral label, or every operation from this release refuses there. Removed early by owner override of the ADR 0001 deprecation window; `legacy.py` is kept with the four refused names. Server payload digests are unchanged (PTC-30).

### Changed

- TerrainLink is removed from prose, comments and docs. The repository was extracted from TerrainLink under the MIT licence; the README line is dropped and the origin is recorded here, and `LICENSE` is unchanged. Legacy `terrainlink` names remain only as compatibility data in `src/plane_toolchain/legacy.py` and in the migration docs. Both server payload digests changed (`ce_tools.py` and `pages_backend.py` comment wording only, no behaviour change), so hosts need a redeploy by the owner (Saltbox role pin bump, tracked by PTC-53). ADR 0001 is Accepted (PTC-29).
- The Pages deploy helper writes only neutral names: backup container `<name>-before-plane-toolchain-pages`, label `plane-toolchain.pages.source`, image tag `plane-toolchain-pages:<digest16>` and backend module `plane.api.plane_toolchain_pages`. It first read the legacy names and migrated them; PTC-30 removed that, see Removed. Saltbox users set the neutral label after the next `owner:plane-pages:build` (PTC-27).
- The MCP compat deploy writes `server.py.before-plane-toolchain-ce` and `plane-toolchain-ce-receipt.json`. It first read and migrated the legacy names; PTC-30 removed that, see Removed (PTC-28).
- Legacy deploy names are centralised in `plane_toolchain/legacy.py`, with a test that rejects new references elsewhere. Behaviour is unchanged.

### Fixed

- The installed shim, the MCP bridge and every `uv run`/`uv sync` mise task run `uv` with `VIRTUAL_ENV` cleared, so an active unrelated virtualenv no longer triggers the uv "does not match the project environment" warning. The post-apply message names only the `.plane/local.env` values still missing or empty, and otherwise points straight at `.plane/plane.sh enable`. Installs with the previous shim still revert (PTC-56).
- Project install revert accepts installs reformatted by the target's prettier and shfmt hooks. The installed `.mcp.json`, hook JSON and receipt are parsed and only the installed entries removed, not compared as bytes. `.plane/plane.sh` is accepted as shipped and as shfmt v3.14.1 formats it with `-i 0` (tabs, the defaults), `-i 2`, `-i 2 -ci` or `-i 4`, for the current shim and every earlier version. Any other shfmt style, and any content change, is refused: run `git checkout` (or reconcile by hand) on `.plane/plane.sh` before revert (PTC-54).
- The Plane bridge and session status resolve git-ignored `config/local.env`, `config/plane.enc.env` and `tools/mcp-remote/node_modules` from the main checkout when a linked worktree lacks them (`config/local.env` merges per key, the worktree overriding). Only linked worktrees fall back.
- Pages revert accepts a derived container built from older source, so an upgrade can run revert, then apply.
- Restoring the original API (revert, reconcile) checks API health only: the upstream image has no Pages route, so the Pages probe made every revert roll back.

## [0.1.0] - 2026-11-12

### Added

- Plane CE extensions and GitHub Actions integration examples.
- Security policy and pull request template (#10).
- Pinned lint of workflows and shell scripts with actionlint, zizmor and shellcheck (#6).
- Renovate configuration for pinned dependency updates (#11).
- Pre-commit config matching `mise run check` (#15).
- Default-branch ruleset recorded with maintenance commands (#5).
- Tag-triggered release workflow and this changelog (#19).
- Pages deploy health check probes a Pages route (#22).
- Remote deploys survive SSH disconnects; Pages apply reconciles a half-finished run (#24).
- Pages revert finishes an interrupted revert (#25).

### Changed

- Deploy remote programs are built from testable functions (#12).
- Declare `httpx2`, enable ruff `S` rules and use mise Python in CI (#14).
- MCP bridge runs a locked `mcp-remote` tree; Node pinned to 26.13.1 (#20).

### Fixed

- Pages revert recovers from failures and a repeat revert is idempotent (#17).
- MCP compat deploy waits for health, rolls back, and keeps receipts kill-safe (#18).
- Retained rollback containers no longer auto-start (#23).

- Bridge loads checkout connection settings and the encrypted key.
- Default SOPS age key path when the bridge runs outside mise.
- Pages backend returns 404 for missing projects and a slim list payload (#9).
- `PLANE_ENABLED` parser is shared and state files are hardened (#8).
- CE tools canonicalise IDs, guard SDK private calls and use a configurable bind host (#7).

### Security

- Example write workflows allow-list trusted events and fail closed (#13).
- Pages API responses never include internal error text (#16, #21).
