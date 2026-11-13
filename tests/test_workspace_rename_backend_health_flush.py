"""Workspace rename backend snippet: health and cache flush."""

import json
import sys
import types
import pytest
from support_workspace_rename_backend import (
    checks,
    journal,
    make_orm,
    other_workspace_holds_new,
    raise_oserror,
    renamed_orm,
    restore,
    run_flush,
    run_health,
    run_op,
    snippet_namespace,
)


@pytest.mark.parametrize(
    "setup,probe,failed",
    [
        (renamed_orm, lambda: 200, set()),
        (make_orm, lambda: 200, {"new_present", "old_absent"}),  # not renamed yet
        (renamed_orm, lambda: 503, {"api"}),
        (renamed_orm, lambda: 302, {"api"}),  # a redirect is not an answer
        (renamed_orm, raise_oserror, {"api"}),
        (renamed_orm, None, {"api"}),
    ],
    ids=["healthy", "not_renamed", "api_503", "api_redirect", "api_down", "no_probe"],
)
def test_health_requires_new_present_and_old_absent(setup, probe, failed):
    orm = setup()
    before, writes = orm.snapshot(), len(orm.log)
    ok, events = run_health(orm, probe)
    assert ok is (not failed)
    assert checks(events) == {
        name: name not in failed for name in ("api", "new_present", "old_absent")
    }
    assert events[-1] == {"event": "summary", "ok": not failed, "step": "health"}
    assert orm.snapshot() == before and len(orm.log) == writes  # read-only: no update logged


def test_health_fails_when_another_workspace_holds_old():
    orm = renamed_orm()
    orm.models["workspace"].rows.append({"id": "w9", "slug": "old-slug", "deleted_at": None})
    ok, events = run_health(orm, lambda: 200)
    assert not ok and checks(events)["old_absent"] is False


def test_health_refuses_without_workspace_id():
    ok, events = run_health(renamed_orm(), lambda: 200, workspace_id=None)
    assert not ok and not checks(events)
    assert events == [
        {"event": "refuse", "reason": "workspace_id_missing"},
        {"event": "summary", "ok": False, "step": "health"},
    ]


def test_health_fails_when_new_is_held_by_another_workspace():
    orm = other_workspace_holds_new()  # NEW exists, OLD is absent, but w2 holds NEW
    ok, events = run_health(orm, lambda: 200, workspace_id="w1")
    assert not ok
    assert checks(events) == {"api": True, "new_present": False, "old_absent": True}
    assert run_health(orm, lambda: 200, workspace_id="w2")[0]


def test_health_checks_a_revert_with_the_slugs_swapped():
    orm = renamed_orm()
    records = [json.loads(json.dumps(r)) for r in journal(run_op("step_slug", make_orm())[1])]
    assert restore(orm, records)[0]
    ok, _ = run_health(orm, lambda: 200, old="new-slug", new="old-slug", workspace_id="w1")
    assert ok


def test_snippet_api_probe_gets_instances_endpoint(monkeypatch):
    gets = []

    def get(url, timeout, allow_redirects):
        gets.append((url, timeout, allow_redirects))
        return types.SimpleNamespace(status_code=200)

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(get=get))
    ns = snippet_namespace()
    assert ns["_api_probe"]({})() == 200
    assert gets == [("http://127.0.0.1:8000/api/instances/", 10, False)]  # never follow
    ns["_api_probe"]({"api_url": "http://api:8000/"})()
    assert gets[-1][0] == "http://api:8000/api/instances/"


def test_snippet_main_passes_the_api_probe_to_health():
    ns, orm, lines, seen = snippet_namespace(), renamed_orm(), [], []

    def probe_factory(args):
        seen.append(args)
        return lambda: 200

    request = {"op": "health", "args": {"old": "old-slug", "new": "new-slug", "workspace_id": "w1"}}
    ns["_main"](request, lambda: orm, lambda args: None, lines.append, probe_factory)
    assert json.loads(lines[-1]) == {"event": "summary", "ok": True, "step": "health"}
    assert seen == [request["args"]]


def test_flush_cache_clears_cache():
    orm = make_orm()
    orm.settings.SESSION_ENGINE = "django.contrib.sessions.backends.db"
    orm.cache.update({"/api/workspaces/": "old-slug", "/api/users/me/workspaces/": "x"})
    ok, events = run_flush(orm)
    assert ok and orm.cache == {}
    assert events == [
        {
            "event": "summary",
            "ok": True,
            "step": "cache",
            "cleared": True,
            "sessions_in_cache": False,
        }
    ]


def test_flush_cache_dry_run_reports_session_cache_without_clearing():
    orm = make_orm()
    orm.cache["k"] = "v"
    orm.settings.SESSION_ENGINE = "django.contrib.sessions.backends.cache"
    ok, events = run_flush(orm, dry_run=True)
    assert ok and orm.cache == {"k": "v"}
    assert events[-1]["cleared"] is False and events[-1]["sessions_in_cache"] is True


@pytest.mark.parametrize(
    "engine,expected",
    [
        ("django.contrib.sessions.backends.cache", True),
        ("django.contrib.sessions.backends.db", False),
        ("django.contrib.sessions.backends.cached_db", False),  # the db copy survives
        ("django.contrib.sessions.backends.file", False),
        ("django.contrib.sessions.backends.signed_cookies", False),
        ("plane.custom.backends.cache", "unknown"),  # never guessed from the name
        (None, "unknown"),
    ],
)
def test_flush_cache_maps_known_session_engines_only(engine, expected):
    orm = make_orm()
    if engine is not None:
        orm.settings.SESSION_ENGINE = engine
    summary = run_flush(orm, dry_run=True)[1][-1]
    assert summary["sessions_in_cache"] == expected
    assert "session_cache_alias" not in summary
    assert "backends" not in json.dumps(summary)  # the engine name is never emitted


@pytest.mark.parametrize("alias,reported", [("default", False), ("sessions", True)])
def test_flush_cache_reports_a_non_default_session_cache_alias(alias, reported):
    orm = make_orm()
    orm.settings.SESSION_ENGINE = "django.contrib.sessions.backends.cache"
    orm.settings.SESSION_CACHE_ALIAS = alias
    summary = run_flush(orm, dry_run=True)[1][-1]
    assert summary.get("session_cache_alias") == (alias if reported else None)


def test_flush_cache_failure_ends_with_summary_false():
    class Broken:
        def clear(self):
            raise ConnectionError("redis down")

    orm = make_orm()
    orm.cache = Broken()
    ok, events = run_flush(orm)
    assert not ok
    assert events == [
        {"event": "error", "reason": "cache_clear_failed", "detail": "ConnectionError"},
        {"event": "summary", "ok": False, "step": "cache"},
    ]
