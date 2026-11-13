"""Django snippet for the workspace rename, run through `manage.py shell` in the API container.

The shipped source is `workspace_rename_match.py` followed by this file, so the matcher names
are globals here. Emits one JSON line per event through `emit` and writes nothing to disk.
Events carry only ids, tables, columns and counts, with one exception: `journal` events carry
the original values of a row about to be written, because the host must persist them (T5's
`append_journal`, mode 0600) for a byte-exact revert. The host never prints them.

`check` is read-only. The apply steps are `step_slug` (one transaction) and `step_rows` (one
row at a time); each emits the journal line for a row before it writes that row, with queryset
`.update()` only. `restore` writes the journal (sent inside the request) back in one
transaction and never calls Live. `health` is read-only; `flush_cache` clears the cache.
"""

# Dependencies below exist in the Plane backend image, not in this project's environment.
# pyright: reportMissingImports=false, reportUndefinedVariable=false
# ruff: noqa: F821

import base64
import json

if "validate_slug" not in globals():  # absent only when imported outside the shipped snippet
    from plane_toolchain.workspace_rename_match import (  # noqa: F401
        hosts_from_url,
        rewrite_html,
        rewrite_json_slug,
        scan_json,
        scan_text,
        validate_slug,
    )

# Rows rewritten by apply: (db table, model key, ((column, kind), ...)). Kind "html" is text
# with links, "json" a JSON document. Work items and Pages also rebuild the editor fields.
REWRITE_SCOPE = (
    ("issues", "issue", (("description_html", "html"),)),
    ("pages", "page", (("description_html", "html"),)),
    ("issue_comments", "comment", (("comment_html", "html"),)),
    ("notifications", "notification", (("data", "json"),)),
    # ASSUMPTION: comments link to a `Description` row (model `Description`, table
    # `descriptions`) that carries its own description_html and a workspace_id.
    ("descriptions", "description", (("description_html", "html"),)),
    # ASSUMPTION: model `EmailNotificationLog`, table `email_notification_logs`, has no
    # workspace column; a row belongs to OLD when its data names `workspace_slug` OLD.
    ("email_notification_logs", "email_log", (("data", "json"),)),
)
REBUILD_FIELDS = ("description_json", "description_binary", "description_stripped")
REBUILD_TABLES = ("issues", "pages", "descriptions")
# Columns apply derives from a rewritten column on in-scope rows; never unfixed.
DERIVED_COLUMNS = {
    "issues": REBUILD_FIELDS,
    "pages": REBUILD_FIELDS,
    "descriptions": REBUILD_FIELDS,
    "issue_comments": ("comment_json", "comment_stripped", "comment_binary"),
}
MAX_IDS = 50
UNOWNED = ("email_log",)  # models without a workspace column

# step_slug rewrites these in the slug transaction, without Live: html by rewrite_html, plain
# text by rewrite_html (only own-host absolute URLs can match), JSON by rewrite_json_slug.
SLUG_STEP = (
    (
        "issue_comments",
        "comment",
        (("comment_html", "html"), ("comment_stripped", "text"), ("comment_json", "json")),
    ),
    (
        "descriptions",
        "description",
        (
            ("description_html", "html"),
            ("description_stripped", "text"),
            ("description_json", "json"),
        ),
    ),
    ("notifications", "notification", (("data", "json"),)),
    ("email_notification_logs", "email_log", (("data", "json"),)),
)
# step_rows rebuilds work items and Pages through Live, with these variants.
ROW_STEP = (("issues", "issue", "rich"), ("pages", "page", "document"))
ROW_FIELDS = ("description_html", *REBUILD_FIELDS)
# Journalled as base64 text; journal_values decodes them back to bytes.
BINARY_COLUMNS = ("description_binary", "comment_binary")
PAGE_SIZE = 200


def _scope(key, workspace_id):
    """Queryset lookup for a model's rows in the workspace."""
    return {} if key in UNOWNED else {"workspace_id": workspace_id}


def _owned(key, row, workspace_id, old):
    if key in UNOWNED:
        return isinstance(row["data"], (dict, list)) and scan_json(row["data"], old)["slug"] > 0
    return str(row["workspace_id"]) == workspace_id


