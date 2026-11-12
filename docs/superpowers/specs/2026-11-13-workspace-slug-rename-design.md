# Workspace slug rename: design

- **Date:** 2026-11-13
- **Status:** Draft, awaiting owner approval
- **Target:** Plane CE v1.4.3 (the version the CE extensions are tested against)

## Intent

The owner wants to change a workspace's URL slug (`https://<host>/<slug>/...`) on their
live Plane CE instance. Plane CE has no UI for this. The owner first asked for
whole-instance export/import mise tasks. Source research showed that an in-place rename is
possible and much smaller: every relation and every stored file key uses the workspace UUID,
not the slug. The owner chose the in-place route.

Existing links inside Plane content must keep working after the rename (owner decision). A
short outage during the rename is acceptable (owner decision).

## Goals

- Owner-only mise tasks to check, apply and revert a rename of one workspace slug on one
  Plane CE instance.
- Rewrite stored links that point at the old slug, so they resolve after the rename.
- A rollback that restores the exact previous state of every record touched.
- The same safety style as the existing deploy helpers: read-only check first, receipts,
  survives SSH disconnects, refuses ambiguous state, never prints runtime configuration.

## Non-goals

- Whole-instance export/import or migration to another host.
- Renaming more than one workspace in one run.
- Rewriting version history (stored copies of earlier work item and Page versions).
- Fixing systems outside Plane: webhook receivers, bookmarks, emails already sent. The
  check reports them; the owner updates them.
- A redirect from old URLs. See "Left to the builder".

## Anti-goals (a failure even if the rename works)

- Any rewritten record that cannot be restored byte for byte by revert.
- Rewriting text that is not a link to this workspace (bare words, other workspaces whose
  slug shares a prefix, other hosts).
- An open editor or background job writing the old slug back after the rewrite.
- Running against the live host without explicit owner authority.
- Printing secrets, database URLs or other runtime configuration.

## Constraints

- AGENTS.md: owner tasks only, explicit authority before any live run, credentials from the
  environment only, private identities and endpoints kept out of public files.
- Remote code follows `src/plane_toolchain/remote.py`: stdlib-only functions assembled by
  `program()` and run as `python3 -c` over SSH, with the SIGHUP/SIGPIPE preamble.
- Plane-side work (validation, rewrites, cache) runs inside the API container through
  `docker exec <api> python manage.py shell`, so it uses Plane's own models and settings.
  The shipped Django snippet is fixed source in this repo, like `pages_backend.py`.
- Saltbox: nothing here may change container definitions. Services are stopped and started
  with `docker stop` and `docker start` (see "Builder check resolutions", item 1).

## What the research found (Plane CE v1.4.3)

- `db/models/workspace.py:136`: `slug = SlugField(max_length=48, db_index=True,
  unique=True, validators=[slug_validator])`. The unique index includes soft-deleted
  workspaces, whose slugs become `<slug>__<epoch>`.
- `utils/constants.py:5`: `RESTRICTED_WORKSPACE_SLUGS`. The serializer regex is
  `^[a-zA-Z0-9_-]+$`.
- Foreign keys and `last_workspace_id` use UUIDs. Asset keys are `f"{workspace.id}/..."`
  (`app/views/asset/v2.py:390,619,845`). No files move.
- The slug is stored as text in work item and Page `description_html`, and in the editor
  document `description_binary` (`db/models/issue.py:140`, `db/models/page.py:33`). Work
  item embeds store `workspace_identifier="<slug>"`.
- It is also stored in comment HTML, and in notification `data` JSON (`workspace_slug`,
  `bgtasks/notification_task.py:421,496,549`).
- Cache keys include the request path (`utils/cache.py:14-40`). Cached payloads of
  `/api/workspaces/` and `/api/users/me/workspaces/` contain the slug.
- Routes resolve the workspace by slug only. There is no redirect.
- Plane Live converts HTML to an editor document at `/live/convert-document/`. This repo's
  Pages backend already uses that route (`pages_backend.py:141`).

## The tasks (owner decisions and my calls)

`OLD` and `NEW` come from the environment (`PLANE_RENAME_FROM`, `PLANE_RENAME_TO`), like
the other owner tasks. Host and container names reuse the existing variables
(`PLANE_MCP_SSH`, `PLANE_API_CONTAINER`, `PLANE_LIVE_URL`) plus new ones for the database
and the services to stop. *My call.*

### `owner:plane-workspace-rename:check` (read-only)

- Validates `NEW`: regex, at most 48 characters, not restricted, not used by any workspace
  including soft-deleted ones. Validates that `OLD` exists and is not deleted.
- Counts and lists, per record ID and field, every planned rewrite (see "Matching").
- Reports what it will not fix: version history rows, webhooks with active receivers, and
  the reminder to update `PLANE_WORKSPACE` in `config/local.env` and the GitHub examples'
  `vars.PLANE_WORKSPACE`.
- Reports the Saltbox detection result.
- Exits non-zero if apply would refuse.

### `owner:plane-workspace-rename:apply`

Order matters. The slug changes before the content rewrite, so any client still using the
old slug gets 404s and cannot write the old slug back.

1. Re-run every check. Refuse on any failure, or if a receipt for another rename exists.
2. Write a pending receipt (atomic write, as in `mcp_deploy.py`).
3. Stop the web, space, admin, worker and beat-worker services, and restart Live to drop
   open editor sessions. *My call:* the API, database, Redis and Live stay up, because the
   rewrite needs them.
