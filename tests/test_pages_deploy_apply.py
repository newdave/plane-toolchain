"""Pages deploy: apply, reconcile and replacement container."""

import json
import subprocess
import pytest
from fake_docker import NOW, FakeDocker
from plane_toolchain import pages_deploy
from support_pages_deploy import (
    APPLIED,
    APPLIED_WITH_FAILED,
    BACKUP,
    DIGEST,
    HEALTH,
    HELD,
    IMAGE,
    LABEL,
    LOOKUP,
    MUTATIONS,
    NOTE,
    PAGES,
    PS,
    REMIND,
    RETAINED,
    SOURCE,
    assert_freshly_applied,
    assert_original_restored,
    hang_after,
    labelled_running,
    deploy,
    remote,
    restart,
    retained,
    running,
)
from support_pages_deploy import applied  # noqa: F401
from support_pages_deploy import host  # noqa: F401
from support_pages_deploy import retired  # noqa: F401


def test_apply_builds_derived_image_and_retains_original_stopped(host, monkeypatch, capsys):
    remote("apply", host, monkeypatch)
    assert capsys.readouterr().out == (
        "Pages derived image applied; API healthy; original container retained for rollback"
        "; retained containers: " + BACKUP + REMIND + "\n"
    )
    assert host.calls == [
        *LOOKUP,
        ("inspect", BACKUP),
        ("image", "inspect", "sha256:base"),
        ("build", "-t", IMAGE, "-"),
        ("stop", "api"),
        ("rename", "api", BACKUP),
        ("update", "--restart=no", BACKUP),
        ("create", "api"),
        ("start", "api"),
        HEALTH,
        PAGES,
        RETAINED,
    ]
    derived, original = host.containers["api"], host.containers[BACKUP]
    assert derived["image"] == IMAGE and derived["running"]
    assert derived["labels"] == {"owner": "kept", LABEL: DIGEST}
    assert derived["env"] == ["OPAQUE_SECRET=do-not-print", "CE_PLANE_LIVE_URL=http://live:3000"]
    assert derived["host_config"] == {"RestartPolicy": {"Name": "unless-stopped"}}
    assert original["labels"] == {"owner": "kept"} and not original["running"]
    # Build stdout and health output never reach the SSH terminal; build stderr is captured
    # so a failure can show a bounded tail.
    quiet = (subprocess.DEVNULL, subprocess.DEVNULL)
    assert host.streams[("build", "-t", IMAGE, "-")] == (subprocess.DEVNULL, subprocess.PIPE)
    assert host.streams[HEALTH] == quiet
    assert host.streams[PAGES] == quiet
    [(tag, files)] = host.builds
    assert tag == IMAGE
    assert files["plane_pages.py"] == SOURCE.encode()
    dockerfile = files["Dockerfile"].decode()
    assert dockerfile.startswith("FROM registry.example/plane-backend@sha256:abc\n")
    assert "COPY plane_pages.py /code/plane/api/plane_toolchain_pages.py\n" in dockerfile
    assert "from plane.api.plane_toolchain_pages import routes" in dockerfile
    assert dockerfile.endswith(
        "RUN python -m py_compile /code/plane/api/plane_toolchain_pages.py\n"
    )


def test_apply_is_idempotent_when_label_matches(applied, monkeypatch, capsys):
    remote("apply", applied, monkeypatch)
    assert capsys.readouterr().out == "Pages image already applied; API healthy\n"
    assert applied.calls == [*LOOKUP, HELD, HEALTH, PAGES]


def test_apply_refuses_when_rollback_container_exists(host, monkeypatch):
    host.add(BACKUP, running=False)
    with pytest.raises(ValueError, match="Existing rollback container"):
        remote("apply", host, monkeypatch)
    # The second inspect reads the backup's labels for reconciliation; nothing changes.
    assert host.calls == [*LOOKUP, HELD, ("inspect", BACKUP), ("inspect", BACKUP)]


def test_apply_refuses_image_without_registry_digest(host, monkeypatch):
    host.images["sha256:base"] = []
    with pytest.raises(ValueError, match="no immutable registry digest"):
        remote("apply", host, monkeypatch)
    assert ("stop", "api") not in host.calls


