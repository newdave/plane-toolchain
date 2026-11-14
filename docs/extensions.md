# CE extension contract and deployment

The extensions augment the official Plane MCP server. They reuse its API-key authentication and workspace context. They do not replace the official tools or grant additional project access.

## Shipped capabilities

| Tool | Operations | Limits |
| --- | --- | --- |
| `ce_workitem_relation` | Definitions, list, create built-in dependencies | Additive, same project, 1–100 distinct targets; no deletion or custom definitions |
| `ce_intake` | Decide an intake record: reject (-1), snooze (0), accept (1), duplicate (2) | `decide` only; `workitem_id` is the record's issue id; snooze needs `snoozed_till`, duplicate needs `duplicate_to`; CE applies it for project admins only |
| `ce_page` | List, retrieve, create, update project Pages | Update replaces the whole body; needs `expected_updated_at` and `force=true`; no deletion |

Relations normalise CE `issue_id` objects into the MCP dependency contract. CE relations are workspace-wide, so `list` may return issue ids from other projects. Each row keeps its own `project_id`; rows are not filtered, only ids are exposed (no content), and reading the issue still needs that project's membership. Each source and target is checked through the project-scoped work-item API before creating a relation.

`ce_intake` exists because the official intake update calls `PATCH .../intake-issues/<id>/status`, which Plane CE v1.4.3 does not route (404). It sends the top-level `status`, `snoozed_till` or `duplicate_to` to the routed `PATCH .../intake-issues/<id>/` (`IntakeIssueUpdateSerializer`, read at tag `v1.4.3`), after checking the record is readable. CE applies those fields only when the caller's project role is above member; for others the request succeeds without effect, so the tool rereads the record and fails if the status did not change. Accepting moves a triage item to the project default state.

Pages require both the MCP extension and the backend adapter. The backend keeps Plane authentication, permissions, sanitisation and throttling. It checks project membership and private-page visibility.

Permission model, aligned with Plane CE v1.4.3 (`apps/api/plane/app/permissions/page.py` `ProjectPagePermission`, and `apps/api/plane/app/views/page/base.py` `PagesDescriptionViewSet.partial_update` and `PageViewSet.retrieve`/`list`, read at tag `v1.4.3`):

