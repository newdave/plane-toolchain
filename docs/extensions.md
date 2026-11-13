# CE extension contract and deployment

The extensions augment the official Plane MCP server. They reuse its API-key authentication and workspace context. They do not replace the official tools or grant additional project access.

## Shipped capabilities

| Tool | Operations | Limits |
| --- | --- | --- |
| `ce_workitem_relation` | Definitions, list, create built-in dependencies | Additive, same project, 1–100 distinct targets; no deletion or custom definitions |
| `ce_page` | List, retrieve, create, update project Pages | Update replaces the whole body; needs `expected_updated_at` and `force=true`; no deletion |

Relations normalise CE `issue_id` objects into the MCP dependency contract. Each source and target is checked through the project-scoped work-item API before creating a relation.

Pages require both the MCP extension and the backend adapter. The backend keeps Plane authentication, permissions, sanitisation and throttling. It checks project membership and private-page visibility. Creation converts HTML through Plane Live into consistent JSON and binary document formats before writing in a transaction. Conversion failure writes nothing. List requests fail above 1,000 Pages rather than silently truncating results.

### Page update

`ce_page` update sends `PATCH .../ce-pages/<page_id>/` with `description_html`, `expected_updated_at` and `force`. The backend runs these steps in order and writes nothing if one fails:

1. Validate the request. Without `force=true` it refuses: an edit disconnects open editors.
2. Load the page. Refuse locked (400), archived (400) or changed pages (409 when `updated_at` differs from `expected_updated_at`, compared as instants). Record a SHA-256 digest of the stored HTML and binary body.
3. Sanitise the HTML and convert it through Live (502 on failure).
4. Publish Live's `force_close` command for the page on the Redis channel `hocuspocus:admin` (Plane's `REDIS_URL`). A publish error or no subscriber returns 502.
5. Wait 1 second so Live can store edits it held in memory.
6. In a transaction, lock the row with `select_for_update` and recheck locked and archived. Refuse with 409 only if the stored body differs from the digest. Live re-storing an unchanged document on unload bumps `updated_at`; that does not block the write.
7. Save the old body as a `PageVersion` and prune the page's versions to 20, oldest first. Then write the HTML, JSON and binary formats in one `save()`.

After commit it queues Plane's `page_transaction` (mention log). It does not queue `track_page_version`: in CE v1.4.3 that task reads a missing `Page.description` attribute, and if fixed it would overwrite the snapshot in place.

It then polls the stored binary for 12 seconds (one Live debounce plus margin). If it changes, the response is 409 "Edit was overwritten by an open editor; retry". The write and its version stay, but Live's store has replaced the body. A successful update therefore takes about 13 seconds plus conversion time.

Missing or invisible pages get 403 from Plane's `ProjectPagePermission` before the view runs, as for retrieve. The view's own 404 is defence in depth.

Why: Live keeps an open document in memory and stores it with a 10-second debounce. It never re-reads the database, so a database write while the document is open is overwritten. `force_close` is the only external way to disconnect its editors; Live then stores and unloads the document.

Risks:

- Edits typed in the last moments before the disconnect may be lost. Usually Live stores them first, the body changes and the update is refused with 409. A store arriving after the lock overwrites the new body; the 12-second watch reports it, but a store after the watch goes unreported.
- Any refusal after `force_close` (409, locked, archived) leaves editors disconnected with nothing written.
- Plane's own page metadata update (rename and similar) saves the whole row. One that read the page before this update can write the old body back. Nothing here detects that.
- An editor that reconnects inside the wait loads the old body.
- An offline client keeps its old document state. When it reconnects, it may merge that state into the new body.
- The previous body stays recoverable from page version history.

**PTC-44 migration note.** `ce_tools.py` and `pages_backend.py` changed, so both payload digests changed. Hosts report a source difference on `owner:plane-mcp-compat:check` and `owner:plane-pages:check`. Redeploy both through the owner tasks, with explicit deployment authority. For Pages, run revert, then apply. Update needs both payloads redeployed.

The backend adapter was tested against **Plane CE v1.4.3**. Its Django imports, permission contract and Live conversion route depend on that layout. Compatibility with another version requires testing in a separate environment. The MCP entry point imports `plane` and `plane_mcp` from the existing official server image; those server dependencies are not installed by the client package.

## Existing deployment helpers

These helpers target existing owner-managed Docker services over SSH. They are not a general Plane installer. Extraction has not applied them to any host.

Provide non-secret destination settings through an authorised local environment:

| Helper | Required variables |
| --- | --- |
| MCP entry point | `PLANE_MCP_SSH`, absolute `PLANE_MCP_DIRECTORY`; optional `PLANE_MCP_CONTAINER` (default `plane-mcp`) |
| Pages backend | `PLANE_MCP_SSH`, `PLANE_API_CONTAINER`, `PLANE_LIVE_URL` reachable from the backend container |

After reviewing the destination, use the paired tasks:

