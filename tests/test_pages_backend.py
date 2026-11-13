"""Pages creation must validate every document format before storing anything."""

import pytest
from plane_toolchain.pages_backend import creation_payload


def test_creation_sanitises_html_then_converts_consistent_document_formats():
    calls = []
    result = creation_payload(
        {"name": "Planning", "description_html": "<p>Safe</p>", "access": 0},
        lambda value: value.replace("Safe", "Clean"),
        lambda value: (
            calls.append(value)
            or {"description_json": {"type": "doc"}, "description_binary": "AQ=="}
        ),
    )
    assert calls == ["<p>Clean</p>"]
    assert result["description_html"] == calls[0]
    assert result["description_binary"] == "AQ=="
    assert result["description_json"] == {"type": "doc"}


@pytest.mark.parametrize(
    "data",
    [
        {"name": "X", "description_html": "text", "owned_by": "other"},
        {"name": "", "description_html": "text"},
        {"name": "X", "description_html": "text", "access": 2},
    ],
)
def test_creation_rejects_unadvertised_or_invalid_fields_before_conversion(data):
    with pytest.raises(ValueError):
        creation_payload(data, lambda value: value, lambda value: pytest.fail("Must not convert"))


def test_conversion_failure_does_not_produce_a_partial_document():
    with pytest.raises(ValueError):
        creation_payload(
            {"name": "X", "description_html": "text"},
            lambda value: value,
            lambda value: {"description_json": {}},
        )


def test_replacement_preserves_opaque_config_and_does_not_mutate_source():
    from plane_toolchain.pages_deploy import replacement

    original = {
        "Config": {
            "Image": "base",
            "Env": ["OPAQUE=value"],
            "Labels": {"owner": "kept"},
            "Cmd": ["start"],
            "Hostname": "old",
        },
        "HostConfig": {"Binds": ["/data:/data"], "NetworkMode": "shared"},
        "NetworkSettings": {"Networks": {"shared": {"Aliases": ["service"], "IPAMConfig": None}}},
    }
    body = replacement(original, "derived", "http://live:3000", "digest")
    assert body["Env"] == ["OPAQUE=value", "CE_PLANE_LIVE_URL=http://live:3000"]
    assert body["Labels"] == {"owner": "kept", "plane-toolchain.pages.source": "digest"}
    assert body["HostConfig"] == original["HostConfig"]
    assert body["NetworkingConfig"]["EndpointsConfig"]["shared"]["Aliases"] == ["service"]
    assert original["Config"]["Image"] == "base"
    assert original["Config"]["Labels"] == {"owner": "kept"}


def test_replacement_rejects_auto_removal_before_stopping_original():
    from plane_toolchain.pages_deploy import replacement

    with pytest.raises(ValueError, match="AutoRemove"):
        replacement({"HostConfig": {"AutoRemove": True}}, "derived", "http://live:3000", "digest")


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


def test_missing_or_archived_project_is_404_not_500(monkeypatch):
    view, calls, http404 = _pages_view(monkeypatch, missing_project=True)
    with pytest.raises(http404):
        view.get(view.request, "w", "x")
    assert calls["lookups"][0][1]["archived_at__isnull"] is True


def test_list_requests_light_fields_and_returns_them_unchanged(monkeypatch):
    view, calls, _ = _pages_view(monkeypatch)
    response = view.get(view.request, "w", "x")
    assert calls["values"] == (
        "id",
        "name",
        "access",
        "owned_by",
        "created_at",
        "updated_at",
        "archived_at",
        "is_locked",
        "parent",
    )
    assert not [f for f in calls["values"] if f.startswith("description")]
    assert response.data == [{"id": "p1"}]


def test_retrieve_keeps_full_detail_serializer(monkeypatch):
    view, calls, _ = _pages_view(monkeypatch)
    response = view.get(view.request, "w", "x", page_id="y")
    assert response.data == {"detail": "page-row"}
    assert "values" not in calls


