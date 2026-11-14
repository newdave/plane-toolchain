"""Pages deploy: retained containers, prune and label sets."""

import json
import subprocess
import sys
import pytest
from fake_docker import FakeDocker
from plane_toolchain import pages_deploy
from support_pages_deploy import (
    BACKUP,
    COMPOSE,
    DIGEST,
    IMAGE,
    LABEL,
    LEGACY_BACKUP,
    deploy,
    park_retired,
    prune,
)
from support_pages_deploy import applied  # noqa: F401
from support_pages_deploy import composed  # noqa: F401
from support_pages_deploy import host  # noqa: F401


def test_apply_and_revert_results_list_retained_container_names(host):
    park_retired(host)
    message = deploy("apply", host)
    assert BACKUP in message
    assert "api-pages-reverted-1" in message and "api-pages-failed-2-1" in message
    assert "retained containers" in message
    assert "OPAQUE_SECRET" not in message


def test_revert_result_lists_retained_container_names(applied):
    park_retired(applied)
    message = deploy("revert", applied, later=5)
    assert "api-pages-reverted-1" in message and "api-pages-failed-2" in message


def test_prune_is_a_dry_run_by_default(host):
    park_retired(host)
    before = set(host.containers)
    message = prune(host)
    assert set(host.containers) == before
    assert "Dry run" in message and "PLANE_PAGES_PRUNE=yes" in message
    assert "api-pages-reverted-1" in message and "api-pages-failed-2-1" in message
    assert "rm" not in {call[0] for call in host.calls}


def test_prune_with_confirmation_removes_only_stopped_labelled_retired_containers(host):
    park_retired(host)
    host.add("api-pages-failed-3", labels={LABEL: DIGEST}, running=True)
    host.add(BACKUP, labels={LABEL: DIGEST}, running=False)
    host.add("api-other-pages-failed-9", labels={LABEL: DIGEST}, running=False)
    message = prune(host, confirm=True)
    assert set(host.containers) == {
        "api",
        BACKUP,
        "api-pages-reverted-1",
        "api-pages-failed-3",
        "api-other-pages-failed-9",
    }
    assert "Pruned: api-pages-failed-2, api-pages-failed-2-1" in message
    assert "kept running: api-pages-failed-3" in message


def test_prune_never_removes_an_unlabelled_aside_container(host):
    # restore_any parks the unlabelled upstream original as `<reverted>-aside`.
    host.add("api-pages-reverted-1-aside", running=False)
    assert prune(host, confirm=True) == "No retired containers to prune"
    assert "api-pages-reverted-1-aside" in host.containers


def test_prune_never_removes_a_labelled_aside_container(host):
    host.add("api-pages-reverted-1-aside", labels={LABEL: DIGEST}, running=False)
    prune(host, confirm=True)
    assert "api-pages-reverted-1-aside" in host.containers


def test_prune_never_removes_an_unlabelled_failed_container(host):
    host.add("api-pages-failed-5", running=False)
    host.add("api-pages-reverted-6", running=False)
    prune(host, confirm=True)
    assert {"api-pages-failed-5", "api-pages-reverted-6"} <= set(host.containers)


def test_prune_leaves_the_live_api_and_rollback_container(applied):
    park_retired(applied)
    prune(applied, confirm=True)
    assert set(applied.containers) == {"api", BACKUP, "api-pages-reverted-1"}
    assert applied.containers["api"]["running"]
    assert ("stop", "api") not in applied.calls and ("rm", "api") not in applied.calls


def test_prune_with_nothing_retired_changes_nothing(host):
    assert prune(host, confirm=True) == "No retired containers to prune"
    assert not {call[0] for call in host.calls} & {"rm", "stop", "rename", "update"}


def test_prune_reports_a_container_that_would_not_go(host):
    park_retired(host)
    host.fail.append(("rm", "api-pages-failed-2"))
    message = prune(host, confirm=True)
    assert "api-pages-failed-2" in message and "could not remove" in message
    assert "api-pages-failed-2-1" not in host.containers


def test_prune_keeps_the_newest_reverted_container_as_revert_evidence(host):
    park_retired(host)
    host.add("api-pages-reverted-0", labels={LABEL: DIGEST}, running=False)
    message = prune(host, confirm=True)
    assert "api-pages-reverted-1" in host.containers
    assert "api-pages-reverted-0" not in host.containers
    assert message.endswith(
        "; kept newest reverted container (revert recovery): api-pages-reverted-1"
    )