def _count(value, kind, old, hosts):
    """Total matches in one stored value; other value types count zero."""
    if isinstance(value, str):
        return sum(scan_text(value, old, hosts).values())
    if kind == "json" and isinstance(value, (dict, list)):
        return sum(scan_json(value, old, hosts).values())
    return 0


def _workspace_id(orm, old, new, emit):
    """Validate OLD and NEW; return the OLD workspace id, or None after emitting refusals."""
    rows = list(orm.models["workspace"].all_objects.values("pk", "slug", "deleted_at"))
    taken = {r["slug"] for r in rows}  # soft-deleted rows included
    current = [r for r in rows if r["slug"] == old and not r["deleted_at"]]
    ok = True
    if not current:
        emit({"event": "refuse", "reason": "old_missing_or_deleted"})
        ok = False
    try:
        validate_slug(new, orm.restricted_slugs, taken)
    except ValueError as exc:
        emit({"event": "refuse", "reason": "new_slug_invalid", "detail": str(exc)})
        ok = False
    return str(current[0]["pk"]) if ok else None


def _plan(orm, workspace_id, old, hosts, emit):
    planned = rebuild = 0
    for table, key, columns in REWRITE_SCOPE:
        names = [c for c, _ in columns]
        owner = () if key in UNOWNED else ("workspace_id",)
        rows = orm.models[key].all_objects.filter(**_scope(key, workspace_id))
        rows = rows.values("pk", *owner, *names)
        for row in rows:
            if not _owned(key, row, workspace_id, old):
                continue
            for column, kind in columns:
                n = _count(row[column], kind, old, hosts)
                if not n:
                    continue
                planned += 1
                event = {
                    "event": "plan",
                    "table": table,
                    "id": str(row["pk"]),
                    "field": column,
                    "count": n,
                }
                if table in REBUILD_TABLES:
                    event["rebuild"] = list(REBUILD_FIELDS)
                    rebuild += 1
                emit(event)
    return planned, rebuild


def _report(emit, table, column, ids, reason):
    """Emit one unfixed event: ids only, capped, never row content."""
    emit(
        {
            "event": "unfixed",
            "table": table,
            "column": column,
            "reason": reason,
            "count": len(ids),
            "ids": ids[:MAX_IDS],
            **({"truncated": True} if len(ids) > MAX_IDS else {}),
        }
    )


def _unfixed(orm, workspace_id, old, hosts, emit):
    """Report hits apply will not change; never change them.

    In-scope columns are rewritten only for the old workspace, so hits in other workspaces are
    reported as `other_workspace`. Hits in tables outside the scope are `out_of_scope`.
    """
    in_scope = {(t, c) for t, _, cols in REWRITE_SCOPE for c, _ in cols}
    in_scope |= {(t, c) for t, cols in DERIVED_COLUMNS.items() for c in cols}
    total = 0
    for table, key, columns in REWRITE_SCOPE:
        for column, kind in columns:
            owner = () if key in UNOWNED else ("workspace_id",)
            rows = orm.models[key].all_objects.values("pk", *owner, column).iterator()
            ids = [
                str(r["pk"])
                for r in rows
                if not _owned(key, r, workspace_id, old) and _count(r[column], kind, old, hosts)
            ]
            if ids:
                total += len(ids)
                _report(emit, table, column, ids, "other_workspace")
    for db_table, model, attname, column, kind in orm.text_columns():
        if (db_table, column) in in_scope:
            continue
        ids = [
            str(r["pk"])
            for r in model.all_objects.values("pk", attname).iterator()
            if _count(r[attname], kind, old, hosts)
        ]
        if ids:
            total += len(ids)
            _report(emit, db_table, column, ids, "out_of_scope")
    return total


def check(args, orm, emit, convert=None):
    """Read-only: validate the rename and report planned rewrites. True if apply may run."""
    old, new = args["old"], args["new"]
    hosts = hosts_from_url(orm.settings.WEB_URL)
    workspace_id = _workspace_id(orm, old, new, emit)
    if workspace_id is None:
        emit({"event": "summary", "ok": False})
        return False
    planned, rebuild = _plan(orm, workspace_id, old, hosts, emit)
    unfixed = _unfixed(orm, workspace_id, old, hosts, emit)
    emit(
        {"event": "summary", "ok": True, "planned": planned, "rebuild": rebuild, "unfixed": unfixed}
    )
    return True