def test_apply_refuses_auto_remove_before_stopping_original(host, monkeypatch):
    host.add("api", labels={"owner": "kept"}, AutoRemove=True)
    with pytest.raises(ValueError, match="AutoRemove"):
        remote("apply", host, monkeypatch)
    assert ("stop", "api") not in host.calls
    assert host.containers["api"]["running"]


def test_apply_create_failure_restores_original(host, monkeypatch):
    host.fail.append("create")
    with pytest.raises(ValueError, match="creation"):
        remote("apply", host, monkeypatch)
    assert host.calls[-5:] == [
        ("create", "api"),
        ("inspect", "api"),
        ("rename", BACKUP, "api"),
        ("update", "--restart=unless-stopped", "api"),
        ("start", "api"),
    ]
    assert_original_restored(host)
    assert set(host.containers) == {"api"}


def test_apply_start_failure_retains_failed_derived_and_restores_original(host, monkeypatch):
    host.fail.append("start")
    with pytest.raises(subprocess.CalledProcessError):
        remote("apply", host, monkeypatch)
    failed = f"api-pages-failed-{NOW}"
    assert host.calls[-9:] == [
        ("start", "api"),
        ("inspect", "api"),
        ("stop", "api"),
        ("inspect", failed),
        ("rename", "api", failed),
        ("update", "--restart=no", failed),
        ("rename", BACKUP, "api"),
        ("update", "--restart=unless-stopped", "api"),
        ("start", "api"),
    ]
    assert_original_restored(host)
    assert host.containers[failed]["image"] == IMAGE
    assert not host.containers[failed]["running"]
    assert retained(host) == {failed: "no"}
    assert restart(host, "api") == "unless-stopped"


def test_apply_docker_timeout_mid_swap_rolls_back(host, monkeypatch):
    # PTC-96: a hung `docker start` of the derived container expires and the swap rolls back.
    host.hang.append("start")
    with pytest.raises(RuntimeError, match=r"^docker start timed out after 300 s$"):
        remote("apply", host, monkeypatch)
    assert host.calls[-3:] == [
        ("rename", BACKUP, "api"),
        ("update", "--restart=unless-stopped", "api"),
        ("start", "api"),
    ]
    assert_original_restored(host)
    assert all(host.timeouts.values()), host.timeouts  # every docker call had a timeout
    assert host.timeouts[("build", "-t", IMAGE, "-")] == 1800


def test_apply_hung_existence_probe_raises_instead_of_reading_absent(host, monkeypatch):
    # PTC-96 review: `probe("inspect", backup)` timing out must not look like "no backup".
    host.hang.append(("inspect", BACKUP))
    with pytest.raises(RuntimeError, match=r"^docker inspect timed out after 300 s$"):
        remote("apply", host, monkeypatch)
    assert not [c for c in host.calls if c[0] in MUTATIONS | {"build"}]
    assert host.containers["api"]["running"] and BACKUP not in host.containers


def test_apply_health_failure_retains_failed_derived_and_restores_original(host, monkeypatch):
    host.created_healthy = False
    with pytest.raises(ValueError, match="API health check failed"):
        remote("apply", host, monkeypatch)
    assert host.calls.count(HEALTH) == 45
    assert host.calls.count(("sleep", 1)) == 45
    assert_original_restored(host)
    assert not host.containers[f"api-pages-failed-{NOW}"]["running"]


def test_apply_retire_rename_failure_keeps_live_policy(host):
    host.fail.append(("rename", "api", BACKUP))
    with pytest.raises(subprocess.CalledProcessError):
        deploy("apply", host)
    assert restart(host, "api") == "unless-stopped"
    assert BACKUP not in host.containers
    # The stopped original is started again: a failed retire rename never leaves the API down.
    assert running(host) == ["api"]
    assert host.calls[-3:] == [("stop", "api"), ("rename", "api", BACKUP), ("start", "api")]


def test_apply_retire_failure_restarts_the_api_with_sigterm_held(host, monkeypatch):
    events = []
    monkeypatch.setattr(pages_deploy, "hold_sigterm", lambda: events.append("hold"))
    monkeypatch.setattr(pages_deploy, "release_sigterm", lambda *a: events.append("release"))
    real = host.probe
    host.probe = lambda *args: (events.append(args[0]), real(*args))[1]
    host.fail.append(("rename", "api", BACKUP))
    with pytest.raises(subprocess.CalledProcessError):
        deploy("apply", host)
    assert events[-3:] == ["hold", "start", "release"]