def test_prune_keeps_a_lone_reverted_container_even_when_the_api_is_stopped(host):
    # A revert killed after `rename api -> reverted`: the evidence must survive a prune.
    host.add("api-pages-reverted-1", labels={LABEL: DIGEST}, running=False)
    host.containers["api"]["running"] = False
    message = prune(host, confirm=True)
    assert message.startswith("No retired containers to prune; kept newest reverted")
    assert "api-pages-reverted-1" in host.containers
    assert ("rm", "api-pages-reverted-1") not in host.calls


def test_prune_dry_run_does_not_offer_the_newest_reverted_container(host):
    park_retired(host)
    message = prune(host)
    assert "Would remove: api-pages-failed-2, api-pages-failed-2-1;" in message


def test_prune_skips_a_container_removed_between_listing_and_inspect(host):
    park_retired(host)
    host.add("api-pages-failed-1", labels={LABEL: DIGEST}, running=False)
    host.fail.append(("inspect", "api-pages-failed-1"))
    message = prune(host, confirm=True)
    assert "Pruned: api-pages-failed-2, api-pages-failed-2-1" in message
    assert "api-pages-failed-1" in host.containers  # skipped, never removed


def test_prune_refuses_legacy_named_state(host):
    host.add(LEGACY_BACKUP, running=False)
    with pytest.raises(ValueError, match="Legacy-named state"):
        prune(host, confirm=True)


def test_main_prune_needs_no_live_url(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "argv", ["pages_deploy", "prune"])
    monkeypatch.setenv("PLANE_MCP_SSH", "owner@host")
    monkeypatch.setenv("PLANE_API_CONTAINER", "plane-api_1")
    monkeypatch.delenv("PLANE_LIVE_URL", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kw: calls.append((args, kw)))
    pages_deploy.main()
    assert json.loads(calls[0][1]["input"]) == {
        "operation": "prune",
        "container": "plane-api_1",
        "confirm": False,
    }
    monkeypatch.setenv("PLANE_PAGES_PRUNE", "yes")
    pages_deploy.main()
    assert json.loads(calls[1][1]["input"])["confirm"] is True


def test_failed_restart_policy_update_warns_on_stderr(host, capsys):
    host.fail.append(("update", "--restart=no", BACKUP))
    deploy("apply", host)
    err = capsys.readouterr().err
    assert "Restart policy update failed" in err and BACKUP in err
    assert "OPAQUE_SECRET" not in err


def test_successful_restart_policy_updates_stay_silent(host, capsys):
    deploy("apply", host)
    assert capsys.readouterr().err == ""


def test_apply_keeps_the_retained_original_labels_untouched(composed):
    deploy("apply", composed)
    assert composed.containers[BACKUP]["labels"] == COMPOSE


def test_derived_carries_the_compose_labels_plus_exactly_the_source_label(composed):
    deploy("apply", composed)
    assert composed.containers["api"]["labels"] == {**COMPOSE, LABEL: DIGEST}


def test_revert_keeps_every_label_set_unchanged(composed):
    deploy("apply", composed)
    deploy("revert", composed, later=5)
    assert composed.containers["api"]["labels"] == COMPOSE
    [parked] = [n for n in composed.containers if "-pages-reverted-" in n]
    assert composed.containers[parked]["labels"] == {**COMPOSE, LABEL: DIGEST}


def test_failed_apply_retires_a_failed_container_with_the_same_label_set(composed):
    composed.created_healthy = False
    with pytest.raises(ValueError):
        deploy("apply", composed)
    assert composed.containers["api"]["labels"] == COMPOSE
    [failed] = [n for n in composed.containers if "-pages-failed-" in n]
    assert composed.containers[failed]["labels"] == {**COMPOSE, LABEL: DIGEST}


def test_replacement_adds_no_label_other_than_the_source_label():
    original = FakeDocker()
    original.add("api", labels=dict(COMPOSE))
    body = pages_deploy.replacement(original.document("api"), IMAGE, "http://live:3000", DIGEST)
    assert set(body["Labels"]) - set(COMPOSE) == {LABEL}
    assert all(body["Labels"][key] == value for key, value in COMPOSE.items())


def test_prune_keeps_a_suffixed_newest_reverted_and_removes_the_older_plain(host):
    host.add("api-pages-reverted-5", labels={LABEL: DIGEST}, running=False)
    host.add("api-pages-reverted-5-1", labels={LABEL: DIGEST}, running=False)
    prune(host, confirm=True)
    assert "api-pages-reverted-5-1" in host.containers
    assert "api-pages-reverted-5" not in host.containers