class _Abort(Exception):
    """Stop a step with a content-free reason; rolls back an enclosing transaction."""

    def __init__(self, reason, **where):
        super().__init__(reason)
        self.event = {"event": "error", "reason": reason, **where}


class JournalValueError(TypeError):
    """A binary column read back as something other than bytes; it cannot be journalled."""


def _journal_value(column, value):
    """JSON-safe original: binary columns as base64 text (memoryview and bytes alike)."""
    if column in BINARY_COLUMNS and value is not None:
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise JournalValueError(column)
        return base64.b64encode(bytes(value)).decode("ascii")
    return value


def journal_values(record):
    """Column values of a journal row record, binary columns decoded back to bytes."""
    return {
        column: base64.b64decode(value, validate=True)
        if column in BINARY_COLUMNS and value is not None
        else value
        for column, value in record["fields"].items()
    }


def _emit_row(emit, table, row_id, originals):
    record = {
        "kind": "row",
        "table": table,
        "id": row_id,
        "fields": {c: _journal_value(c, v) for c, v in originals.items()},
    }
    emit({"event": "journal", "record": record})


def _pages(manager, lookup, columns):
    """Yield rows in pk order, PAGE_SIZE per query (keyset paging); never load a whole table."""
    last = None
    while True:
        query = manager.filter(**lookup)
        if last is not None:
            query = query.filter(pk__gt=last)
        page = list(query.order_by("pk").values("pk", *columns)[:PAGE_SIZE])
        yield from page
        if len(page) < PAGE_SIZE:
            return
        last = page[-1]["pk"]


def _rewrite(value, kind, old, new, hosts):
    """Return (value, count) for one stored value; other value types are left alone."""
    if kind in ("html", "text") and isinstance(value, str):
        return rewrite_html(value, old, new, hosts)
    if kind == "json" and isinstance(value, (dict, list)):
        return rewrite_json_slug(value, old, new, hosts)
    return value, 0


def _live_slugs(orm, slug):
    rows = orm.models["workspace"].all_objects.values("pk", "slug", "deleted_at")
    return [str(r["pk"]) for r in rows if r["slug"] == slug and not r["deleted_at"]]


def _renamed_refusal(orm, old, new, workspace_id):
    """Refusal reason unless OLD is gone and NEW is held by exactly `workspace_id`, else None.

    `workspace_id` is the id step_slug renamed, from the journal's workspace record. Without
    it, a NEW workspace that this rename never touched would look like a finished slug step.
    """
    if not workspace_id:
        return "workspace_id_missing"
    current = _live_slugs(orm, new)
    if _live_slugs(orm, old) or not current:
        return "slug_not_renamed"
    if current != [str(workspace_id)]:
        return "workspace_mismatch"
    return None


def step_slug(args, orm, emit, convert=None):
    """One transaction: the workspace slug, then comments, their Description mirrors and
    notification data. Each original is journalled before its row is written, and each write
    is guarded on those originals; any error rolls everything back. Never calls Live.

    The summary carries the renamed `workspace_id`. Re-running after a commit is a no-op only
    when args name that same id (`workspace_id`) and NEW is held by it."""
    old, new = args["old"], args["new"]
    given = args.get("workspace_id")
    summary = {"event": "summary", "ok": False, "step": "slug"}
    if given and not _live_slugs(orm, old):
        reason = _renamed_refusal(orm, old, new, given)
        if reason:
            emit({"event": "refuse", "reason": reason})
            emit(summary)
            return False
        emit({**summary, "ok": True, "already": True, "workspace_id": str(given)})
        return True
    workspace_id = _workspace_id(orm, old, new, emit)
    if workspace_id is None:
        emit(summary)
        return False
    if given and str(given) != workspace_id:
        emit({"event": "refuse", "reason": "workspace_mismatch"})
        emit(summary)
        return False
    hosts = hosts_from_url(orm.settings.WEB_URL)
    rewritten = not_rebuilt = 0
    try:
        with orm.atomic():
            emit(
                {
                    "event": "journal",
                    "record": {"kind": "workspace", "id": workspace_id, "slug": old},
                }
            )
            workspaces = orm.models["workspace"].all_objects
            if workspaces.filter(pk=workspace_id, slug=old).update(slug=new) != 1:
                raise _Abort("slug_update_failed")
            for table, key, columns in SLUG_STEP:
                manager = orm.models[key].all_objects
                names = [c for c, _ in columns]
                owner = () if key in UNOWNED else ("workspace_id",)
                for row in _pages(manager, _scope(key, workspace_id), (*owner, *names)):
                    if not _owned(key, row, workspace_id, old):
                        continue
                    changes = {}
                    for column, kind in columns:
                        value, n = _rewrite(row[column], kind, old, new, hosts)
                        if n:
                            changes[column] = value
                    if not changes:
                        continue
                    row_id = str(row["pk"])
                    originals = {c: row[c] for c in changes}
                    _emit_row(emit, table, row_id, originals)
                    # Guarded on the journalled originals: a concurrent edit aborts the step.
                    if manager.filter(pk=row["pk"], **originals).update(**changes) != 1:
                        raise _Abort("row_changed", table=table, id=row_id)
                    rewritten += 1
                    # ASSUMPTION: these binary mirrors are left as stored (no Live call here).
                    not_rebuilt += table in ("descriptions", "issue_comments")
    except _Abort as stop:
        emit(stop.event)
        emit(summary)
        return False
    except Exception as exc:  # rolled back by atomic(); report the type only, never content
        emit({"event": "error", "reason": "write_failed", "detail": type(exc).__name__})
        emit(summary)
        return False
    emit(
        {
            **summary,
            "ok": True,
            "rewritten": rewritten,
            "not_rebuilt": not_rebuilt,
            "workspace_id": workspace_id,
        }
    )
    return True


