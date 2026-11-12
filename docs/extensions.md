# CE extension contract and deployment

The extensions augment the official Plane MCP server. They reuse its API-key authentication and workspace context. They do not replace the official tools or grant additional project access.

## Shipped capabilities

| Tool | Operations | Limits |
| --- | --- | --- |
| `ce_workitem_relation` | Definitions, list, create built-in dependencies | Additive, same project, 1–100 distinct targets; no deletion or custom definitions |
| `ce_page` | List, retrieve, create project Pages | No editing or deletion of existing collaborative documents |

Relations normalise CE `issue_id` objects into the MCP dependency contract. Each source and target is checked through the project-scoped work-item API before creating a relation.

Pages require both the MCP extension and the backend adapter. The backend keeps Plane authentication, permissions, sanitisation and throttling. It checks project membership and private-page visibility. Creation converts HTML through Plane Live into consistent JSON and binary document formats before writing in a transaction. Conversion failure writes nothing. List requests fail above 1,000 Pages rather than silently truncating results.

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

The MCP helper rewrites `server.py` in place (truncate, write, flush, fsync) so the bind-mounted inode survives. The rewrite is not atomic: a crash mid-write leaves a partial file, and the backup `server.py.before-terrainlink-ce` is the manual recovery copy. After the restart it polls the container (running, not restarting, healthy when a healthcheck exists, restart count stable) for up to 45 seconds. On timeout or a crash loop it restores the previous bytes, restarts again and fails. A pending receipt (`pending`, `previous`) is written atomically before the rewrite and replaced by the final receipt once healthy. A rollback restores the old receipt text exactly, or deletes it on a first apply. After a killed deploy, `revert` and `apply` accept either the installed or previous digest while the receipt is pending, and `apply` repairs a stale receipt. A leftover `terrainlink-ce-receipt.json.tmp` is harmless and is overwritten by the next write. Without a healthcheck, the container must stay running for about 5 seconds; a slow healthcheck (long interval or start_period) may need more than the default 45 attempts. Original-versus-installed detection compares SHA-256 digests with the receipt; with no recorded original it accepts only a plain entry point that references `get_header_mcp` and defines no functions or classes.

A check fails when the installed source differs. The MCP helper refuses tracked or unexpected host entry points, preserves a backup and checks receipts before overwriting. The Pages helper builds a derived image from the running immutable image, preserves opaque Docker configuration and keeps the original container for rollback. It rejects auto-removal and static IP configurations requiring a specific migration. It checks backend HTTP health; verify authenticated MCP operations separately after applying.

The extracted server payloads retain their original bytes, including comments, so existing deployment digests still match, except as noted below.

**PTC-13 migration note.** `ce_tools.py` changed (canonical UUIDs, SDK private-call guard, `FASTMCP_HOST`), so its payload digest changed. `pages_backend.py` is unchanged. A host with a pre-PTC-13 `ce_tools.py` receipt will report a source difference on `owner:plane-mcp-compat:check`; review, then re-apply. Receipt and backup names are unchanged, so revert still works.

`ce_tools.py` is not version-pinned to the Plane SDK because the SDK lives in the hosted image. It calls private SDK methods (`relations._get`, `pages._get`, `pages._post`) and now raises a clear `RuntimeError` naming a missing method. Retest after upgrading the image. Project, work item and target IDs are canonicalised with `str(UUID(x))` (lowercase, hyphenated) before building paths.

The MCP entry point binds to `FASTMCP_HOST` (bare host or address; default `0.0.0.0`, which the container needs to be reachable) and `FASTMCP_PORT` (default `8211`).

Legacy `terrainlink` backup names, receipt names, image labels and backend module paths are compatibility metadata, not a dependency on TerrainLink. The helpers still write them, because existing rollback state depends on them. [ADR 0001](decisions/0001-neutral-names.md) sets the neutral replacements and the migration: read both names, write only neutral names, refuse when they disagree, and remove legacy reads after a deprecation window. Do not rename them outside that migration.

**PTC-7 migration note.** The Pages helper now runs `docker update --restart=no` on every retired container (`<name>-before-terrainlink-pages`, `<name>-pages-reverted-*`, `<name>-pages-failed-*`) and restores the live restart policy when one is promoted back. Retained containers from earlier deployments keep their old policy and may restart with the daemon beside the live API. Before relying on this, run `docker update --restart=no` once on each existing retained container.

Apply and revert restart services. Obtain deployment authority before running them. A retained rollback container must be reconciled before a later Pages upgrade; the helper refuses to discard it automatically. To upgrade after the Pages source changes, run revert, then apply: revert accepts a derived container built from older source, and apply then builds the new image.

The remote helpers ignore SIGHUP and a closed SSH channel, so a dropped session does not stop an apply or its rollback halfway. A SIGKILL or reboot can still interrupt one. Re-run Pages apply to finish it: with no API container, apply restores the rollback container (using `unless-stopped` and saying so when its own policy was already retired to `no`); with a stopped derived container, apply retires it as `-pages-failed-*` and restores the original. It then applies normally. If the restored original fails to start after a stopped derived container was retired, the API stays down until the next apply. Any other layout with a rollback container is still refused. Re-run MCP apply or revert: its pending receipt accepts either entry point.
