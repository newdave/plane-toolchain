"""Pages list, retrieve, create views: errors and messages."""

import pytest
from support_pages_backend import _pages_view


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