def _rebuilt(result, html, stripped, old, new, hosts):
    """Validate a Live answer into the four columns to write; raise ValueError if unusable."""
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("description_json"), dict)
        or not isinstance(result.get("description_binary"), str)
        or not result["description_binary"]
    ):
        raise ValueError("unusable conversion")
    binary = base64.b64decode(result["description_binary"], validate=True)
    if isinstance(result.get("description_stripped"), str):
        stripped = result["description_stripped"]
    elif isinstance(stripped, str):  # Live omits it: rewrite the stored text instead
        stripped = rewrite_html(stripped, old, new, hosts)[0]
    return {
        "description_html": html,
        "description_json": result["description_json"],
        "description_binary": binary,
        "description_stripped": stripped,
    }


def step_rows(args, orm, emit, convert=None):
    """Rewrite work item and Page descriptions that still match OLD, one row at a time.

    The HTML is rewrite_html(original); the editor fields come from Live (`rich` for work
    items, `document` for Pages). Order per row: convert, journal, update. A failed conversion
    stops the step before that row is touched. Resume re-derives the remaining rows, because a
    written row no longer matches."""
    old, new = args["old"], args["new"]
    summary = {"event": "summary", "ok": False, "step": "rows"}
    workspace_id = args.get("workspace_id")
    reason = _renamed_refusal(orm, old, new, workspace_id)
    if reason is None and convert is None:
        reason = "live_url_missing"
    if reason:
        emit({"event": "refuse", "reason": reason})
        emit(summary)
        return False
    workspace_id, hosts, rewritten = str(workspace_id), hosts_from_url(orm.settings.WEB_URL), 0
    where = {}
    try:
        for table, key, variant in ROW_STEP:
            manager = orm.models[key].all_objects
            for row in _pages(manager, {"workspace_id": workspace_id}, ("description_html",)):
                where = {}
                html = row["description_html"]
                if not isinstance(html, str):
                    continue
                new_html, n = rewrite_html(html, old, new, hosts)
                if not n:
                    continue
                row_id = str(row["pk"])
                where = {"table": table, "id": row_id}
                original = manager.filter(pk=row["pk"]).values(*ROW_FIELDS)[0]
                try:
                    fields = _rebuilt(
                        convert(new_html, variant),
                        new_html,
                        original["description_stripped"],
                        old,
                        new,
                        hosts,
                    )
                except Exception:  # Live down, timeout or bad answer: write nothing here
                    emit({"event": "error", "reason": "convert_failed", **where})
                    emit({**summary, "rewritten": rewritten})
                    return False
                _emit_row(emit, table, row_id, original)
                # The HTML guard makes the write a no-op if the row changed since it was read.
                if manager.filter(pk=row["pk"], description_html=html).update(**fields) != 1:
                    emit({"event": "error", "reason": "row_changed", **where})
                    emit({**summary, "rewritten": rewritten})
                    return False
                rewritten += 1
    except Exception as exc:  # report the type only, never content; earlier rows stay written
        reason = "row_failed" if where else "read_failed"
        emit({"event": "error", "reason": reason, **where, "detail": type(exc).__name__})
        emit({**summary, "rewritten": rewritten})
        return False
    emit({**summary, "ok": True, "rewritten": rewritten})
    return True