4. Take a database backup with `pg_dump` inside the database container, written to a
   host path outside any web root. Record its path and SHA-256 in the receipt. Refuse to
   continue if the backup fails.
5. In one database transaction: change the slug, and rewrite the comment HTML and the
   notification JSON. Store each record's original values in the receipt first.
6. Work items and Pages: rewrite `description_html`, then rebuild `description_binary` from
   the new HTML through Live's convert route. Store each record's original HTML,
   editor JSON and editor document (base64) in the receipt first. Only records that contain
   a match are rebuilt.
7. Delete the cache keys for both slugs, plus `/api/workspaces/` and
   `/api/users/me/workspaces/`.
8. Start the stopped services in reverse order. Health check: the API answers, `NEW` resolves
   through the API, and `OLD` returns 404.
9. Mark the receipt complete.

A kill at any step must leave a state that `apply` (resume) or `revert` accepts. The pending
receipt records which step finished.

### `owner:plane-workspace-rename:revert`

- Uses the receipt to restore the slug and every touched field to its stored original value,
  byte for byte. It does not reconvert anything.
- Uses the same stop, cache-flush, start and health-check steps.
- Restoring the `pg_dump` backup is a documented manual last resort, not automatic.

## Matching

Rewrite only these forms, where `OLD` is followed by `/`, `"`, `'`, `?`, `#` or the end of
the value:

- an absolute URL on the instance's own host: `https://<host>/OLD/...`. The host comes from
  Plane's configured web URL, read inside the API container;
- a root-relative link: `href="/OLD/...`;
- the embed attribute `workspace_identifier="OLD"`;
- the notification JSON key `workspace_slug` with the value `OLD`.

Everything else stays unchanged, including other hosts and slugs that start with `OLD`.

## Risks

- Rebuilding an editor document from HTML can drop details the HTML does not carry. This is
  limited to matching records, `check` reports the count, and revert restores the original
  document.
- Old bookmarks, emails already sent and invite links break. There is no redirect in CE.
- A new workspace could later take the old slug and capture stale external links.
- The receipt holds copies of content. It must stay on the host, owner-readable only
  (mode 0600), and never be printed.

## Left to the builder

- Exact environment variable names for the database container, the backup directory and the
  list of services to stop. Defaults should match the official CE compose names.
- How the Django snippet is shipped: a fixed module piped to `manage.py shell`, following
  the `pages_backend.py` pattern.
- Receipt layout and its file name. It needs a neutral name (see ADR 0001).
- An optional reverse-proxy redirect from `/OLD/...` to `/NEW/...`: documented as a manual step
  in `docs/extensions.md`, not automated. *My call.*
- Tests: FakeDocker and a fake database or ORM layer for check, apply, revert and a kill at
  every apply step; matcher unit tests including prefix collisions and other hosts.

## Builder check resolutions (all my calls)

A reviewer read this document as the builder. Answers:

1. **Saltbox.** PTC-23 detection is scope only; nothing is built yet. The rename does not
   depend on it. Stopping and starting uses `docker stop` and `docker start` on both plain
   Docker and Saltbox hosts, because neither changes a container definition. `check` reports
   whether the host looks Saltbox-managed, for information only.
2. **Scope of rewrites.** Rewrite only these: work item descriptions, Page descriptions,
   comment HTML and notification `data`. In addition, `check` scans every text and JSON
   column in Plane's tables for the old-slug link forms. It reports hits outside that set
   (for example webhook logs or intake) without rewriting them. History tables (versions,
   activity, webhook logs) are never rewritten. **Celery:** stop beat first, wait until the
   queue is empty or 60 seconds pass, then stop the worker. Report any jobs still queued,
   and never purge them. Emails and webhooks sent after the restart use the new slug.
3. **Receipt storage.** The receipt is a host directory (owner-only, mode 0700, with 0600
   files): `receipt.json` holds the step state, and `records.jsonl` holds one line per
   record with its original values. The Django snippet writes nothing to disk. It streams
   JSON lines on stdout from `docker exec`, and the remote program writes them to the host
   with fsync. The design works for any data size.
4. **Editor documents.** Work items use the Live variant `rich` and Pages use `document`.
   Both return `description_json` and `description_binary`, so rebuild both, plus
   `description_stripped`. Write with queryset `.update()`, so no versions, activity,
   signals or `updated_at` change, and revert restores the original bytes exactly.
5. **Deleted and archived records.** Include them, using the unfiltered manager
   (`all_objects` or the equivalent). A restored item should not come back with a broken
   link.
6. **Resume.** Each record's original values are written to `records.jsonl` before that
   record is rewritten. Resume re-derives the remaining matches from the database. Revert
   accepts partly rewritten state: it restores every journalled record and leaves the rest
   untouched.
7. **Backup.** `docker exec <db> sh -c 'pg_dump -Fc -U "$POSTGRES_USER" "$POSTGRES_DB"'`
   uses the container's own environment, and the remote program streams stdout to a host
   file (mode 0600) in the receipt directory. No credentials pass through the toolchain or
   are printed.
8. **Health check.** Inside the API container: `/api/instances/` answers, and the shell
   confirms a workspace with slug `NEW` exists and none with slug `OLD`. After the run,
   the owner updates `PLANE_WORKSPACE` and runs one authenticated read through the MCP
   bridge locally. The docs list this as the final manual step.