```sh
mise run owner:plane-mcp-compat:check
mise run owner:plane-mcp-compat:apply
mise run owner:plane-mcp-compat:revert

mise run owner:plane-pages:check
mise run owner:plane-pages:apply
mise run owner:plane-pages:revert
```

The MCP helper rewrites `server.py` in place (truncate, write, flush, fsync) so the bind-mounted inode survives. The rewrite is not atomic on purpose: `os.replace` would give `server.py` a new inode that the container may never see. The backup `server.py.before-plane-toolchain-ce` and the receipt are written atomically (temporary file, fsync, `os.replace`, directory fsync), and the backup is complete before `server.py` is touched. A crash mid-write leaves `server.py` empty or a prefix of the new entry point (or, in a killed rollback or revert, of the backup). `apply` and `revert` recognise exactly that state when the receipt is pending for the new bytes, or the file is a prefix of the backup, and carry on from the backup; foreign content is still refused. A rollback after such a recovery restores the backup, not the partial file. After the restart it polls the container (running, not restarting, healthy when a healthcheck exists, restart count stable) for up to 45 seconds. On timeout or a crash loop it restores the previous bytes, restarts again and fails. A pending receipt (`pending`, `previous`) is written atomically before the rewrite and replaced by the final receipt once healthy. A rollback restores the old receipt text exactly, or deletes it on a first apply. After a killed deploy, `revert` and `apply` accept either the installed or previous digest while the receipt is pending, and `apply` repairs a stale receipt. A leftover `plane-toolchain-ce-receipt.json.tmp` is harmless and is overwritten by the next write. Without a healthcheck, the container must stay running for about 5 seconds; a slow healthcheck (long interval or start_period) may need more than the default 45 attempts. Original-versus-installed detection compares SHA-256 digests with the receipt; with no recorded original it accepts only a plain entry point that references `get_header_mcp` and defines no functions or classes.