# Restore knows each journalled table's model key and the columns it may write back. Columns
# with a kind are re-derived (the expected rewritten value) and guard the write; work items and
# Pages guard on description_html only, because Live built their editor fields.
RESTORE_COLUMNS = {
    **{table: (key, dict(columns)) for table, key, columns in SLUG_STEP},
    **{
        table: (key, {c: "html" if c == "description_html" else None for c in ROW_FIELDS})
        for table, key, _ in ROW_STEP
    },
}
ROW_TABLES = tuple(table for table, _, _ in ROW_STEP)


class _Refuse(Exception):
    """A journal or state problem found before any write; carries the refusal reason."""


def _restore_plan(args):
    """Validate the request's journal; return (workspace_id, {(table, id): originals}).

    Generations: a rolled-back step_slug leaves journal lines that a rerun repeats, so the
    first original per (table, id, column) wins. Raises _Refuse before anything is written."""
    records = args.get("journal")
    if not isinstance(records, list):
        raise _Refuse("journal_invalid")
    if not records:
        raise _Refuse("journal_empty")
    workspace_id, rows = None, {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("id"), str):
            raise _Refuse("journal_invalid")
        if record.get("kind") == "workspace":
            if workspace_id not in (None, record["id"]):
                raise _Refuse("journal_invalid")
            if record.get("slug") != args["old"]:
                raise _Refuse("journal_mismatch")
            workspace_id = record["id"]
            continue
        table, fields = record.get("table"), record.get("fields")
        if record.get("kind") != "row" or table not in RESTORE_COLUMNS:
            raise _Refuse("journal_invalid")
        columns = RESTORE_COLUMNS[table][1]
        if not isinstance(fields, dict) or not fields or not set(fields) <= set(columns):
            raise _Refuse("journal_invalid")
        if not any(columns[c] for c in fields):  # nothing to re-derive, so no safe guard
            raise _Refuse("journal_invalid")
        if table in ROW_TABLES and set(fields) != set(ROW_FIELDS):  # step_rows journals all
            raise _Refuse("journal_invalid")
        try:
            values = journal_values(record)
        except (TypeError, ValueError):  # bad base64 or a non-text binary value
            raise _Refuse("journal_invalid") from None
        originals = rows.setdefault((table, record["id"]), {})
        for column, value in values.items():
            originals.setdefault(column, value)
    if workspace_id is None:
        raise _Refuse("workspace_record_missing")
    given = args.get("workspace_id")
    if given and str(given) != workspace_id:
        raise _Refuse("workspace_mismatch")
    return workspace_id, rows


def _restore_target(orm, old, new, workspace_id):
    """True if the slug must go back to OLD, False if it already has; refuse anything else."""
    rows = list(orm.models["workspace"].all_objects.values("pk", "slug", "deleted_at"))
    target = [r for r in rows if str(r["pk"]) == workspace_id and not r["deleted_at"]]
    if not target or target[0]["slug"] not in (old, new):
        raise _Refuse("workspace_mismatch")
    if any(r["slug"] == old and str(r["pk"]) != workspace_id for r in rows):  # deleted too
        raise _Refuse("old_slug_taken")
    return target[0]["slug"] == new


def _restore_row(orm, table, row_id, originals, workspace_id, old, new, hosts):
    """Restore one row if it still holds the rewrite; return "restored", "already" or a skip.

    The current values are compared with the originals (already restored) and with the
    re-derived rewrite (restore). Anything else is a newer edit, and is never overwritten.
    JSON null and SQL NULL both read as None, so None is never used to tell them apart."""
    key, columns = RESTORE_COLUMNS[table]
    manager = orm.models[key].all_objects
    owner = () if key in UNOWNED else ("workspace_id",)
    current = list(manager.filter(pk=row_id).values(*owner, *originals))
    if not current:
        return "missing"
    current = current[0]
    if owner and str(current["workspace_id"]) != workspace_id:
        return "other_workspace"
    if all(current[c] == v for c, v in originals.items()):
        return "already"
    expected = {
        c: _rewrite(originals[c], columns[c], old, new, hosts)[0] for c in originals if columns[c]
    }
    if any(current[c] != v for c, v in expected.items()):
        return "changed"
    # Guarded on the expected values: a write between the read and here updates nothing.
    if manager.filter(pk=row_id, **expected).update(**originals) != 1:
        return "changed"
    return "restored"


