"""Pages visibility and permissions by role."""

import pytest
from support_pages_backend import PAGES, STAMP, _pages_view, _visible_ids


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