def test_apply_reconciles_missing_api_with_default_policy_note(retired, monkeypatch, capsys):
    remote("apply", retired, monkeypatch)
    assert capsys.readouterr().out == NOTE + APPLIED
    assert retired.calls[:8] == [
        PS,
        HELD,
        ("inspect", BACKUP),
        ("inspect", BACKUP),
        ("rename", BACKUP, "api"),
        ("update", "--restart=unless-stopped", "api"),
        ("start", "api"),
        HEALTH,
    ]
    assert_freshly_applied(retired, "unless-stopped")
    assert set(retired.containers) == {"api", BACKUP}


def test_apply_reconciles_missing_api_keeping_unretired_policy(retired, monkeypatch, capsys):
    # Died between the rename and `update --restart=no`: the backup still holds the live policy.
    retired.containers[BACKUP]["host_config"]["RestartPolicy"] = {
        "Name": "on-failure",
        "MaximumRetryCount": 3,
    }
    remote("apply", retired, monkeypatch)
    assert capsys.readouterr().out == APPLIED
    assert ("update", "--restart=on-failure:3", "api") in retired.calls
    assert_freshly_applied(retired, "on-failure")


def test_apply_reconcile_health_failure_leaves_original_running(retired):
    retired.containers[BACKUP]["healthy"] = False
    with pytest.raises(ValueError, match="API health check failed"):
        deploy("apply", retired)
    assert running(retired) == ["api"]
    assert retired.containers["api"]["labels"] == {"owner": "kept"}
    assert not retired.builds


@pytest.mark.parametrize("label", [DIGEST, "older-digest"])
def test_apply_reconciles_stopped_derived_as_failed_apply(retired, monkeypatch, capsys, label):
    # Died after create, before start or health: the derived copy of the live policy is kept.
    retired.add(
        "api",
        labels={"owner": "kept", LABEL: label},
        running=False,
        RestartPolicy={"Name": "always"},
    )
    stale = retired.containers["api"]["id"]
    remote("apply", retired, monkeypatch)
    assert capsys.readouterr().out == APPLIED_WITH_FAILED
    failed = f"api-pages-failed-{NOW}"
    assert retired.calls[:12] == [
        *LOOKUP,
        HELD,
        ("inspect", BACKUP),
        ("inspect", BACKUP),
        ("inspect", failed),
        ("rename", "api", failed),
        ("update", "--restart=no", failed),
        ("rename", BACKUP, "api"),
        ("update", "--restart=always", "api"),
        ("start", "api"),
        HEALTH,
    ]
    assert retired.containers[failed]["id"] == stale
    assert_freshly_applied(retired, "always")
    assert set(retired.containers) == {"api", BACKUP, failed}


def test_apply_reconcile_promote_failure_stays_reconcilable(retired, capsys):
    retired.add("api", labels={LABEL: "old"}, running=False)
    retired.fail.append(("rename", BACKUP, "api"))
    with pytest.raises(subprocess.CalledProcessError):
        deploy("apply", retired)
    # Now the missing-`api` layout: the next apply promotes the original.
    assert "api" not in retired.containers and BACKUP in retired.containers
    assert set(retained(retired).values()) == {"no"}
    assert deploy("apply", retired) == APPLIED_WITH_FAILED.rstrip("\n")
    assert capsys.readouterr().out == NOTE
    assert_freshly_applied(retired, "unless-stopped")


def test_reconcile_and_failing_apply_in_same_second_keep_both_failed(retired):
    # now() is constant: reconcile (b) and the failed apply would pick the same name.
    retired.add("api", labels={LABEL: "old"}, running=False)
    stale = retired.containers["api"]["id"]
    retired.created_healthy = False
    with pytest.raises(ValueError, match="API health check failed"):
        deploy("apply", retired)
    failed = f"api-pages-failed-{NOW}"
    assert retired.containers[failed]["id"] == stale
    assert retired.containers[failed + "-1"]["image"] == IMAGE
    assert_original_restored(retired)
    assert running(retired) == ["api"] and restart(retired, "api") == "unless-stopped"
    assert set(retained(retired).values()) == {"no"}


def test_reconcile_note_survives_a_later_apply_failure(retired, capsys):
    retired.fail.append("create")
    with pytest.raises(ValueError, match="creation"):
        deploy("apply", retired)
    assert capsys.readouterr().out == NOTE
    # The create rollback restores the promoted original with the policy it was given.
    assert running(retired) == ["api"] and restart(retired, "api") == "unless-stopped"
    assert set(retired.containers) == {"api"}


