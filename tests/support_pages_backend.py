"""Shared helpers and fixtures for the split test_pages_backend files."""


def _pages_view(
    monkeypatch, missing_project=False, binary=None, pages=None, guest=False, guest_flag=True
):
    """Build the Pages view from stubbed Django/Plane modules (they exist only in the CE image)."""
    import sys
    import types

    from plane_toolchain import pages_backend

    calls = {}

    class Http404(Exception):
        pass

    class DjangoValidationError(Exception):
        pass

    def module(name, **attrs):
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, mod)

    class Rows:
        def filter(self, *args, **kwargs):
            if pages is not None:
                return PageRows(
                    [
                        row
                        for row in self.items
                        if all(q.matches(row) for q in args)
                        and all(row.get(k) == v for k, v in kwargs.items())
                    ]
                )
            return self

        def distinct(self):
            return self

        def order_by(self, *args):
            return self

        def values(self, *fields):
            calls["values"] = fields
            return [{"id": "p1"}]

    class PageRows(Rows):
        def __init__(self, items):
            self.items = items

        def get(self, **kwargs):
            for row in self.items:
                if all(row.get(k) == v for k, v in kwargs.items()):
                    return row
            raise Http404

    class Project:
        pass

    def get_object_or_404(source, **kwargs):
        calls.setdefault("lookups", []).append((source, kwargs))
        if kwargs["id"] == "bad":
            raise DjangoValidationError
        if source is Project:
            if missing_project:
                raise Http404
            return types.SimpleNamespace(guest_view_all_features=guest_flag)
        if pages is not None:
            return source.get(**kwargs)
        return "page-row"

    class Q:
        def __init__(self, *args, **kwargs):
            self.kwargs, self.others = kwargs, []

        def __or__(self, other):
            combined = Q()
            combined.others = [self, other]
            return combined

        def matches(self, row):
            if self.others:
                return any(q.matches(row) for q in self.others)
            return all(row.get(k) == v for k, v in self.kwargs.items())

    class Members:
        def filter(self, **kwargs):
            return types.SimpleNamespace(exists=lambda: guest)

    class Permission:
        pass

    class BaseAPIView:
        @classmethod
        def as_view(cls, **kwargs):
            return type(cls.__name__, (cls,), {"allowed": kwargs.get("http_method_names")})

    class PageViewSet:
        permission_classes = [Permission]

    class DetailSerializer:
        def __init__(self, instance, many=False):
            self.data = {"detail": instance}

    for name in (
        "requests",
        "django",
        "plane",
        "plane.api",
        "plane.api.views",
        "plane.app",
        "plane.app.views",
        "plane.app.views.page",
        "plane.bgtasks",
        "plane.settings",
        "rest_framework",
    ):
        module(name)
    module("plane.bgtasks.page_transaction_task", page_transaction=None)
    module("plane.settings.redis", redis_instance=None)
    module("django.db", transaction=None)
    module("django.urls", path=lambda *args, **kwargs: (args, kwargs))
    module("django.shortcuts", get_object_or_404=get_object_or_404)
    module("django.http", Http404=Http404)
    module(
        "django.core",
    )
    module("django.core.exceptions", ValidationError=DjangoValidationError)
    module("django.db.models", Q=Q)
    module("plane.api.views.base", BaseAPIView=BaseAPIView)
    module("plane.app.permissions", ProjectPagePermission=Permission)
    module(
        "plane.app.serializers",
        PageBinaryUpdateSerializer=binary,
        PageDetailSerializer=DetailSerializer,
        PageSerializer=None,
    )
    module("plane.app.views.page.base", PageViewSet=PageViewSet)
    module(
        "plane.db.models",
        Page=types.SimpleNamespace(objects=PageRows(pages) if pages is not None else Rows()),
        PageVersion=None,
        Project=Project,
        ProjectMember=types.SimpleNamespace(objects=Members()),
    )
    module("rest_framework.exceptions", APIException=Exception, ValidationError=ValueError)
    module("rest_framework.permissions", IsAuthenticated=object)
    module(
        "rest_framework.response",
        Response=lambda data, status=200: types.SimpleNamespace(data=data),
    )
    view = pages_backend.routes()[0][0][1]()
    view.kwargs = {"slug": "w", "project_id": "x"}
    view.request = types.SimpleNamespace(user="u")
    return view, calls, Http404


STAMP = "2026-11-13T01:02:03.123456Z"