def restore(args, orm, emit, convert=None):
    """Write each journalled original back with queryset `.update()`, in one transaction.

    The journal arrives in the request (`args["journal"]`, the T5 records), because stdin is
    the program source under `manage.py shell`. The journal's workspace id must hold NEW (or
    OLD, once restored), and no other workspace may hold OLD. Rows that no longer hold the
    expected rewrite are skipped and reported. Never calls Live. Idempotent."""
    old, new = args["old"], args["new"]
    summary = {"event": "summary", "ok": False, "step": "restore"}
    try:
        workspace_id, rows = _restore_plan(args)
        slug_back = _restore_target(orm, old, new, workspace_id)
    except _Refuse as refusal:
        emit({"event": "refuse", "reason": str(refusal)})
        emit(summary)
        return False
    hosts = hosts_from_url(orm.settings.WEB_URL)
    counts = {"restored": 0, "already": 0}
    skips = []
    try:
        with orm.atomic():
            if slug_back:
                workspaces = orm.models["workspace"].all_objects
                if workspaces.filter(pk=workspace_id, slug=new).update(slug=old) != 1:
                    raise _Abort("slug_restore_failed")
            for (table, row_id), originals in sorted(rows.items()):
                outcome = _restore_row(orm, table, row_id, originals, workspace_id, old, new, hosts)
                if outcome in counts:
                    counts[outcome] += 1
                else:
                    skips.append({"event": "skip", "table": table, "id": row_id, "reason": outcome})
    except _Abort as stop:
        emit(stop.event)
        emit(summary)
        return False
    except Exception as exc:  # rolled back by atomic(); report the type only, never content
        emit({"event": "error", "reason": "write_failed", "detail": type(exc).__name__})
        emit(summary)
        return False
    for skip in skips:
        emit(skip)
    emit(
        {
            **summary,
            "ok": True,
            "slug_restored": slug_back,
            "restored": counts["restored"],
            "already_restored": counts["already"],
            "skipped": len(skips),
            "workspace_id": workspace_id,
        }
    )
    return True


def health(args, orm, emit, convert=None, probe=None):
    """Read-only: the API answers, NEW is held by a live workspace and OLD by none.

    `probe()` returns the HTTP status of the API's /api/instances/. With `workspace_id`, NEW
    must be held by exactly that id. For a revert, the host passes the slugs swapped. Every
    check runs and is reported; any failure gives `summary ok:false`."""
    old, new = args["old"], args["new"]
    try:
        api = probe is not None and probe() == 200
    except Exception:  # unreachable or timed out: a failed check, never a crash
        api = False
    holders = _live_slugs(orm, new)
    results = [
        ("api", api),
        ("new_present", bool(holders)),
        ("old_absent", not _live_slugs(orm, old)),
    ]
    if args.get("workspace_id"):
        results.append(("workspace", holders == [str(args["workspace_id"])]))
    for name, ok in results:
        emit({"event": "health", "check": name, "ok": ok})
    ok = all(ok for _, ok in results)
    emit({"event": "summary", "ok": ok, "step": "health"})
    return ok


def flush_cache(args, orm, emit, convert=None):
    """Clear the whole Django cache (`cache.clear()`), not pattern deletes, during the outage.

    `sessions_in_cache` tells the operator whether sign-ins are lost with it: only the pure
    cache session engine keeps sessions there. `dry_run` reports it without clearing. The
    engine name itself is never emitted."""
    engine = str(getattr(orm.settings, "SESSION_ENGINE", ""))
    summary = {"event": "summary", "ok": False, "step": "cache"}
    cleared = not args.get("dry_run")
    if cleared:
        try:
            orm.cache.clear()
        except Exception as exc:  # report the type only
            emit({"event": "error", "reason": "cache_clear_failed", "detail": type(exc).__name__})
            emit(summary)
            return False
    sessions = engine.endswith("backends.cache")
    emit({**summary, "ok": True, "cleared": cleared, "sessions_in_cache": sessions})
    return True


