"""In-memory stand-in for the Django pieces the rename snippet touches.

Mimics only what the snippet uses: per-model `objects` (soft-delete filtered) and `all_objects`
(unfiltered) managers, `values`, `filter(...).update(...)` and its row count, `atomic()` with
rollback, a cache, `settings.WEB_URL` and the list of text and JSON columns.
"""

import contextlib
import copy
from types import SimpleNamespace


class QuerySet:
    def __init__(self, table, rows):
        self.table, self.rows = table, rows

    def filter(self, **kw):
        keep = [r for r in self.rows if all(r.get(k) == v for k, v in kw.items())]
        return QuerySet(self.table, keep)

    def values(self, *cols):
        rows = [{c: r["id"] if c == "pk" else r[c] for c in cols} for r in self.rows]
        return ValuesSet(self.table, rows)

    def update(self, **kw):
        for r in self.rows:
            r.update(copy.deepcopy(kw))
        self.table.updates.append((sorted(kw), len(self.rows)))
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
    def __init__(self, db_table, rows, columns):
        self.db_table, self.rows, self.columns = db_table, rows, columns
        self.updates = []
        self.streamed = 0
        self.objects = Manager(self, False)
        self.all_objects = Manager(self, True)


class FakeOrm:
    """`tables` maps a model key to `(db_table, columns, rows)`; columns are {name: "text"|"json"}."""

    def __init__(self, tables, web_url="https://plane.example.com", restricted=("api", "spaces")):
        self.models = {key: Table(db, rows, cols) for key, (db, cols, rows) in tables.items()}
        self.settings = SimpleNamespace(WEB_URL=web_url)
        self.restricted_slugs = set(restricted)
        self.cache = {}

    def text_columns(self):
        return [
            (t.db_table, t, c, kind) for t in self.models.values() for c, kind in t.columns.items()
        ]

    @contextlib.contextmanager
    def atomic(self):
        saved = self.snapshot()
        try:
            yield
        except BaseException:
            for k, rows in saved.items():
                self.models[k].rows[:] = rows
            raise

    def snapshot(self):
        return {k: copy.deepcopy(t.rows) for k, t in self.models.items()}