@pytest.mark.parametrize("bad", ["project", "page"])
def test_malformed_ids_are_404_not_500(monkeypatch, bad):
    view, _, http404 = _pages_view(monkeypatch)
    if bad == "project":
        view.kwargs["project_id"] = "bad"
        with pytest.raises(http404):
            view.get(view.request, "w", "bad")
    else:
        with pytest.raises(http404):
            view.get(view.request, "w", "x", page_id="bad")


def test_post_keeps_own_validation_messages_for_client(monkeypatch):
    view, _, _ = _pages_view(monkeypatch)
    view.request.data = {"name": "", "description_html": "x"}
    with pytest.raises(ValueError, match="non-empty page name"):
        view.post(view.request, "w", "x")


def test_post_hides_internal_error_text_from_client(monkeypatch):
    class Leaky:
        def __init__(self, data):
            pass

        def is_valid(self, raise_exception=False):
            raise ValueError("secret-token-123 at /srv/internal.py")

    view, _, _ = _pages_view(monkeypatch, binary=Leaky)
    view.request.data = {"name": "X", "description_html": "<p>x</p>"}
    with pytest.raises(ValueError) as caught:
        view.post(view.request, "w", "x")
    assert str(caught.value) == "Invalid page input"
    assert "secret-token-123" not in str(caught.value)


def test_input_errors_come_from_the_fixed_message_table():
    from plane_toolchain.pages_backend import INPUT_ERRORS, PageInputError

    for key, message in INPUT_ERRORS.items():
        error = PageInputError(key)
        assert error.key == key
        assert str(error) == message
    with pytest.raises(KeyError):
        PageInputError("not-a-key")


# PTC-44: whole-body edits of existing Pages.

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


def test_update_force_closes_editors_before_versioning_and_single_save():
    events = []
    page = _page(events=events)
    result, events = _update(page=page, events=events)
    names = [e if isinstance(e, str) else e[0] for e in events]
    assert names == ["load", "prepare", "close", "wait", "lock", "version", "save"]
    assert ("close", page.id) in events
    assert events[3][1] >= 1
    assert result["page"] is page
    assert result["old_html"] == "<p>Old</p>"


def test_update_writes_binary_html_and_json_together_in_one_save():
    events = []
    _update(page=_page(events=events), events=events)
    saves = [e for e in events if isinstance(e, tuple) and e[0] == "save"]
    assert saves == [("save", "<p>New</p>!", {"type": "new"}, b"new")]


def test_update_captures_old_body_in_a_version_before_writing():
    _, events = _update()
    assert ("version", "<p>Old</p>", {"type": "old"}, b"old") in events
    assert [e[0] for e in _writes(events)] == ["version", "save"]


@pytest.mark.parametrize(
    ("overrides", "key"),
    [({"is_locked": True}, "locked"), ({"archived_at": "2026-11-01"}, "archived")],
)
def test_update_refuses_locked_or_archived_pages_without_writing(overrides, key):
    events = []
    error, events = _update(page=_page(events=events, **overrides), events=events)
    assert error.key == key
    assert not _writes(events)
    assert not _closes(events)


def test_update_precondition_mismatch_is_conflict_without_writing():
    error, events = _update({"expected_updated_at": "2026-11-13T01:02:04Z"})
    assert error.key == "conflict"
    assert not _writes(events)
    assert not _closes(events)


def test_update_precondition_compares_instants_not_strings():
    result, _ = _update({"expected_updated_at": "2026-11-13T11:02:03.123456+10:00"})
    assert not isinstance(result, Exception)


def test_update_unchanged_live_restore_bumping_updated_at_does_not_block_write():
    from datetime import datetime, timezone

    events = []
    restored = _page(events=events, updated_at=datetime(2026, 11, 13, 2, tzinfo=timezone.utc))
    result, events = _update(events=events, locked_page=restored)
    assert result["page"] is restored
    assert [e[0] for e in _writes(events)] == ["version", "save"]


@pytest.mark.parametrize(
    "changed", [{"description_binary": b"flushed"}, {"description_html": "<p>Flushed</p>"}]
)
def test_update_refuses_when_content_changed_under_lock_after_force_close(changed):
    events = []
    flushed = _page(events=events, **changed)
    error, events = _update(events=events, locked_page=flushed)
    assert error.key == "conflict"
    assert not _writes(events)