def _page(**overrides):
    from datetime import datetime, timezone
    from types import SimpleNamespace

    events = overrides.pop("events", [])
    page = SimpleNamespace(
        id="0b5e1b2c-6f9e-4a39-9d3b-3d2f1c0a9e11",
        updated_at=datetime(2026, 11, 13, 1, 2, 3, 123456, tzinfo=timezone.utc),
        is_locked=False,
        archived_at=None,
        description_html="<p>Old</p>",
        description_json={"type": "old"},
        description_binary=b"old",
    )
    page.__dict__.update(overrides)
    page.save = lambda: events.append(
        ("save", page.description_html, page.description_json, page.description_binary)
    )
    return page


def _update(data=None, page=None, events=None, prepare=None, close=None, locked_page=None):
    """Run page_update with recording fakes; return (result-or-error, events)."""
    from contextlib import contextmanager

    from plane_toolchain.pages_backend import PageInputError, page_update

    events = [] if events is None else events
    page = page or _page(events=events)
    locked_page = locked_page or page

    @contextmanager
    def locked():
        events.append("lock")
        yield locked_page

    def default_prepare(html):
        events.append(("prepare", html))
        return {
            "description_html": html + "!",
            "description_json": {"type": "new"},
            "description_binary": b"new",
        }

    def default_close(doc_id):
        events.append(("close", doc_id))

    request = {"description_html": "<p>New</p>", "expected_updated_at": STAMP, "force": True}
    request.update(data or {})
    try:
        result = page_update(
            request,
            load=lambda: events.append("load") or page,
            prepare=prepare or default_prepare,
            close_editors=close or default_close,
            wait=lambda seconds: events.append(("wait", seconds)),
            locked=locked,
            version=lambda row: events.append(
                ("version", row.description_html, row.description_json, row.description_binary)
            ),
        )
    except PageInputError as error:
        return error, events
    return result, events


def _writes(events):
    return [e for e in events if isinstance(e, tuple) and e[0] in ("save", "version")]


def _closes(events):
    return [e for e in events if isinstance(e, tuple) and e[0] == "close"]


NON_OBJECT_BODIES = [[], ["name"], None, "text", 7]


def _row(page_id, owner, access):
    """A page row as the view's filters see it: lookup path to value."""
    return {
        "id": page_id,
        "owned_by": owner,
        "access": access,
        "workspace__slug": "w",
        "project_pages__project_id": "x",
        "project_pages__deleted_at__isnull": True,
    }


# Plane CE v1.4.3 rules, read from tag v1.4.3 (apps/api/plane/app/permissions/page.py
# ProjectPagePermission, and apps/api/plane/app/views/page/base.py PagesDescriptionViewSet):
# PATCH needs an active project role of admin or member; the owner of a page passes the
# object check; a non-owner passes only on public pages (access 0); another user's private
# page is denied; the description update and list/retrieve query use owned_by=user | access=0.
# A guest without guest_view_all_features sees only own pages (PageViewSet.retrieve/list).
PAGES = [
    _row("own-private", "u", 1),
    _row("own-public", "u", 0),
    _row("other-public", "u2", 0),
    _row("other-private", "u2", 1),
]


def _visible_ids(monkeypatch, **kwargs):
    view, _, _ = _pages_view(monkeypatch, pages=PAGES, **kwargs)
    return sorted(row["id"] for row in view.visible().items)


class _ApiError(Exception):
    def __init__(self, detail=""):
        super().__init__(detail)
        self.status_code = 500