- The view keeps `IsAuthenticated` and Plane's own `ProjectPagePermission`; startup fails if `PageViewSet.permission_classes` is not exactly that. The caller must be an active project member.
- Create (POST) and update (PATCH) need project role admin or member. Guests may read only.
- A page owner passes the object check. A non-owner passes only on a public page (`access=0`); another user's private page is denied (403 from the permission class, 404 from the view).
- So any admin or member can update any public page, whoever owns it. This matches Plane's own description update, which filters on `owned_by=user OR access=0`.
- A guest without `guest_view_all_features` sees only pages they own (Plane's retrieve and list rule). The view applies the same filter.
- Differences from Plane, all stricter: the view also refuses an archived project (404), and update requires `force=true` and `expected_updated_at`. Locked and archived pages are refused as in Plane. Creation converts HTML through Plane Live into consistent JSON and binary document formats before writing in a transaction. Conversion failure writes nothing. List requests fail above 1,000 Pages rather than silently truncating results.

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

The update returns as soon as the transaction commits, so a worker is never held for a Live debounce. The API call takes about 1 second plus conversion time. The 200 response is the page detail plus a `late_store_check` object:

- `recheck_after`: informational ISO 8601 time, 12 seconds after the commit (one Live debounce plus margin). Clients should wait `seconds` rather than use their own clock.
- `seconds`: 12.
- `compare`: `description_html`.
- `html_sha256`: SHA-256 of the stored HTML just written.
- `on_difference`: what a mismatch means.

Detection happens in the `ce_page` MCP tool, not the API. `ce_page` is async: after a successful PATCH it waits the hinted `seconds` (the SDK calls run in threads, so the event loop is never blocked), retrieves the page and compares the SHA-256 of `description_html` with `late_store_check.html_sha256`. It returns the update response plus `late_store: "unchanged"`. If the digest differs, Live's late store replaced the body (the write and its version stay) and the tool fails with "Page <id> was overwritten by an open editor after the update; retrieve it and retry", as the old 409 did. The wait is capped at 60 seconds. A backend payload without `late_store_check` is returned unchanged.

Other clients of the REST route must wait `seconds` after the response (not compare `recheck_after` against their own clock), then retrieve and compare the digest. A mismatch can in rare cases be benign (a legitimate edit by someone else after the update): compare the content, and retry once if the update is missing. The recheck cost is that the MCP call takes about 13 seconds plus conversion time, as before, but no API worker is held. A store after the window goes undetected, as before.

Missing or invisible pages get 403 from Plane's `ProjectPagePermission` before the view runs, as for retrieve. The view's own 404 is defence in depth.

### Pages backend fixes (PTC-69, PTC-70, PTC-71)

`pages_backend.py` and `ce_tools.py` changed, so both payload digests changed. PTC-69: non-object request bodies get 400. PTC-70: update returns at once with `late_store_check` and the MCP tool does the recheck. PTC-71: permission tests and docs; `ce_tools.py` docstring only. Redeploy both through the owner tasks with deployment authority: for Pages run revert, then apply; also apply the MCP compat payload. Update behaviour needs both payloads.

Why: Live keeps an open document in memory and stores it with a 10-second debounce. It never re-reads the database, so a database write while the document is open is overwritten. `force_close` is the only external way to disconnect its editors; Live then stores and unloads the document.

Risks:

- Edits typed in the last moments before the disconnect may be lost. Usually Live stores them first, the body changes and the update is refused with 409. A store arriving after the lock overwrites the new body; the recheck after 12 seconds reveals it, but a store after that window goes unreported and a client that skips the recheck never sees it.
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
mise run owner:plane-pages:prune
```

`prune` (needs `PLANE_MCP_SSH` and `PLANE_API_CONTAINER`, not `PLANE_LIVE_URL`) is a dry run by default: it lists what it would remove. Set `PLANE_PAGES_PRUNE=yes` to delete. Only a stopped container that carries the Pages source label (a derived container) and is named exactly `<api>-pages-reverted-<digits>` or `<api>-pages-failed-<digits>` (optionally with further `-<digits>`) qualifies. It never removes the live API, the rollback container `<api>-before-plane-toolchain-pages`, the newest `<api>-pages-reverted-<digits>` container (a revert killed midway needs it to restore the API and its restart policy; remove it by hand with `docker rm` once the API is healthy), a running container, an `-aside` parking name or anything unlabelled, because an unlabelled container may be the upstream original. It skips a container that vanishes during the run and reports what it removed, kept running or could not remove. Apply and revert list the retained container names in their result. A failed `docker update --restart` on a retained container prints a warning on stderr. A failed `docker build` reports the exit code and the last five stderr lines with the base image reference and its repository path replaced by `<base image>` and any `lookup <host>` DNS host by `lookup <host>`; the build context holds no runtime configuration. Container creation uses a 30 second socket timeout and names only the HTTP status or error kind on failure. The health wait stops after 45 attempts or 120 seconds, whichever comes first, and each probe `docker exec` times out after 20 seconds.

A `revert` killed after the original took back the `<api>` name but before it started leaves `<api>` stopped. A repeat `revert` starts it (restoring the derived container's live restart policy, or the unless-stopped default with a notice) only when a `<api>-pages-reverted-<digits>` container exists as evidence of the interrupted revert. A stopped `<api>` without one is treated as stopped on purpose and left alone.

**Compose-managed hosts. Warning: run `revert` before any `docker compose up` or `down` of the API service, and before any other orchestrator (Saltbox, Ansible and the like) recreates the API container by name. They have the same hazard. Apply a Compose or image update only while Pages is reverted, then run `apply` again.** Docker labels cannot be changed on an existing container, so the retained original (`<api>-before-plane-toolchain-pages`) keeps its `com.docker.compose.*` labels, and the derived container carries the same ones plus `plane-toolchain.pages.source`. Verified against Docker Compose v5.5.0 on a scratch project (a service container swapped as the helper does):

- `docker compose up -d` stops and removes the derived container and starts the retained original under its backup name. Pages is gone and the API runs on the upstream image. This is the safer outcome: with the compose labels stripped from the derived container, `up` instead started the original beside it, two API containers. Run `apply` again to recover; it promotes the running original back to the API name and re-derives.
- `docker compose down` removes the retained original too, because its labels match the project. Run `revert` (or `check`) first if you need the rollback container to survive.

Possible follow-up, not done: a Compose override that pins the API `image:` to the derived image would let `up` keep Pages. Tests pin the label sets (`tests/test_pages_deploy.py`, PTC-67) so any change is deliberate. Not verified: a Saltbox-managed host, or recovery by `apply` against a real compose-started original.

The MCP helper rewrites `server.py` in place (truncate, write, flush, fsync) so the bind-mounted inode survives. The rewrite is not atomic on purpose: `os.replace` would give `server.py` a new inode that the container may never see. The backup `server.py.before-plane-toolchain-ce` and the receipt are written atomically (temporary file, fsync, `os.replace`, directory fsync), and the backup is complete before `server.py` is touched. A crash mid-write leaves `server.py` empty or a prefix of the new entry point (or, in a killed rollback or revert, of the backup). `apply` and `revert` recognise exactly that state and carry on from the backup: a prefix of the new bytes while the receipt is pending for them, or a prefix of the backup while the receipt is `pending` or marked `reverting`. `revert` writes the `reverting` marker (a copy of the final receipt) atomically before its rewrite and puts the unmarked receipt back afterwards. With a final receipt a shortened `server.py` is a host edit and is refused, as is any foreign content. The receipt files are now created 0600. A rollback after such a recovery restores the backup, not the partial file. After the restart it polls the container (running, not restarting, healthy when a healthcheck exists, restart count stable) for up to 45 seconds. On timeout or a crash loop it restores the previous bytes, restarts again and fails. A pending receipt (`pending`, `previous`) is written atomically before the rewrite and replaced by the final receipt once healthy. A rollback restores the old receipt text exactly, or deletes it on a first apply. After a killed deploy, `revert` and `apply` accept either the installed or previous digest while the receipt is pending, and `apply` repairs a stale receipt. A leftover `plane-toolchain-ce-receipt.json.tmp` is harmless and is overwritten by the next write. Without a healthcheck, the container must stay running for about 5 seconds; a slow healthcheck (long interval or start_period) may need more than the default 45 attempts. Original-versus-installed detection compares SHA-256 digests with the receipt; with no recorded original it accepts only a plain entry point that references `get_header_mcp` and defines no functions or classes.

The MCP helper writes and reads only `server.py.before-plane-toolchain-ce` and `plane-toolchain-ce-receipt.json`. It refuses legacy-named state; see [Hosts with legacy-named state](#hosts-with-legacy-named-state). It writes the receipt atomically and moves nothing between names. A failed health check rolls the entry point back. `revert` restores `server.py.before-plane-toolchain-ce`, verified against the receipt digest.

A check fails when the installed source differs. The MCP helper refuses tracked or unexpected host entry points, preserves a backup and checks receipts before overwriting. The Pages helper builds a derived image from the running immutable image, preserves opaque Docker configuration and keeps the original container for rollback. It rejects auto-removal and static IP configurations requiring a specific migration. It checks backend HTTP health; verify authenticated MCP operations separately after applying.

### Saltbox route

A Saltbox role owns the API container and pins its image, labels and environment in the inventory, so the direct `apply` route (which swaps the container) does not fit. It also refuses a local running image. Use the build-only task instead. Obtain deployment authority first.

1. Run `mise run owner:plane-pages:build` (needs `PLANE_MCP_SSH`; optional `PLANE_WORKER_CONTAINER`, default `plane-worker`, and `PLANE_BACKEND_IMAGE`). It derives `makeplane/plane-backend:<tag>` from the worker image unless `PLANE_BACKEND_IMAGE` is set, builds from that image's registry digest and prints `<image tag> <digest>`; the digest covers `pages_backend.py`, the create-body function and the Dockerfile. It refuses when the upstream image is missing or has no registry digest. It never creates, stops, renames or removes a container.
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

The remote helpers ignore SIGHUP and a closed SSH channel, so a dropped session does not stop an apply or its rollback halfway. The Pages and MCP helpers turn SIGTERM into exit 143 (one stderr line) through their rollbacks, so `kill` on the host program restores the previous state. SIGTERM is blocked for the duration of every failure-triggered rollback (Pages apply and revert, MCP write and health failures): one that arrives meanwhile is delivered after the rollback finishes and the failure is reported on stderr, and the program then exits 143. A child `docker` process started during a rollback inherits the blocked mask on purpose: a signal sent to the whole process group cannot cut a rollback command short. A SIGKILL or reboot can still interrupt one. Re-run Pages apply to finish it: with no API container, apply restores the rollback container (using `unless-stopped` and saying so when its own policy was already retired to `no`); with a stopped derived container, apply retires it as `-pages-failed-*` and restores the original. It then applies normally. If the restored original fails to start after a stopped derived container was retired, the API stays down until the next apply. Any other layout with a rollback container is still refused. Re-run MCP apply or revert: its pending receipt accepts either entry point.

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
