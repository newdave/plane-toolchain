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
            return cls

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
        "rest_framework",
    ):
        module(name)
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