def _patch_env(monkeypatch, locked_changes=None, reads=(b"new",), existing_versions=0):
    """Real patch() over fakes recording transaction scope, locking and writes."""
    import base64
    import sys
    import time
    import types
    from contextlib import contextmanager
    from datetime import datetime, timezone

    from plane_toolchain import pages_backend

    _pages_view(monkeypatch)
    log = []
    state = {"depth": 0, "next_id": 1000}

    def row(**changes):
        page = types.SimpleNamespace(
            id="0b5e1b2c-6f9e-4a39-9d3b-3d2f1c0a9e11",
            workspace_id="ws",
            updated_at=datetime(2026, 11, 13, 1, 2, 3, 123456, tzinfo=timezone.utc),
            is_locked=False,
            archived_at=None,
            description_html="<p>Old</p>",
            description_json={"type": "old"},
            description_binary=memoryview(b"old"),
        )
        page.__dict__.update(changes)
        page.save = lambda: log.append(("save", state["depth"] > 0, page.description_binary))
        return page

    first, locked_row = row(), row(**(locked_changes or {}))
    read_queue = list(reads)

    @contextmanager
    def atomic():
        log.append("atomic-enter")
        state["depth"] += 1
        try:
            yield
        finally:
            state["depth"] -= 1
            log.append("atomic-exit")

    class Locked:
        def filter(self, *args, **kwargs):
            return self

    class Reread:
        def values_list(self, field, flat=False):
            assert field == "description_binary"
            return self

        def first(self):
            return read_queue.pop(0) if len(read_queue) > 1 else read_queue[0]

    class PageRows:
        def filter(self, *args, **kwargs):
            return Reread() if "pk" in kwargs else self

        def distinct(self):
            return self

        def order_by(self, *args):
            return self

        def values(self, *fields):
            return ["id-subquery"]

        def select_for_update(self):
            log.append("select_for_update")
            return Locked()

    versions = [
        {"id": n, "page_id": first.id, "last_saved_at": n} for n in range(1, existing_versions + 1)
    ]

    class Versions:
        def __init__(self, items):
            self.items = items

        def filter(self, **kwargs):
            items = versions
            if "page_id" in kwargs:
                items = [v for v in items if v["page_id"] == kwargs["page_id"]]
            if "id__in" in kwargs:
                items = [v for v in items if v["id"] in kwargs["id__in"]]
            return Versions(items)

        def order_by(self, key):
            reverse = key.startswith("-")
            return Versions(sorted(self.items, key=lambda v: v["last_saved_at"], reverse=reverse))

        def values_list(self, field, flat=False):
            return [v[field] for v in self.items]

        def delete(self):
            ids = [v["id"] for v in self.items]
            versions[:] = [v for v in versions if v["id"] not in ids]
            log.append(("prune", sorted(ids), state["depth"] > 0))

        def create(self, **kwargs):
            state["next_id"] += 1
            versions.append(
                {"id": state["next_id"], "page_id": kwargs["page_id"], "last_saved_at": 10**6}
            )
            log.append(("version", state["depth"] > 0, bytes(kwargs["description_binary"])))

    class Project:
        pass

    def get_object_or_404(source, **kwargs):
        if source is Project:
            return types.SimpleNamespace(guest_view_all_features=True)
        if isinstance(source, Locked):
            log.append("locked-get")
            return locked_row
        log.append("load")
        return first

    class Binary:
        def __init__(self, data):
            self.data = data

        def is_valid(self, raise_exception=False):
            self.validated_data = dict(self.data)
            if "description_binary" in self.data:
                self.validated_data["description_binary"] = base64.b64decode(
                    self.data["description_binary"]
                )
            return True

    def post(url, json, timeout):
        return types.SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"description_json": {"type": "doc"}, "description_binary": "bmV3"},
        )

    class Client:
        def publish(self, channel, message):
            log.append("publish")
            return 1

    def module(name, **attrs):
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, mod)

    module("requests", post=post, RequestException=OSError)
    module("django.db", transaction=types.SimpleNamespace(atomic=atomic))
    module("django.shortcuts", get_object_or_404=get_object_or_404)
    module("rest_framework.exceptions", APIException=_ApiError, ValidationError=ValueError)
    module("plane.settings.redis", redis_instance=Client)
    module(
        "plane.bgtasks.page_transaction_task",
        page_transaction=types.SimpleNamespace(delay=lambda **kwargs: log.append("transaction")),
    )
    serializers = sys.modules["plane.app.serializers"]
    monkeypatch.setattr(serializers, "PageBinaryUpdateSerializer", Binary)
    models = sys.modules["plane.db.models"]
    monkeypatch.setattr(models, "Page", types.SimpleNamespace(objects=PageRows()))
    monkeypatch.setattr(models, "PageVersion", types.SimpleNamespace(objects=Versions(versions)))
    monkeypatch.setattr(models, "Project", Project)
    monkeypatch.setattr(time, "sleep", lambda seconds: log.append(("sleep", seconds)))
    monkeypatch.setenv("CE_PLANE_LIVE_URL", "http://live.invalid")

    view = pages_backend.routes()[1][0][1]()
    view.kwargs = {"slug": "w", "project_id": "x", "page_id": first.id}
    view.request = types.SimpleNamespace(
        user=types.SimpleNamespace(id="u"),
        data={"description_html": "<p>New</p>", "expected_updated_at": STAMP, "force": True},
    )
    return view, log, versions


def _run_patch(view):
    return view.patch(view.request, "w", "x", page_id=view.kwargs["page_id"])


def _first(log, kind):
    return next(
        i for i, e in enumerate(log) if e == kind or (isinstance(e, tuple) and e[0] == kind)
    )


def _tuples(log, *kinds):
    return [e for e in log if isinstance(e, tuple) and e[0] in kinds]
