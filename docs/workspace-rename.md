# Workspace slug rename runbook

Owner runbook for renaming a Plane CE workspace slug in place on a live host. The tasks are owner-only: `apply` and `revert` stop services, so get explicit deployment authority first. Run them only with the slugs and host you were authorised for. `check` is read-only and safe to run first.

Every identity below is a placeholder. Keep real hosts, container names, slugs and receipt paths out of public files.

## Contents

- [What the rename does](#what-the-rename-does)
- [Prerequisites and environment](#prerequisites-and-environment)
- [Verify before any live run](#verify-before-any-live-run)
- [Check](#check)
- [Flush cache dry run](#flush-cache-dry-run)
- [Apply](#apply)
- [Revert](#revert)
- [Known risks](#known-risks)
- [Last resort: manual pg_restore](#last-resort-manual-pg_restore)
- [After the run](#after-the-run)
- [Saltbox hosts](#saltbox-hosts)

## What the rename does

Plane stores every relation and asset key against the workspace UUID, so only the slug and links to it change. Apply changes the workspace slug first, then rewrites stored links to the old slug in work item and Page descriptions, comment HTML, descriptions linked from comments and notification data. Version history, activity and webhook logs are never rewritten. Work items and Pages get their editor documents rebuilt through Plane Live. Plane is unavailable to users while services are stopped.

Apply writes a receipt directory on the host (default `~/.local/state/plane-toolchain/workspace-rename` of the SSH user, mode 0700, files 0600):

- `receipt.json`: the step, phase and rotation record.
- `records.jsonl`: the journal, one line per rewritten row with its original values, written before the row changes. A kill mid-append can leave a torn last line; the next append repairs it first, so it never corrupts the record that follows.
- `run.lock`: the lock file that keeps two runs apart.
- `backup.dump`: a `pg_dump -Fc` of the Plane database, with its SHA-256 in the receipt.

The receipt directory holds copies of workspace content. Keep it owner-only, never print or share it, and never delete it.

## Prerequisites and environment

- SSH access to the Plane host, and Docker on that host. The rename uses only `docker stop`, `start`, `restart`, `exec` and read-only `inspect`; it never changes a container definition.
- A recent, separate backup of the host, taken before the first apply.
- Explicit owner authority for the outage window.

| Variable | Use |
| --- | --- |
| `PLANE_RENAME_FROM` | Current slug (letters, digits, `-`, `_`). |
| `PLANE_RENAME_TO` | New slug. Must differ from the old one. |
| `PLANE_MCP_SSH` | SSH destination of the Plane host. |
| `PLANE_API_CONTAINER` | API container name. |
| `PLANE_LIVE_URL` | Live URL reachable from the API container, used to rebuild editor documents. |
| `PLANE_RENAME_RECEIPT_DIR` | Optional absolute path for the receipt directory on the host. |
| `PLANE_API_URL` | Optional API base URL, as seen inside the API container, for the health probe (default `http://127.0.0.1:8000`). |
| `PLANE_DB_CONTAINER` | Optional, default `plane-db`. |
| `PLANE_REDIS_CONTAINER` | Optional, default `plane-redis`. |
| `PLANE_WEB_CONTAINER` | Optional, default `plane`. |
| `PLANE_SPACE_CONTAINER` | Optional, default `plane-space`. |
| `PLANE_ADMIN_CONTAINER` | Optional, default `plane-admin`. |
| `PLANE_WORKER_CONTAINER` | Optional, default `plane-worker`. |
| `PLANE_BEAT_CONTAINER` | Optional, default `plane-beat-worker`. |
| `PLANE_LIVE_CONTAINER` | Optional, default `plane-live`. |
| `PLANE_MQ_CONTAINER` | Optional, default `plane-mq` (RabbitMQ). |

The defaults are the Saltbox container names. On other hosts set every override that differs. Values are validated before any SSH call and never printed. The request, including the Django snippet, goes to the host on stdin; the remote exit code is the task's exit code. Ctrl-C locally prints guidance and exits 130, but the host program may still be running: check the receipt, then rerun the same task to resume, or run revert.

The three tasks:

```sh
mise run owner:plane-workspace-rename:check
mise run owner:plane-workspace-rename:apply
mise run owner:plane-workspace-rename:revert
```

## Verify before any live run

The code is tested against fakes, not a real Plane. Verify these against a Plane CE v1.4.3 container before the first apply. Treat any mismatch as a blocker.

`check` now probes the structural assumptions itself, read-only, through Django's `_meta`, and refuses (so apply refuses too) when one is wrong. For each model it checks the table name, that an unfiltered manager exists (`all_objects`, else the base manager) and that every field the snippet reads or writes exists with the expected kind: the link columns are text, the JSON columns are JSON stored as `jsonb`, the binary columns are binary, and `workspace_id` exists on every owned model (including `Page` and `Description`). Every mismatch is printed by model and field, never row content, for example `Model mismatch: page.workspace_id (missing)` or `Model mismatch: notification.data (db_type, expected jsonb, found json)`. A mismatch means the snippet was written for a different Plane: do not work around it, report it. The list below is what the probe cannot know.

The owner should run `mise run owner:plane-workspace-rename:check` once against the real v1.4.3 deployment (or a copy) and record the result: the probe has only been run against fakes.

- `EmailNotificationLog` exists (table `email_notification_logs`) and has no workspace column. The snippet attributes a row to the old workspace when its `data` names `workspace_slug` with the old slug. A reused old slug or a nested key could match unrelated logs, so prefer the top-level key.
- `Description.workspace_id` exists (table `descriptions`), and comments link to a `Description` row.
- The comment columns `comment_html`, `comment_json`, `comment_stripped` and `comment_binary` exist as the snippet expects.
- Whether Live's convert route returns `description_stripped`. Restore guards work items and Pages on `description_html` only.
- Description rows rewritten in the slug step are not rebuilt through Live, so their `description_binary` stays stale (counted as `not_rebuilt` in the summary). If such rows back editor content, a later editor save can bring the old slug back. Decide before apply whether that is acceptable.
- The API listens on port 8000 inside its container and `/api/instances/` answers 200 there. The health check fails on any other status, redirects included. Set `PLANE_API_URL` if the port differs.
- The RabbitMQ vhost the queue drain probes. It runs `rabbitmqctl list_queues` on `${RABBITMQ_DEFAULT_VHOST:-/}` inside `PLANE_MQ_CONTAINER` and needs a queue named `celery` there. If none is listed, stopping services fails with `celery queue not found; drain not verified`.
- The Redis cache database. `cache.clear()` on django_redis issues `FLUSHDB` and wipes the whole Redis database. Confirm that Plane's cache database index is not shared with anything else (rate limits, a broker or result backend, other applications).
- Whether Plane has JSON `null` in the rewritten JSON columns. A JSON-null original is restored as SQL NULL.

## Check

Run `check` first, and again after any change of environment. It is read-only: it writes nothing on the host, not even the receipt directory.

```sh
mise run owner:plane-workspace-rename:check
```

It confirms the services exist, loads any receipt, validates both slugs, plans every rewrite, runs the cache flush as a dry run and reports whether the host looks Saltbox-managed. It prints planned rewrites by table, id and column only, and hits it will not fix (`Not rewritten: N in table.column (reason)`): `other_workspace` for the same link forms in another workspace, `out_of_scope` for tables outside the rewrite scope such as webhook logs. Review those. Success ends with `Check passed: apply may run` and exit 0. A refusal prints `Refused: ...` and exits non-zero.

What each refusal means:

| Message | Meaning and action |
| --- | --- |
| `container not found for service <key>` | A container name is wrong. Set the matching `PLANE_*_CONTAINER`. |
| `Model mismatch: <model>.<field> (<reason>)` | The Plane models differ from what the snippet assumes (reasons `missing`, `kind`, `db_type`, `table`, `manager`). See [Verify before any live run](#verify-before-any-live-run). |
| `old_missing_or_deleted` | No live workspace has the old slug. |
| `new_slug_invalid` | The new slug breaks the format, is over 48 characters, is restricted or is already taken (soft-deleted workspaces keep their slug as `<slug>__<epoch>`). The detail says which. |
| `old_slug_taken`, `workspace_mismatch` | The slugs and the journalled workspace disagree. Find which workspace holds each slug before doing anything else. |
| `receipt belongs to a different rename` | A receipt for other slugs is in the directory. Do not mix renames. Move it aside only once you know that rename is finished. |
| `receipt is unreadable`, `receipt store path ...` | The receipt is corrupt, a symlink, loosely permissioned or foreign-owned. Fix by hand. |
| `receipt is in the revert phase; run revert` | A revert is unfinished. Rerun revert. |
| `this rename was reverted and the revert is complete; archive or move the receipt directory before a new apply` | See [After a finished revert](#after-a-finished-revert). |
| `found <files> without a receipt` | A journal or backup (current or rotated) from an earlier rename is present. Move it or the directory aside. Do not delete it. |
| `backup.dump is missing`, `backup.dump does not match the recorded SHA-256`, `backup.dump has no recorded SHA-256`, `receipt is past the backup but has no backup SHA-256` | The backup and the receipt disagree. Investigate before resuming. |
| `journal has a corrupt line`, `journal has no workspace record`, `journal names more than one workspace` | The journal is damaged or mixed. Stop and consider revert or the last resort. |
| `workspace_id_missing`, `live_url_missing`, `slug_not_renamed` | The step ran out of order or without its inputs. Rerun from `check`. |

If a receipt for the same rename exists, check says which step it is at and that apply resumes from there.

## Flush cache dry run

`check` runs the cache flush as a dry run. Read its line: `Cache not cleared (dry run); sessions kept in the cache: <value>`.

- `True`: sessions live only in the cache, so the real flush signs everyone out. Warn users that they must sign in again.
- `False`: sessions survive the flush (database, file or signed-cookie sessions).
- `unknown`: the session engine is not one the tool knows. Assume users must sign in again.

A `Sessions use cache alias ...` line means sessions use a non-default cache alias. Also confirm the Redis note above: the real flush clears the whole database, not just Plane's keys.

## Apply

Apply only with explicit owner authority and after a clean `check`.

```sh
mise run owner:plane-workspace-rename:apply
```

Every refusal runs before the first stop, so a refused apply changes nothing. Then it writes a pending receipt and steps through these states, each as an effect followed by recording the step:

| Step | What happened |
| --- | --- |
| `pending` | Receipt written. |
| `stopped` | Outage window starts. Beat stops, the queue drains for up to 60 seconds (leftover jobs are reported and never purged), then worker, web, space and admin stop. Live restarts to drop editor sessions. API, database, Redis and RabbitMQ stay up. |
| `backed_up` | Free space in the receipt directory is checked first (at least the database size, and 64 MiB). Then `pg_dump` to `backup.dump`, which must list with `pg_restore -l` inside the database container before it is kept; SHA-256 recorded. An empty, failed or unreadable dump is refused, and the failure carries a short stderr tail with quoted text redacted. |
| `slug_done` | One transaction: the slug, comment HTML, linked descriptions and notification data. Originals are journalled first. |
| `rows_done` | Work item and Page descriptions rewritten one row at a time, editor documents rebuilt through Live, originals journalled first. |
| `cache_done` | The cache is cleared (`FLUSHDB`). |
| `started` | Services start in reverse order. The outage window ends. |
| `complete` | Health: the API answers 200, the new slug is held by the journalled workspace and the old slug is held by none. |

The outage lasts from `stopped` to `started`, and grows with the number of rows to rebuild through Live.

Apply and revert hold an exclusive lock (`run.lock` in the receipt directory, mode 0600) for the whole run. A second run, for example a double start or a rerun while the host program of a dropped SSH session is still going, refuses before it stops anything with `Refused: another apply or revert holds the lock on the receipt directory`. Wait for the first run, or look on the host for it. The lock goes when the run ends or its process dies. `check` takes no lock.

A run that is already `complete` says `Rename already complete`. A rerun after a kill says `Resuming the rename from step <step>` and stops services again before continuing.

### Failures before slug_done

Message: `Rename failed before the slug change (<step>): ...`. No data changed. The tool restarts the services, resets the receipt to `pending` and sets the backup and journal aside as `backup-<UTC stamp>.dump` and `records-<UTC stamp>.jsonl` (kept, listed in the receipt). Fix the cause and rerun apply: it stops services and takes a fresh backup. A warning that services were not all restarted means start the remaining ones by hand.

If the host program was killed rather than failing, the receipt is kept and a backup whose SHA-256 matches is reused, unless a service ran again meanwhile (then it is set aside too).

If the slug step outcome is unclear, the tool probes the slugs read-only first. `Slug step unclear, but NEW is held by the journalled workspace; continuing` means it carried on. `Rename slug step outcome unknown` and `Rename resume failed at <step> ... slug state unknown` mean services were left as they are and nothing was reset: check which workspace holds each slug and which services run, then rerun apply or run revert.

### Failures after slug_done

There is no automatic rollback. Message: `Rename stopped at <step> (moving to <target>): ... Rerun apply to resume, or run revert; nothing is rolled back automatically.` Services stay stopped (the message says if some may still run or did not start). You decide:

- Resume (rerun apply) when the cause is fixable: Live down, a wrong `PLANE_LIVE_URL`, a service that failed to start. Resume re-derives the rows still to rewrite.
- Revert when you want the old state back, or the cause cannot be fixed.

`Rename health check failed: ...` means services are running and the receipt is left at `started`. Investigate, then rerun apply to recheck, or run revert.

Snippet errors such as `convert_failed`, `row_changed` (a row changed after it was read), `write_failed` or `cache_clear_failed` name only ids and error types.

### Hung or orphaned snippets

Each snippet runs in the API container with a per-run `PLANE_RENAME_RUN=<token>` in its environment. Stopping the local `docker exec` client does not stop that Python process, so after any abort the tool runs a small stdlib script in the container (`python -c`, reading `/proc`) that sends SIGTERM and then SIGKILL to the marked processes and confirms none is left. Only then does the failure continue to the usual handling (restart and reset before the slug change, or stay stopped after it).

- A snippet that prints nothing for 15 minutes is treated as hung and stopped: `<op> timed out after 900 s without output; the snippet process was stopped in the API container`. The timer restarts on every output line, so a long step that keeps answering is never cut off.
- `snippet process could not be confirmed stopped` means the kill could not be confirmed. Services are not restarted and the receipt is not reset. Find and stop the process by hand in the API container (its environment holds `PLANE_RENAME_RUN`), then rerun apply or revert.
- Apply and revert sweep first: any marked process left by an earlier killed run is stopped before the preflight, and the run refuses (`could not confirm that no earlier snippet is running`) if that cannot be confirmed. `check` never sweeps, because it takes no lock and could meet a live run's snippet.
- The kill script needs `python` and a readable `/proc` in the API container, as the Plane backend image has.

## Revert

Use revert to return to the old slug from any partial or complete apply. It restores the slug and every journalled field from `records.jsonl`; it never calls Live and never restores `backup.dump`.

```sh
mise run owner:plane-workspace-rename:revert
```

Steps: `reverting`, `restored` (services stop, the journal is restored in one transaction, the cache is cleared), `started`, `reverted` (health with the slugs swapped). A kill at any point is resumable: rerun revert (`Resuming the revert from step <step>`). A `Revert stopped at <step>: ...` message says whether services are stopped. Fix the cause and rerun.

Refusals come before the first stop: no receipt, a receipt for another rename, an unknown receipt state, a journal without a workspace record, an empty journal past the slug change (restore `backup.dump` by hand), or an unclear slug state. If the slug change never committed, or nothing was journalled, revert says no restore is needed and only checks health.

### Partial restore

Rows that changed after the rename are not overwritten. The tool prints each skipped row as `Skipped <table> <id> (<reason>)`, restarts services and ends with `Revert incomplete: the restore was partial (<n> rows skipped ...)` and a non-zero exit. The receipt stays at `reverting`. The skipped rows keep their NEW links. List them from the output, then either reconcile them by hand, or restore `backup.dump` (which loses every later edit). Rerunning revert rechecks them. The reason `changed` means the row holds neither the original nor the expected rewrite, for example after a `WEB_URL` change or a user edit.

### After a finished revert

Apply and check refuse until the receipt directory is archived. After `Revert complete: <new> is back to <old>`, move the whole receipt directory aside, keeping `backup.dump`, `records.jsonl` and any rotated files in the archive. Only then run a new apply. Do not delete anything.

## Known risks

- Journal durability: the snippet prints each journal line before it writes the row, and the host then fsyncs it. If the host program is killed with SIGKILL between the print and the fsync during `rows_done`, a row can be rewritten without a journal entry. Only the `pg_dump` backup recovers that row.
- JSON null and SQL NULL both read back as null. A JSON-null original is restored as SQL NULL. Check whether Plane has such rows in the rewritten columns.
- `WEB_URL` decides which absolute links count as this instance. If it changes between apply and revert, the restore guard sees rows as changed and skips them (a partial restore).
- Rebuilt editor documents: Live rebuilds the editor document from the new HTML and can drop details the HTML does not carry. Revert restores the stored originals and does not reconvert. Restore guards work items and Pages on `description_html` only.
- Stale `description_binary` on Description rows (see the verification list).
- Stale bookmarks, emails already sent and invite links to the old slug break. Plane CE has no redirect.
- A new workspace can later take the old slug and capture stale external links.
- The receipt directory holds copies of content. Keep it private.
- The cache clear signs users out when sessions are cache-only, and wipes the whole Redis database it targets.

## Last resort: manual pg_restore

Use only when revert cannot recover, for example an empty journal past the slug change, or a row lost to the journal-durability risk. A restore replaces the database with its state at the dump and loses every change since. Get explicit owner authority, stop the Plane services and any other writer to the database, and take a fresh dump of the current state first.

The files live in the receipt directory on the host (`PLANE_RENAME_RECEIPT_DIR`, else `~/.local/state/plane-toolchain/workspace-rename` of the SSH user):

- `backup.dump`: the current dump. Check its SHA-256 against `backup_sha256` in `receipt.json`.
- `backup-<stamp>.dump` and `records-<stamp>.jsonl`: rotated copies from earlier failures before the slug change.
- `records.jsonl`: the current journal.

Never delete any of them. Restore with `pg_restore` inside the database container, using that container's own `POSTGRES_USER` and `POSTGRES_DB` variables so no credentials are typed. For example, stream the file into `docker exec -i <db> sh -c 'pg_restore --clean --if-exists -U "$POSTGRES_USER" -d "$POSTGRES_DB"'`. Check the options against your Postgres version first; the tool does not run this for you. Then clear the cache, start the services and confirm the old slug works. Archive the receipt directory afterwards.

## After the run

After a complete apply, update everything outside Plane that names the slug:

1. Set `PLANE_WORKSPACE` in `config/local.env` to the new slug.
2. Update `vars.PLANE_WORKSPACE` for the GitHub examples.
3. Run one authenticated read through the MCP bridge to prove the new slug works.
4. Update webhook receivers and any integration that stores the old slug or URL.
5. Optionally add a reverse-proxy redirect from `/<old>/...` to `/<new>/...`. This is manual; the tool does not do it.

## Saltbox hosts

The default container names are the Saltbox ones. `check` and `apply` print `Saltbox-managed host: yes|no (information only)`, read from the `com.github.saltbox.saltbox_managed` label on the database container. It changes nothing. Stopping and starting uses `docker stop` and `docker start`, which do not alter container definitions, so the Saltbox inventory needs no edit. Do not run a Saltbox role against Plane during the outage window.