def test_apply_with_backup_and_running_current_derived_stays_idempotent(applied):
    assert deploy("apply", applied) == "Pages image already applied; API healthy"
    assert running(applied) == ["api"]


@pytest.mark.parametrize(
    "layout",
    ["running-original", "running-older-derived", "labelled-backup", "unhealthy-derived"],
)
def test_apply_refuses_other_layouts_with_backup(retired, layout):
    if layout == "running-original":
        retired.add("api", labels={"owner": "kept"})
    elif layout == "running-older-derived":
        retired.add("api", labels={LABEL: "older-digest"})
    elif layout == "labelled-backup":
        retired.containers[BACKUP]["labels"][LABEL] = "older-digest"
    else:
        retired.add("api", labels={LABEL: DIGEST}, healthy=False)
    before = json.dumps(retired.containers, sort_keys=True)
    with pytest.raises(ValueError, match="Existing rollback container|API health check failed"):
        deploy("apply", retired)
    assert json.dumps(retired.containers, sort_keys=True) == before
    assert not retired.builds


def test_apply_stopped_current_derived_without_backup_still_refuses(host):
    host.add("api", labels={LABEL: DIGEST}, running=False)
    with pytest.raises(ValueError, match="API health check failed"):
        deploy("apply", host)
    assert not host.builds


def test_replacement_drops_old_id_alias():
    fake = FakeDocker()
    fake.add("api")
    original = fake.document("api")
    original["NetworkSettings"]["Networks"]["shared"]["Aliases"] = ["api", original["Id"][:12]]
    body = pages_deploy.replacement(original, IMAGE, "http://live:3000", DIGEST)
    assert body["NetworkingConfig"]["EndpointsConfig"]["shared"]["Aliases"] == ["api"]


def test_update_failure_does_not_break_apply_or_revert(host):
    host.fail += [("update",)] * 4
    assert deploy("apply", host).startswith("Pages derived image applied")
    assert deploy("revert", host).startswith("Original API container restored")
    assert host.containers["api"]["running"]


@pytest.mark.parametrize("explicit", [False, True])
def test_replacement_clears_default_hostname_keeps_explicit(explicit):
    fake = FakeDocker()
    fake.add("api", hostname="custom-host" if explicit else None)
    original = fake.document("api")
    body = pages_deploy.replacement(original, IMAGE, "http://live:3000", DIGEST)
    if explicit:
        assert body["Hostname"] == "custom-host"
    else:
        assert original["Config"]["Hostname"] == original["Id"][:12]
        assert "Hostname" not in body


def test_applied_container_gets_fresh_hostname(host):
    old = host.containers["api"]["hostname"]
    deploy("apply", host)
    derived = host.containers["api"]
    assert derived["hostname"] != old and derived["hostname"] == derived["id"][:12]


@pytest.mark.parametrize(
    "failing",
    [[("rename", BACKUP, "api")], ["start"]],
    ids=["rollback-rename", "rollback-start"],
)
def test_apply_rollback_failure_still_restores_the_original(host, monkeypatch, failing):
    host.fail.append(("create", "api"))
    host.fail.extend(failing)
    with pytest.raises(ValueError, match="creation") as raised:
        remote("apply", host, monkeypatch)
    # The original error wins; the recovery error stays as its context.
    assert isinstance(raised.value.__context__, subprocess.CalledProcessError)
    assert_original_restored(host)
    assert BACKUP not in host.containers


def test_apply_sigterm_between_the_retire_rename_and_set_restart_restores_the_original(host):
    real = host.probe

    def terminate(*args):
        if args == ("update", "--restart=no", BACKUP):
            raise SystemExit(143)  # what the SIGTERM handler raises
        return real(*args)

    host.probe = terminate
    with pytest.raises(SystemExit):
        deploy("apply", host)
    assert_original_restored(host)
    assert BACKUP not in host.containers