def test_update_recheck_under_lock_still_refuses_newly_locked_page():
    events = []
    error, events = _update(events=events, locked_page=_page(events=events, is_locked=True))
    assert error.key == "locked"
    assert not _writes(events)


def test_late_store_check_hint_names_when_to_recheck_and_what_to_compare():
    from datetime import datetime, timezone

    from plane_toolchain.pages_backend import LATE_STORE_WINDOW_SECONDS, late_store_check

    now = datetime(2026, 11, 13, 1, 2, 3, tzinfo=timezone.utc)
    hint = late_store_check(now, "<p>x</p>")
    import hashlib

    assert hint["html_sha256"] == hashlib.sha256(b"<p>x</p>").hexdigest()
    assert LATE_STORE_WINDOW_SECONDS > 10  # Live debounce is 10 s
    assert hint["seconds"] == LATE_STORE_WINDOW_SECONDS
    assert hint["recheck_after"] == "2026-11-13T01:02:15+00:00"
    assert hint["compare"] == "description_html"
    assert "retry" in hint["on_difference"]


def test_update_without_force_is_refused_before_any_side_effect():
    error, events = _update({"force": False})
    assert error.key == "force"
    assert "disconnects open editors" in str(error)
    assert events == []


@pytest.mark.parametrize(
    "data",
    [
        {"expected_updated_at": None},
        {"expected_updated_at": "yesterday"},
        {"expected_updated_at": "2026-11-13T01:02:03"},
        {"description_html": 5},
        {"force": "yes"},
        {"name": "rename"},
    ],
)
def test_update_rejects_invalid_requests_before_any_side_effect(data):
    error, events = _update(data)
    assert isinstance(error, ValueError)
    assert events == []


def test_update_conversion_failure_writes_nothing_and_keeps_editors_open():
    from plane_toolchain.pages_backend import PageInputError

    def prepare(html):
        raise PageInputError("document")

    error, events = _update(prepare=prepare)
    assert error.key == "document"
    assert not _writes(events)
    assert not _closes(events)


def test_update_close_failure_writes_nothing():
    from plane_toolchain.pages_backend import PageInputError

    def close(doc_id):
        raise PageInputError("close")

    error, events = _update(close=close)
    assert error.key == "close"
    assert not _writes(events)
    assert "lock" not in events


def test_force_close_message_matches_live_admin_contract():
    import json

    from plane_toolchain.pages_backend import force_close_message

    channel, message = force_close_message("abc")
    assert channel == "hocuspocus:admin"
    data = json.loads(message)
    assert data["command"] == "force_close"
    assert data["docId"] == "abc"
    assert data["reason"] == "admin_request"
    assert data["code"] == 4000
    assert data["originServer"]


@pytest.mark.parametrize("outcome", [0, RuntimeError("redis down at secret-host")])
def test_publish_failure_or_no_subscriber_raises_close_error(outcome):
    from plane_toolchain.pages_backend import PageInputError, publish_force_close

    class Client:
        def publish(self, channel, message):
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    with pytest.raises(PageInputError) as caught:
        publish_force_close(Client(), "abc")
    assert caught.value.key == "close"
    assert "secret-host" not in str(caught.value)


def test_publish_force_close_sends_on_admin_channel():
    from plane_toolchain.pages_backend import publish_force_close

    sent = []

    class Client:
        def publish(self, channel, message):
            sent.append(channel)
            return 1

    publish_force_close(Client(), "abc")
    assert sent == ["hocuspocus:admin"]


def test_update_errors_map_to_http_status():
    from plane_toolchain.pages_backend import INPUT_ERRORS, UPDATE_STATUS

    assert UPDATE_STATUS["conflict"] == 409
    assert UPDATE_STATUS["close"] == 502
    assert set(UPDATE_STATUS) <= set(INPUT_ERRORS)
    assert "overwritten" not in INPUT_ERRORS


