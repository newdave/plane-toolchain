"""In-memory stand-in for the Django pieces the rename snippet touches.

Mimics only what the snippet uses: per-model `objects` (soft-delete filtered) and `all_objects`
(unfiltered) managers, `values`, `filter(...)` (exact, `pk`, `__gt`), `order_by`, sliced pages,
`filter(...).update(...)` and its row count, `atomic()` with rollback, a cache, `settings.WEB_URL`
and the list of text and JSON columns. `FakeOrm.log` records begin, commit, rollback and each
update with its ids, so tests can order writes against emitted events.

`ManageShell` serves FakeDocker's `on_manage_shell`: it runs the piped program (the request
line plus the assembled snippet, tail included) with fake django, plane and requests modules
bound to a FakeOrm, a FakeLiveService and an API status.
"""

import base64
import contextlib
import copy
import hashlib
import io
import sys
import types
from types import SimpleNamespace


def _matches(row, lookup, value):
    name, _, op = lookup.partition("__")
    actual = row.get("id" if name == "pk" else name)
    if op == "gt":
        return actual is not None and actual > value
    if op:
        raise NotImplementedError(lookup)
    return actual == value


def clone(value):
    """Deep copy that also copies memoryview, as Django returns for BinaryField reads."""
    if isinstance(value, memoryview):
        return memoryview(bytes(value))
    if isinstance(value, dict):
        return {k: clone(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clone(v) for v in value]
    return copy.deepcopy(value)


class QuerySet:
    def __init__(self, table, rows):
        self.table, self.rows = table, rows

    def filter(self, **kw):
        keep = [r for r in self.rows if all(_matches(r, k, v) for k, v in kw.items())]
        return QuerySet(self.table, keep)

    def order_by(self, field):
        key = "id" if field == "pk" else field
        return QuerySet(self.table, sorted(self.rows, key=lambda r: r[key]))

    def values(self, *cols):
        # Like Django, `values()` takes field names (attnames); a DB column name that differs raises.
        rows = [{c: r["id"] if c == "pk" else r[c] for c in cols} for r in self.rows]
        return ValuesSet(self.table, rows)

    def update(self, **kw):
        if self.table.fail_update:
            raise RuntimeError("injected update failure")
        for r in self.rows:
            r.update(copy.deepcopy(kw))
        self.table.updates.append((sorted(kw), len(self.rows)))
        self.table.log.append(("update", self.table.db_table, [r["id"] for r in self.rows]))
        return len(self.rows)

    def __iter__(self):
        return iter(self.rows)


class ValuesSet(list):
    """Result of `values()`: a list of dicts that also streams through `iterator()`."""

    def __init__(self, table, rows):
        super().__init__(rows)
        self.table = table

    def iterator(self):
        self.table.streamed += 1
        return iter(list(self))

    def __getitem__(self, index):
        result = super().__getitem__(index)
        if isinstance(index, slice):
            self.table.pages.append(len(result))  # one paged query
        return result


class Manager:
    def __init__(self, table, include_deleted):
        self.table, self.include_deleted = table, include_deleted

    def all(self):
        rows = self.table.rows
        if not self.include_deleted:
            rows = [r for r in rows if not r.get("deleted_at")]
        return QuerySet(self.table, rows)

    def filter(self, **kw):
        return self.all().filter(**kw)

    def values(self, *cols):
        return self.all().values(*cols)


class Table:
    def __init__(self, db_table, rows, columns, attnames=None, log=None):
        self.db_table, self.rows, self.columns = db_table, rows, columns
        self.attnames = attnames or {}  # DB column -> field name, when they differ
        self.updates = []
        self.streamed = 0
        self.pages = []
        self.fail_update = False
        self.log = [] if log is None else log
        self.objects = Manager(self, False)
        self.all_objects = Manager(self, True)


class FakeOrm:
    """`tables` maps a model key to `(db_table, columns, rows)`; columns are {column: "text"|"json"}.

    A table entry may carry a fourth item, `{column: attname}`; rows are keyed by attname.
    """

    def __init__(self, tables, web_url="https://plane.example.com", restricted=("api", "spaces")):
        self.log = []
        self.models = {
            key: Table(db, rows, cols, *rest[:1], log=self.log)
            for key, (db, cols, rows, *rest) in tables.items()
        }
        self.settings = SimpleNamespace(WEB_URL=web_url)
        self.restricted_slugs = set(restricted)
        self.cache = {}

    def text_columns(self):
        return [
            (t.db_table, t, t.attnames.get(c, c), c, kind)
            for t in self.models.values()
            for c, kind in t.columns.items()
        ]

    @contextlib.contextmanager
    def atomic(self):
        saved = self.snapshot()
        self.log.append(("begin",))
        try:
            yield
        except BaseException:
            for k, rows in saved.items():
                self.models[k].rows[:] = rows
            self.log.append(("rollback",))
            raise
        self.log.append(("commit",))

    def snapshot(self):
        return {k: clone(t.rows) for k, t in self.models.items()}


# --- `manage.py shell` stand-in: run the piped program against a FakeOrm ---

# Model class names the snippet's `_django_orm` imports from plane.db.models, by model key.
PLANE_MODELS = {
    "Workspace": "workspace",
    "Issue": "issue",
    "Page": "page",
    "IssueComment": "comment",
    "Notification": "notification",
    "Description": "description",
    "EmailNotificationLog": "email_log",
}


class FakeLiveService:
    """Live /live/convert-document/: deterministic per (html, variant); `fail` raises."""

    def __init__(self):
        self.calls, self.fail = [], False

    def convert(self, html, variant):
        self.calls.append((html, variant))
        if self.fail:
            raise RuntimeError("Live unavailable")
        digest = hashlib.sha256(f"{variant}:{html}".encode()).hexdigest()
        return {
            "description_json": {"type": "doc", "variant": variant, "digest": digest[:12]},
            "description_binary": base64.b64encode(digest.encode()).decode(),
            "description_stripped": "stripped-" + digest[:8],
        }


def django_modules(orm, live, api):
    """sys.modules entries for the imports `_django_orm`, `_live_convert` and `_api_probe` make.

    `api["status"]` is what the API's /api/instances/ answers."""
    fields = types.ModuleType("django.db.models")
    for name in ("JSONField", "TextField", "CharField"):
        setattr(fields, name, type(name, (), {}))

    def model(table):
        concrete = []
        for column, kind in table.columns.items():
            field = getattr(fields, "JSONField" if kind == "json" else "TextField")()
            field.column, field.attname = column, table.attnames.get(column, column)
            concrete.append(field)
        meta = SimpleNamespace(db_table=table.db_table, concrete_fields=concrete)
        manager = table.all_objects
        return SimpleNamespace(all_objects=manager, _base_manager=manager, _meta=meta)

    models = {key: model(table) for key, table in orm.models.items()}

    class Response:
        def __init__(self, status, body=None):
            self.status_code, self.body = status, body

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError("HTTP error")

        def json(self):
            return self.body

    def post(endpoint, json, timeout):
        assert endpoint.endswith("/live/convert-document/")
        return Response(200, live.convert(json["description_html"], json["variant"]))

    def get(endpoint, timeout, allow_redirects):
        assert endpoint.endswith("/api/instances/") and allow_redirects is False
        return Response(api["status"])

    specs = {
        "django": {},
        "django.apps": {"apps": SimpleNamespace(get_models=lambda: list(models.values()))},
        "django.conf": {"settings": orm.settings},
        "django.core": {},
        "django.core.cache": {"cache": orm.cache},
        "django.db": {},
        "django.db.transaction": {"atomic": orm.atomic},
        "plane": {},
        "plane.db": {},
        "plane.db.models": {name: models[key] for name, key in PLANE_MODELS.items()},
        "plane.utils": {},
        "plane.utils.constants": {"RESTRICTED_WORKSPACE_SLUGS": sorted(orm.restricted_slugs)},
        "requests": {"post": post, "get": get},
    }
    modules = {"django.db.models": fields}
    for name, attributes in specs.items():
        modules[name] = types.ModuleType(name)
        for attribute, value in attributes.items():
            setattr(modules[name], attribute, value)
    for name, module in modules.items():  # bind each submodule on its parent package
        parent, _, leaf = name.rpartition(".")
        if parent:
            setattr(modules[parent], leaf, module)
    return modules


class ManageShell:
    """FakeDocker `on_manage_shell` handler: exec the piped program as `manage.py shell` does,
    with Django, Plane and requests bound to `orm`, a FakeLiveService and the API status.

    Records each request (`requests`). Stdout is what the program printed; an exception that
    escapes the program exits 1, as the real shell would."""

    def __init__(self, orm, live=None, api_status=200):
        self.orm, self.live = orm, live or FakeLiveService()
        self.api = {"status": api_status}
        self.requests = []

    def __call__(self, text):
        modules = django_modules(self.orm, self.live, self.api)
        saved = {name: sys.modules.get(name) for name in modules}
        sys.modules.update(modules)
        out = io.StringIO()
        namespace = {"__name__": "__main__"}
        try:
            with contextlib.redirect_stdout(out):
                exec(compile(text, "<manage.py shell>", "exec"), namespace)  # noqa: S102
        except Exception:  # noqa: BLE001 - the real shell exits non-zero
            return out.getvalue(), 1
        finally:
            for name, module in saved.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module
            if "RENAME_REQUEST" in namespace:
                self.requests.append(namespace["RENAME_REQUEST"])
        return out.getvalue(), 0
