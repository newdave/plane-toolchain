"""In-memory stand-in for the Django pieces the rename snippet touches.

Mimics only what the snippet uses: per-model `objects` (soft-delete filtered) and `all_objects`
(unfiltered) managers, `values`, `filter(...)` (exact, `pk`, `__gt`), `order_by`, sliced pages,
`filter(...).update(...)` and its row count, `atomic()` with rollback, a cache, `settings.WEB_URL`
and the list of text and JSON columns. `FakeOrm.log` records begin, commit, rollback and each
update with its ids, so tests can order writes against emitted events.
"""

import contextlib
import copy
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