@pytest.mark.parametrize("renames_failing", [1, 2], ids=["once", "persistently"])
def test_apply_rollback_never_starts_the_derived_container_when_moving_it_aside_fails(
    host, monkeypatch, capsys, renames_failing
):
    host.created_healthy = False
    host.fail.extend([("rename", "api", f"api-pages-failed-{NOW}")] * renames_failing)
    with pytest.raises(ValueError, match="API health check failed"):
        remote("apply", host, monkeypatch)
    if renames_failing == 1:
        # The retry moves the derived aside and the original comes back.
        assert_original_restored(host)
        assert not host.containers[f"api-pages-failed-{NOW}"]["running"]
    else:
        # A stopped API beats a live unhealthy derived container: both stay stopped.
        assert running(host) == []
        assert host.containers["api"]["labels"].get(LABEL) == DIGEST
        assert not host.containers[BACKUP]["running"]
        assert "Rollback incomplete" in capsys.readouterr().err


def derived_running(host):
    return [n for n, c in host.containers.items() if c["labels"].get(LABEL) and c["running"]]


def fail_after_derived_start(host, command, nth, also=()):
    """Fail the `nth` call (1-based) of `command` on `api` once the derived container started."""
    real, state = host.execute, {"started": False, "seen": 0}

    def execute(*args, **kw):
        state["started"] = state["started"] or args[:2] == ("start", "api")
        if state["started"] and args[:2] == (command, "api"):
            state["seen"] += 1
            if state["seen"] in nth:
                host.fail.append((command, "api"))
                host.fail.extend(also)
        return real(*args, **kw)

    host.execute = execute


@pytest.mark.parametrize("nth", [1, 2], ids=["main-rollback-inspect", "recovery-inspect"])
def test_apply_rollback_does_not_start_an_uninspectable_name(host, capsys, nth):
    host.created_healthy = False
    host.fail.append(("rename", "api", f"api-pages-failed-{NOW}"))
    fail_after_derived_start(host, "inspect", {nth})
    with pytest.raises(ValueError, match="API health check failed"):
        deploy("apply", host)
    assert derived_running(host) == []
    assert not host.containers[BACKUP]["running"]
    assert "Rollback incomplete" in capsys.readouterr().err


def test_apply_rollback_stop_failure_does_not_start_the_original_beside_the_derived(host, capsys):
    host.created_healthy = False
    fail_after_derived_start(host, "stop", {1, 2})  # the rollback's stop, then the recovery's
    with pytest.raises(ValueError, match="API health check failed"):
        deploy("apply", host)
    assert not host.containers[BACKUP]["running"]
    err = capsys.readouterr().err
    assert "Rollback incomplete" in err and "stop failed" in err and BACKUP in err
    # The message must not claim a container is stopped when its stop failed.
    assert "may still be running" in err and "left stopped" not in err


def test_apply_rollback_stops_the_unknown_name_when_inspect_then_ps_fail(host, capsys):
    host.created_healthy = False
    # The rollback's own inspect fails, so its rename backs onto a taken name; then the
    # recovery's ps fails: the state is unknown, but nothing under `api` may keep running.
    fail_after_derived_start(host, "inspect", {1}, also=["ps"])
    with pytest.raises(ValueError, match="API health check failed"):
        deploy("apply", host)
    assert derived_running(host) == []
    assert not host.containers["api"]["running"]
    assert not host.containers[BACKUP]["running"]
    err = capsys.readouterr().err
    assert "Rollback incomplete" in err and "api (stopped)" in err


def test_apply_rollback_incomplete_names_the_parked_derived_container(host, capsys):
    host.created_healthy = False
    host.fail.extend([("rename", BACKUP, "api")] * 2)  # the rollback's rename, then the recovery's
    with pytest.raises(ValueError, match="API health check failed"):
        deploy("apply", host)
    failed = f"api-pages-failed-{NOW}"
    assert not host.containers[failed]["running"] and "api" not in host.containers
    err = capsys.readouterr().err
    assert f"derived container {failed} (stopped)" in err
    assert f"original {BACKUP} (stopped)" in err
    assert "absent" not in err and "unknown" not in err


def test_apply_two_rollback_start_failures_leave_the_original_stopped_and_say_so(host, capsys):
    host.fail.extend([("create", "api"), "start", "start"])
    with pytest.raises(ValueError, match="creation"):
        deploy("apply", host)
    assert running(host) == []
    assert not host.containers["api"]["running"]
    assert host.containers["api"]["labels"] == {"owner": "kept"}
    assert BACKUP not in host.containers
    err = capsys.readouterr().err
    assert "Rollback incomplete" in err and "original api (stopped)" in err