OPERATIONS = {
    "check": check,
    "step_slug": step_slug,
    "step_rows": step_rows,
    "restore": restore,
    "health": health,
    "flush_cache": flush_cache,
}


def run(op, args, orm, emit, convert, probe=None):
    """Run one operation and return its success. `convert(html, variant)` serves step_rows;
    `probe()` (the API status) serves health. No other operation receives the probe."""
    if op not in OPERATIONS:
        emit({"event": "error", "reason": "unknown_op"})
        return False
    if op == "health":
        return health(args, orm, emit, probe=probe)
    return OPERATIONS[op](args, orm, emit, convert)


API_URL = "http://127.0.0.1:8000"  # ASSUMPTION: the API serves on port 8000 in its container


def _api_probe(args):
    """Return a probe for the API's /api/instances/ status. The payload `api_url` wins over
    API_URL; the URL is never emitted. Raises on connection errors and after a 10 s timeout."""
    endpoint = (args.get("api_url") or API_URL).rstrip("/") + "/api/instances/"

    def probe():
        import requests

        return requests.get(endpoint, timeout=10).status_code

    return probe


def _live_convert(args):
    """Live /live/convert-document/ client, or None when no URL is configured.

    The payload `live_url` wins, else the API container's CE_PLANE_LIVE_URL (as pages_backend).
    The URL is never emitted. Raises on HTTP errors and after a 20 s timeout."""
    import os

    base = args.get("live_url") or os.environ.get("CE_PLANE_LIVE_URL")
    if not base:
        return None
    endpoint = base.rstrip("/") + "/live/convert-document/"

    def convert(html, variant):
        import requests

        response = requests.post(
            endpoint, json={"description_html": html, "variant": variant}, timeout=20
        )
        response.raise_for_status()
        return response.json()

    return convert


def _django_orm():
    """Bind the real Plane models; only called inside the API container."""
    from django.apps import apps
    from django.conf import settings
    from django.core.cache import cache
    from django.db import models as dj
    from django.db import transaction
    from plane.db.models import (
        Description,
        EmailNotificationLog,
        Issue,
        IssueComment,
        Notification,
        Page,
        Workspace,
    )
    from plane.utils.constants import RESTRICTED_WORKSPACE_SLUGS

    class Unfiltered:  # soft-delete managers hide deleted rows; fall back to the base manager
        def __init__(self, model):
            self.all_objects = getattr(model, "all_objects", model._base_manager)

    class Orm:
        pass

    orm = Orm()
    models = {
        "workspace": Workspace,
        "issue": Issue,
        "page": Page,
        "comment": IssueComment,
        "notification": Notification,
        "description": Description,
        "email_log": EmailNotificationLog,
    }
    orm.models = {key: Unfiltered(model) for key, model in models.items()}
    orm.settings, orm.restricted_slugs = settings, set(RESTRICTED_WORKSPACE_SLUGS)
    orm.atomic, orm.cache = transaction.atomic, cache

    def text_columns():
        for model in apps.get_models():
            for field in model._meta.concrete_fields:
                if isinstance(field, dj.JSONField):
                    kind = "json"
                elif isinstance(field, (dj.TextField, dj.CharField)):
                    kind = "text"
                else:
                    continue
                yield model._meta.db_table, Unfiltered(model), field.attname, field.column, kind

    orm.text_columns = lambda: list(text_columns())
    return orm


def _main(request, orm_factory, convert_factory, out, probe_factory=None):
    """Run one request, writing each event as one sorted-key JSON line through `out`.

    The request carries everything an operation needs, including restore's journal, because
    stdin is the program source under `manage.py shell`."""
    run(
        request["op"],
        request["args"],
        orm_factory(),
        lambda event: out(json.dumps(event, sort_keys=True)),
        convert_factory(request["args"]),
        probe_factory(request["args"]) if probe_factory else None,
    )


# The tail's exit status is always 0: the host must read the final `summary` event's `ok`.
if "RENAME_REQUEST" in globals():  # the host prepends this only when piping to manage.py shell
    _main(
        RENAME_REQUEST,
        _django_orm,
        _live_convert,
        lambda line: print(line, flush=True),
        _api_probe,
    )
