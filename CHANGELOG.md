# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/) (0.x: minor releases may change behaviour).

## [Unreleased]

### Added

- Installed projects used from a linked git worktree read the main checkout's git-ignored `.plane/local.env` (merged per key, the worktree's own file wins) and `.plane/plane.enc.env`, in place and never copied. The shim also finds `PLANE_TOOLCHAIN` there. The bridge reuses the main checkout's `mcp-remote` only when `tools/mcp-remote/package-lock.json` has the same SHA-256 in both and the bin stays inside the main `node_modules`; otherwise it fails with the `mise run setup` hint. Symlinked main files and a common dir not named `.git` get no fallback. Installs made before this keep working: revert accepts the earlier shim, and repeat apply reports it as outdated without rewriting it (PTC-47).
- `owner:project:label` ensures the `awaiting-decision` label exists in an installed project's Plane project over the REST API (key from the environment only, no network call when settings are missing or Plane is disabled), verifies it by rereading and records its id in the receipt. Installed `AGENTS.md` blocks gain the awaiting-decision rule. Revert never deletes the label. Installations from before this change keep their older block: revert restores them exactly, and apply and label report the block as outdated (PTC-52).
- The workspace rename Django snippet (`workspace_rename_backend.py`) has a read-only `check` that validates the slugs and reports every planned link rewrite per record and field, plus hits it will not fix, as JSON lines with ids, tables, columns and counts only (PTC-34).
- Agents flag a task blocked on a human decision with a comment holding the question and options plus the `awaiting-decision` label, and remove the label when answered (PTC-51).
- `owner:plane-pages:build` builds the derived Pages image from the upstream backend image (or `PLANE_BACKEND_IMAGE`) and prints its tag and source digest, without touching any container. Use it for Saltbox-managed hosts where the running API image is local (PTC-45).
- `ce_page` update replaces an existing Page body. It needs an `expected_updated_at` precondition and `force=true`, disconnects open Live editors first and keeps the old body as a page version (capped at 20). It refuses only if the body changed while editors closed, and reports a late Live overwrite with 409. Both server payloads changed; redeploy them (PTC-44).
- Workspace rename host operations: stop services (beat, queue drain up to 60 s without purging, worker, web, space, admin), restart Live, start in reverse order, and back up the database with `pg_dump` to an owner-only `backup.dump` whose SHA-256 is recorded in the receipt. Only `docker stop`, `start`, `restart` and `exec` are used. Library functions only; no task or CLI yet (PTC-38).
- Workspace rename apply state machine (`workspace_rename.deploy` and the host program `REMOTE`). `check` is read-only. `apply` runs every refusal before the first stop, then steps through `pending`, `stopped`, `backed_up`, `slug_done`, `rows_done`, `cache_done`, `started` and `complete`, each as effect then advance. Kills are resumable: a resume stops services again first and reuses a backup whose SHA-256 matches. If a service is running again before the slug change, the backup is set aside instead. A failure before the slug change restarts the services and resets the receipt to `pending`. It moves the backup and the journal aside under timestamped names (kept, and listed in the receipt), so the next apply takes a fresh dump and journals current originals. An unclear slug step is probed read-only before anything is decided. After the slug change, a failure stops with services down, for resume or revert. Known risk: a kill between the snippet printing a journal line and the host fsyncing it can leave a written row without a journal entry, and only the `pg_dump` backup recovers that row. No task or CLI yet (PTC-39).
- Project installer (`scripts/install_project.py`, `owner:project:apply`/`revert`) that connects another project to the Plane MCP bridge, registers Codex and Claude session hooks, and adds an AGENTS.md block making Plane the authoritative task system (PTC-48).

### Changed

- TerrainLink is removed from prose, comments and docs. The repository was extracted from TerrainLink under the MIT licence; the README line is dropped and the origin is recorded here, and `LICENSE` is unchanged. Legacy `terrainlink` names remain only as compatibility data in `src/plane_toolchain/legacy.py` and in the migration docs. Both server payload digests changed (`ce_tools.py` and `pages_backend.py` comment wording only, no behaviour change), so hosts need a redeploy by the owner (Saltbox role pin bump, tracked by PTC-53). ADR 0001 is Accepted (PTC-29).
- The Pages deploy helper writes only neutral names: backup container `<name>-before-plane-toolchain-pages`, label `plane-toolchain.pages.source`, image tag `plane-toolchain-pages:<digest16>` and backend module `plane.api.plane_toolchain_pages`. It still reads the legacy names, so `check`, `revert` and `apply` accept hosts deployed earlier. A re-apply of a host with a healthy legacy-labelled derived rebuilds it from the retained original with the neutral label and module, retires the old derived stopped as `<name>-pages-reverted-<ts>` and renames the backup to the neutral name once healthy; a failure restores the legacy derived. It refuses to act when a legacy and a neutral backup both exist. Saltbox users set the neutral label after the next `owner:plane-pages:build` (PTC-27).
- The MCP compat deploy writes `server.py.before-plane-toolchain-ce` and `plane-toolchain-ce-receipt.json`. It still reads the legacy names and, on a successful apply or repair, migrates them (receipt first, then the digest-verified backup, then the legacy receipt). It refuses with a reconcile message when legacy and neutral files disagree (PTC-28).
- Legacy deploy names are centralised in `plane_toolchain/legacy.py`, with a test that rejects new references elsewhere. Behaviour is unchanged.

### Fixed

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