def test_patch_view_lookup_of_absent_page_is_404_behind_permission_403(monkeypatch):
    # ProjectPagePermission answers 403 for missing or invisible pages before the
    # view runs (as for retrieve); this 404 is the view's own defence in depth.
    view, _, http404 = _pages_view(monkeypatch)
    view.request.data = {
        "description_html": "<p>x</p>",
        "expected_updated_at": STAMP,
        "force": True,
    }
    with pytest.raises(http404):
        view.patch(view.request, "w", "x", page_id="bad")


def test_patch_without_force_returns_fixed_message_before_lookup(monkeypatch):
    view, calls, _ = _pages_view(monkeypatch)
    view.request.data = {"description_html": "<p>x</p>", "expected_updated_at": STAMP}
    with pytest.raises(ValueError, match="disconnects open editors"):
        view.patch(view.request, "w", "x", page_id="y")
    assert "lookups" not in calls


NON_OBJECT_BODIES = [[], ["name"], None, "text", 7]


@pytest.mark.parametrize("body", NON_OBJECT_BODIES)
def test_pure_validators_reject_non_object_bodies_as_input_errors(body):
    from plane_toolchain.pages_backend import PageInputError, update_request

    with pytest.raises(PageInputError):
        creation_payload(body, lambda v: v, lambda v: pytest.fail("Must not convert"))
    with pytest.raises(PageInputError):
        update_request(body)


@pytest.mark.parametrize("body", NON_OBJECT_BODIES)
def test_post_non_object_body_is_400_not_500(monkeypatch, body):
    view, calls, _ = _pages_view(monkeypatch)
    view.request.data = body
    with pytest.raises(ValueError, match="JSON object"):
        view.post(view.request, "w", "x")
    assert "lookups" not in calls


@pytest.mark.parametrize("body", NON_OBJECT_BODIES)
def test_patch_non_object_body_is_400_not_500(monkeypatch, body):
    view, calls, _ = _pages_view(monkeypatch)
    view.request.data = body
    with pytest.raises(ValueError, match="JSON object"):
        view.patch(view.request, "w", "x", page_id="y")
    assert "lookups" not in calls


@pytest.mark.parametrize("reply", [[], None, "text", 7])
def test_post_non_object_live_reply_is_a_conversion_failure(monkeypatch, reply):
    class Valid:
        def __init__(self, data):
            self.data = data

        def is_valid(self, raise_exception=False):
            return True

        @property
        def validated_data(self):
            return self.data

    view, _, _ = _pages_view(monkeypatch, binary=Valid)
    import sys

    class Reply:
        def raise_for_status(self):
            pass

        def json(self):
            return reply

    class RequestException(Exception):
        pass

    requests = sys.modules["requests"]
    requests.RequestException = RequestException
    requests.post = lambda *args, **kwargs: Reply()
    monkeypatch.setenv("CE_PLANE_LIVE_URL", "http://live.example.com")
    view.request.data = {"name": "X", "description_html": "<p>x</p>"}
    with pytest.raises(Exception) as caught:
        view.post(view.request, "w", "x")
    assert str(caught.value) == "Live document conversion unavailable; nothing written"
    assert caught.value.status_code == 502


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


def test_member_sees_own_pages_and_public_pages_of_others_only(monkeypatch):
    # Non-owner PATCH on a public page is allowed by Plane (members edit public pages).
    assert _visible_ids(monkeypatch) == ["other-public", "own-private", "own-public"]


def test_another_owners_private_page_is_not_visible_to_patch_or_get(monkeypatch):
    view, _, http404 = _pages_view(monkeypatch, pages=PAGES)
    view.request.data = {
        "description_html": "<p>x</p>",
        "expected_updated_at": STAMP,
        "force": True,
    }
    with pytest.raises(http404):
        view.patch(view.request, "w", "x", page_id="other-private")
    with pytest.raises(http404):
        view.get(view.request, "w", "x", page_id="other-private")


def test_non_owner_can_retrieve_a_public_page(monkeypatch):
    view, _, _ = _pages_view(monkeypatch, pages=PAGES)
    assert view.get(view.request, "w", "x", page_id="other-public").data["detail"]["id"] == (
        "other-public"
    )


