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
    assert body["Labels"] == {"owner": "kept", "terrainlink.pages.source": "digest"}
    assert body["HostConfig"] == original["HostConfig"]
    assert body["NetworkingConfig"]["EndpointsConfig"]["shared"]["Aliases"] == ["service"]
    assert original["Config"]["Image"] == "base"
    assert original["Config"]["Labels"] == {"owner": "kept"}


def test_replacement_rejects_auto_removal_before_stopping_original():
    from plane_toolchain.pages_deploy import replacement

    with pytest.raises(ValueError, match="AutoRemove"):
        replacement({"HostConfig": {"AutoRemove": True}}, "derived", "http://live:3000", "digest")


def _pages_view(monkeypatch, missing_project=False, binary=None):
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
            return self

        def distinct(self):
            return self

        def order_by(self, *args):
            return self

        def values(self, *fields):
            calls["values"] = fields
            return [{"id": "p1"}]

    class Project:
        pass

    def get_object_or_404(source, **kwargs):
        calls.setdefault("lookups", []).append((source, kwargs))
        if kwargs["id"] == "bad":
            raise DjangoValidationError
        if source is Project:
            if missing_project:
                raise Http404
            return types.SimpleNamespace(guest_view_all_features=True)
        return "page-row"

    class Q:
        def __init__(self, *args, **kwargs):
            pass

        def __or__(self, other):
            return self

    class Members:
        def filter(self, **kwargs):
            return types.SimpleNamespace(exists=lambda: False)

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
            self.data = ("detail", instance)

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
        Page=types.SimpleNamespace(objects=Rows()),
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
    assert response.data == ("detail", "page-row")
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


def test_update_rechecks_precondition_under_lock_after_editors_flush():
    from datetime import datetime, timezone

    events = []
    flushed = _page(events=events, updated_at=datetime(2026, 11, 13, 2, tzinfo=timezone.utc))
    error, events = _update(events=events, locked_page=flushed)
    assert error.key == "conflict"
    assert not _writes(events)


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


def test_patch_missing_page_is_404_before_any_side_effect(monkeypatch):
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


def test_detail_route_allows_get_and_patch_only(monkeypatch):
    from plane_toolchain import pages_backend

    _pages_view(monkeypatch)
    args, _ = pages_backend.routes()[1]
    assert args[0].endswith("<uuid:page_id>/")
    assert args[1].allowed == ["get", "patch"]
