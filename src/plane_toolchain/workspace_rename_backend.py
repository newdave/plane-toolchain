"""Django snippet for the workspace rename, run through `manage.py shell` in the API container.

The shipped source is `workspace_rename_match.py` followed by this file, so the matcher names
are globals here. Emits one JSON line per event through `emit`, writes nothing to disk and never
emits row content: only ids, tables, columns and counts. `check` is read-only.
"""

# Dependencies below exist in the Plane backend image, not in this project's environment.
# pyright: reportMissingImports=false, reportUndefinedVariable=false
# ruff: noqa: F821

if "validate_slug" not in globals():  # absent only when imported outside the shipped snippet
    from plane_toolchain.workspace_rename_match import (  # noqa: F401
        hosts_from_url,
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
        rows = orm.models[key].all_objects.filter(workspace_id=workspace_id).values("pk", *names)
        for row in rows:
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


def _unfixed(orm, old, hosts, emit):
    """Report hits outside the rewrite scope by table and column; never change them."""
    in_scope = {(t, c) for t, _, cols in REWRITE_SCOPE for c, _ in cols}
    in_scope |= {(t, c) for t, cols in DERIVED_COLUMNS.items() for c in cols}
    total = 0
    for db_table, model, column, kind in orm.text_columns():
        if (db_table, column) in in_scope:
            continue
        ids = [
            str(r["pk"])
            for r in model.all_objects.values("pk", column).iterator()
            if _count(r[column], kind, old, hosts)
        ]
        if ids:
            total += len(ids)
            emit(
                {
                    "event": "unfixed",
                    "table": db_table,
                    "column": column,
                    "count": len(ids),
                    "ids": ids[:MAX_IDS],
                    **({"truncated": True} if len(ids) > MAX_IDS else {}),
                }
            )
    return total


def check(args, orm, emit):
    """Read-only: validate the rename and report planned rewrites. True if apply may run."""
    old, new = args["old"], args["new"]
    hosts = hosts_from_url(orm.settings.WEB_URL)
    workspace_id = _workspace_id(orm, old, new, emit)
    if workspace_id is None:
        emit({"event": "summary", "ok": False})
        return False
    planned, rebuild = _plan(orm, workspace_id, old, hosts, emit)
    unfixed = _unfixed(orm, old, hosts, emit)
    emit(
        {"event": "summary", "ok": True, "planned": planned, "rebuild": rebuild, "unfixed": unfixed}
    )
    return True


OPERATIONS = {"check": check}  # apply and restore register here


def run(op, args, orm, emit, convert):
    """Run one operation and return its success. `convert` serves the apply steps."""
    if op not in OPERATIONS:
        emit({"event": "error", "reason": "unknown_op"})
        return False
    return OPERATIONS[op](args, orm, emit)


def _django_orm():
    """Bind the real Plane models; only called inside the API container."""
    from django.apps import apps
    from django.conf import settings
    from django.core.cache import cache
    from django.db import models as dj
    from django.db import transaction
    from plane.db.models import (
        Description,
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
                yield model._meta.db_table, Unfiltered(model), field.column, kind

    orm.text_columns = lambda: list(text_columns())
    return orm


# The tail's exit status is always 0: the host must read the final `summary` event's `ok`.
if "RENAME_REQUEST" in globals():  # the host prepends this only when piping to manage.py shell
    import json as _json

    run(
        RENAME_REQUEST["op"],
        RENAME_REQUEST["args"],
        _django_orm(),
        lambda event: print(_json.dumps(event), flush=True),
        None,
    )