def test_guest_without_guest_view_all_features_sees_only_own_pages(monkeypatch):
    ids = _visible_ids(monkeypatch, guest=True, guest_flag=False)
    assert ids == ["own-private", "own-public"]


def test_guest_with_guest_view_all_features_sees_public_pages(monkeypatch):
    # Guests still cannot PATCH: ProjectPagePermission allows PATCH for admin/member only.
    ids = _visible_ids(monkeypatch, guest=True, guest_flag=True)
    assert ids == ["other-public", "own-private", "own-public"]


def test_pages_view_is_gated_by_plane_page_permission_and_authentication(monkeypatch):
    # Role checks (admin/member PATCH and POST, guest read only) live in Plane's
    # ProjectPagePermission; the view must keep it and fail closed if CE changes it.
    from plane_toolchain import pages_backend

    _pages_view(monkeypatch)
    view = pages_backend.routes()[1][0][1]
    import sys

    perms = view.permission_classes
    assert perms[0] is sys.modules["rest_framework.permissions"].IsAuthenticated
    assert [c.__name__ for c in perms[1:]] == ["Permission"]
    sys.modules["plane.app.views.page.base"].PageViewSet.permission_classes = []
    with pytest.raises(RuntimeError, match="permission contract changed"):
        pages_backend.routes()


def test_detail_route_allows_get_and_patch_only(monkeypatch):
    from plane_toolchain import pages_backend

    _pages_view(monkeypatch)
    args, _ = pages_backend.routes()[1]
    assert args[0].endswith("<uuid:page_id>/")
    assert args[1].allowed == ["get", "patch"]


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


def test_patch_locks_before_recheck_and_writes_inside_one_transaction(monkeypatch):
    view, log, _ = _patch_env(monkeypatch)
    response = _run_patch(view)
    assert response.data["detail"].id == view.kwargs["page_id"]
    order = [_first(log, k) for k in ("load", "publish", "sleep", "select_for_update")]
    assert order == sorted(order)
    enter, exit_ = _first(log, "atomic-enter"), _first(log, "atomic-exit")
    assert _first(log, "select_for_update") < _first(log, "locked-get")
    assert enter < _first(log, "locked-get") < _first(log, "version") < _first(log, "save") < exit_
    assert _tuples(log, "version", "save") == [("version", True, b"old"), ("save", True, b"new")]
    assert exit_ < _first(log, "transaction")


def test_patch_recheck_uses_the_locked_row_and_rolls_back(monkeypatch):
    view, log, _ = _patch_env(monkeypatch, locked_changes={"description_binary": b"flushed"})
    with pytest.raises(_ApiError) as caught:
        _run_patch(view)
    assert caught.value.status_code == 409
    assert not _tuples(log, "version", "save")
    assert "atomic-exit" in log
    assert "transaction" not in log


def test_patch_prunes_versions_to_twenty_oldest_first_inside_transaction(monkeypatch):
    view, log, versions = _patch_env(monkeypatch, existing_versions=21)
    _run_patch(view)
    assert len(versions) == 20
    assert _tuples(log, "prune") == [("prune", [1, 2], True)]
    assert _first(log, "version") < _first(log, "prune") < _first(log, "save")


def test_patch_keeps_versions_under_the_cap(monkeypatch):
    view, log, versions = _patch_env(monkeypatch, existing_versions=3)
    _run_patch(view)
    assert len(versions) == 4
    assert not _tuples(log, "prune")


def test_patch_returns_at_once_with_a_late_store_hint_and_never_polls(monkeypatch):
    from plane_toolchain.pages_backend import FORCE_CLOSE_WAIT_SECONDS

    view, log, versions = _patch_env(monkeypatch)
    response = _run_patch(view)
    # Only the pre-write wait for Live's store remains; nothing sleeps after commit.
    assert _tuples(log, "sleep") == [("sleep", FORCE_CLOSE_WAIT_SECONDS)]
    assert _first(log, "sleep") < _first(log, "atomic-exit")
    hint = response.data["late_store_check"]
    assert hint["compare"] == "description_html"
    assert hint["seconds"] >= 12
    assert ("save", True, b"new") in log
    assert len(versions) == 1