# PTC-96 fix round 2: a probe that times out raises RuntimeError. Inside a rollback or recovery
# it is an unknown state: no start, a best-effort stop, "Rollback incomplete", original error.


def test_hung_inspect_in_recover_original_is_rollback_incomplete(host, monkeypatch, capsys):
    host.created_healthy = False
    failed = f"api-pages-failed-{NOW}"
    host.fail.append(("rename", "api", failed))  # the rollback's move aside fails: recovery
    hang_after(host, ("start", "api"), ("inspect", failed), nth=2)  # recovery's failed_name
    with pytest.raises(ValueError, match="API health check failed"):
        remote("apply", host, monkeypatch)
    assert ("hung", "inspect", failed) in host.calls
    assert labelled_running(host) == []
    assert not host.containers[BACKUP]["running"]
    err = capsys.readouterr().err
    assert "Rollback incomplete" in err and "Traceback" not in err


def test_hung_stop_in_recover_original_is_rollback_incomplete(host, monkeypatch, capsys):
    host.created_healthy = False
    hang_after(host, ("start", "api"), ("inspect", "api"), nth=1)  # rollback: unknown holder
    hang_after(host, ("start", "api"), ("inspect", "api"), nth=1)  # recovery's inspect too
    hang_after(host, ("start", "api"), ("stop", "api"), nth=1)  # and its best-effort stop
    with pytest.raises(ValueError, match="API health check failed"):
        remote("apply", host, monkeypatch)
    assert not host.containers[BACKUP]["running"]
    err = capsys.readouterr().err
    assert "Rollback incomplete" in err and "may still be running" in err


def test_hung_health_exec_is_retried_then_passes(host, monkeypatch, capsys):
    hang_after(host, ("start", "api"), HEALTH, nth=1)
    remote("apply", host, monkeypatch)
    assert ("hung", *HEALTH) in host.calls
    assert host.calls.count(HEALTH) == 1  # the retry, after the hung first attempt
    assert "Pages derived image applied" in capsys.readouterr().out
    assert host.containers["api"]["labels"].get(LABEL) == DIGEST


def test_hung_probe_in_the_apply_rollback_still_raises_the_original_error(
    host, monkeypatch, capsys
):
    host.fail.append(("rename", "api", BACKUP))  # the retire fails: the original is not retired
    hang_after(host, ("stop", "api"), ("start", "api"), nth=1)  # the rollback's restart hangs
    with pytest.raises(subprocess.CalledProcessError):
        remote("apply", host, monkeypatch)
    err = capsys.readouterr().err
    assert "Rollback incomplete" in err and "start timed out" in err


# PTC-96 fix round 3: the recovery message names where the derived container went, and an
# original found under `name` after a timeout is neither stopped nor misreported.


def test_rename_aside_that_took_effect_but_timed_out_is_named(host, monkeypatch, capsys):
    host.fail.append("start")  # the derived container's start fails: rollback
    failed = f"api-pages-failed-{NOW}"
    hang_after(host, ("start", "api"), ("inspect", "api"), nth=1)  # rollback: to recovery
    hang_after(host, ("start", "api"), ("rename", "api", failed), nth=1, effect=True)
    with pytest.raises(subprocess.CalledProcessError):
        remote("apply", host, monkeypatch)
    assert failed in host.containers and "api" not in host.containers
    assert labelled_running(host) == [] and not host.containers[BACKUP]["running"]
    err = capsys.readouterr().err
    assert f"derived container unknown, possibly api or {failed} (stopped)" in err
    assert f"original {BACKUP} (stopped)" in err


def test_original_start_that_took_effect_but_timed_out_is_left_running(host, monkeypatch, capsys):
    host.created_healthy = False
    failed = f"api-pages-failed-{NOW}"
    host.fail.append(("rename", "api", failed))  # the rollback's move aside fails: recovery
    hang_after(host, ("start", "api"), ("start", "api"), nth=1, effect=True)
    with pytest.raises(ValueError, match="API health check failed"):
        remote("apply", host, monkeypatch)
    assert host.containers["api"]["running"]
    assert host.containers["api"]["labels"].get(LABEL) is None  # the original, not stopped
    assert labelled_running(host) == []
    err = capsys.readouterr().err
    assert "Rollback incomplete" in err and "original api (running)" in err
    assert f"derived container {failed} (stopped)" in err