The MCP helper writes and reads only `server.py.before-plane-toolchain-ce` and `plane-toolchain-ce-receipt.json`. It refuses legacy-named state; see [Hosts with legacy-named state](#hosts-with-legacy-named-state). It writes the receipt atomically and moves nothing between names. A failed health check rolls the entry point back. `revert` restores `server.py.before-plane-toolchain-ce`, verified against the receipt digest.

A check fails when the installed source differs. The MCP helper refuses tracked or unexpected host entry points, preserves a backup and checks receipts before overwriting. The Pages helper builds a derived image from the running immutable image, preserves opaque Docker configuration and keeps the original container for rollback. It rejects auto-removal and static IP configurations requiring a specific migration. It checks backend HTTP health; verify authenticated MCP operations separately after applying.

### Saltbox route

A Saltbox role owns the API container and pins its image, labels and environment in the inventory, so the direct `apply` route (which swaps the container) does not fit. It also refuses a local running image. Use the build-only task instead. Obtain deployment authority first.

1. Run `mise run owner:plane-pages:build` (needs `PLANE_MCP_SSH`; optional `PLANE_WORKER_CONTAINER`, default `plane-worker`, and `PLANE_BACKEND_IMAGE`). It derives `makeplane/plane-backend:<tag>` from the worker image unless `PLANE_BACKEND_IMAGE` is set, builds from that image's registry digest and prints `<image tag> <source digest>`. It refuses when the upstream image is missing or has no registry digest. It never creates, stops, renames or removes a container.
2. Back up `/srv/git/saltbox/inventories/host_vars/localhost.yml`, then set:
   - `plane_role_api_docker_image`: the printed image tag.
   - `plane_role_api_docker_labels_custom`: `{plane-toolchain.pages.source: <printed digest>}`.
   - `plane_role_api_docker_envs_custom`: `CE_PLANE_LIVE_URL` set to the internal Live URL.
3. Run `sb install mod-plane`.
4. Run `mise run owner:plane-pages:check`.

The direct `apply` route is for non-Saltbox hosts.

The extracted server payloads retain their original bytes, including comments, so existing deployment digests still match, except as noted below.

**PTC-13 migration note.** `ce_tools.py` changed (canonical UUIDs, SDK private-call guard, `FASTMCP_HOST`), so its payload digest changed. `pages_backend.py` is unchanged. A host with a pre-PTC-13 `ce_tools.py` receipt will report a source difference on `owner:plane-mcp-compat:check`; review, then re-apply. Revert still works with the neutral backup and receipt names.

`ce_tools.py` is not version-pinned to the Plane SDK because the SDK lives in the hosted image. It calls private SDK methods (`relations._get`, `pages._get`, `pages._post`, `pages._patch`) and now raises a clear `RuntimeError` naming a missing method. Retest after upgrading the image. Project, work item and target IDs are canonicalised with `str(UUID(x))` (lowercase, hyphenated) before building paths.

The MCP entry point binds to `FASTMCP_HOST` (bare host or address; default `0.0.0.0`, which the container needs to be reachable) and `FASTMCP_PORT` (default `8211`).

The Pages helper writes `<name>-before-plane-toolchain-pages`, `plane-toolchain.pages.source`, `plane-toolchain-pages:<digest16>` and `plane.api.plane_toolchain_pages`. The MCP helper writes `server.py.before-plane-toolchain-ce` and `plane-toolchain-ce-receipt.json`. [ADR 0001](decisions/0001-neutral-names.md) sets these names; see [Hosts with legacy-named state](#hosts-with-legacy-named-state). Do not rename them outside that ADR.

**PTC-7 migration note.** The Pages helper now runs `docker update --restart=no` on every retired container (`<name>-before-plane-toolchain-pages`, `<name>-pages-reverted-*`, `<name>-pages-failed-*`) and restores the live restart policy when one is promoted back. Retained containers from earlier deployments keep their old policy and may restart with the daemon beside the live API. Before relying on this, run `docker update --restart=no` once on each existing retained container.

Apply and revert restart services. Obtain deployment authority before running them. A retained rollback container must be reconciled before a later Pages upgrade; the helper refuses to discard it automatically. To upgrade after the Pages source changes, run revert, then apply: revert accepts a derived container built from older source, and apply then builds the new image.

The remote helpers ignore SIGHUP and a closed SSH channel, so a dropped session does not stop an apply or its rollback halfway. The Pages and MCP helpers turn SIGTERM into exit 143 (one stderr line) through their rollbacks, so `kill` on the host program restores the previous state; a second SIGTERM during the rollback is ignored. A SIGKILL or reboot can still interrupt one. Re-run Pages apply to finish it: with no API container, apply restores the rollback container (using `unless-stopped` and saying so when its own policy was already retired to `no`); with a stopped derived container, apply retires it as `-pages-failed-*` and restores the original. It then applies normally. If the restored original fails to start after a stopped derived container was retired, the API stays down until the next apply. Any other layout with a rollback container is still refused. Re-run MCP apply or revert: its pending receipt accepts either entry point.

## Workspace slug rename

Owner tasks rename a Plane workspace slug in place on a live host. The full owner runbook (prerequisites, pre-live verification, failure handling, revert and the manual `pg_restore` last resort) is [Workspace slug rename runbook](workspace-rename.md). `check` is read-only and the safe default. `apply` and `revert` stop services, so obtain deployment authority first.

```sh
mise run owner:plane-workspace-rename:check
mise run owner:plane-workspace-rename:apply
mise run owner:plane-workspace-rename:revert
```

Required environment: `PLANE_RENAME_FROM` and `PLANE_RENAME_TO` (slugs: letters, digits, `-` and `_`; they must differ), `PLANE_MCP_SSH`, `PLANE_API_CONTAINER` and `PLANE_LIVE_URL`. Optional: `PLANE_RENAME_RECEIPT_DIR` (absolute path on the host), `PLANE_API_URL` and the service overrides `PLANE_DB_CONTAINER`, `PLANE_REDIS_CONTAINER`, `PLANE_WEB_CONTAINER`, `PLANE_SPACE_CONTAINER`, `PLANE_ADMIN_CONTAINER`, `PLANE_WORKER_CONTAINER`, `PLANE_BEAT_CONTAINER`, `PLANE_LIVE_CONTAINER` and `PLANE_MQ_CONTAINER`. Every value is validated before any SSH call, and errors never print configuration. The request, including the Django snippet, goes to the host on stdin as JSON; nothing but the fixed program is in the SSH arguments. The remote exit code is the task's exit code. Ctrl-C locally prints guidance (the host program may still be running; check the receipt, then rerun to resume or run revert) and exits 130.

## Hosts with legacy-named state

The helpers read and write only neutral names. They refuse legacy-named state: a Pages rollback container, a Pages source label on the API container or the neutral backup container, or an MCP backup or receipt, under the old names. Pages `check`, `apply` and `revert`, and MCP `check`, `apply` and `revert`, refuse before any change. Pages `build` only creates an image and does not look for legacy state. The refusal names what it found, never content:

```text
Legacy-named state found: <names>. This release no longer reads it. Migrate first with plane-toolchain at commit 1cb6153: Pages revert (apply only if check passes there), MCP apply. Or reconcile manually.
```

To upgrade such a host, check out commit `1cb6153` and run these owner tasks there, then use this release:

- Pages: run `revert` (it restores the original API container, consumes the legacy backup and parks the legacy derived container as an inert `<api>-pages-reverted-<ts>`). Run `apply` there only if `check` passes there. At that commit `apply` refuses when the deployed source digest differs from the shipped one, which is expected on a host deployed before the later payload changes.
- MCP: run `apply` (it migrates the receipt and backup to the neutral names). MCP `revert` at that commit does not migrate.

Then:

- After the Pages `revert` at `1cb6153`, run Pages `apply` with this release.
- Retired `<api>-pages-reverted-*` containers keep the legacy label. They are not checked and are left for owner cleanup.
- A Saltbox host's next build, from this release or the Saltbox role (PTC-53), must write the neutral label, or every operation from this release refuses there.

You can also reconcile the state by hand.
