"""Pages update (PATCH body): locking, versions, force-close, errors."""

import pytest
from plane_toolchain.pages_backend import creation_payload
from support_pages_backend import (
    NON_OBJECT_BODIES,
    STAMP,
    _closes,
    _page,
    _pages_view,
    _update,
    _writes,
)


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
        {"description_html": ""},
        {"description_html": " \n\t "},
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
