"""Pages PATCH view: locking, version pruning and late-store hints."""

import pytest
from support_pages_backend import _ApiError, _first, _patch_env, _run_patch, _tuples


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
