"""Pages deploy: revert, interrupted revert and recovery."""

import subprocess
import pytest
from fake_docker import NOW, FakeDocker
from support_pages_deploy import (
    BACKUP,
    hang_after,
    labelled_running,
    DIGEST,
    HEALTH,
    HELD,
    LABEL,
    LOOKUP,
    NOTE,
    RECOVERY_STEPS,
    RETAINED,
    assert_derived_back,
    assert_original_restored,
    deploy,
    policy_applied,
    remote,
    restart,
    retained,
    running,
)
from support_pages_deploy import applied  # noqa: F401
from support_pages_deploy import host  # noqa: F401
from support_pages_deploy import interrupted  # noqa: F401


def test_revert_restores_original_and_retains_derived_stopped(applied, monkeypatch, capsys):
    remote("revert", applied, monkeypatch)
    assert capsys.readouterr().out == (
        "Original API container restored; derived container retained stopped"
        f"; retained containers: api-pages-reverted-{NOW}; run prune to remove retired ones\n"
    )
    reverted = f"api-pages-reverted-{NOW}"
    assert applied.calls == [
        *LOOKUP,
        HELD,
        ("inspect", BACKUP),
        ("inspect", reverted),  # name collision check
        ("stop", "api"),
        ("rename", "api", reverted),
        ("update", "--restart=no", reverted),
        ("rename", BACKUP, "api"),
        ("update", "--restart=unless-stopped", "api"),
        ("start", "api"),
        HEALTH,
        RETAINED,
    ]
    assert_original_restored(applied)
    assert not applied.containers[reverted]["running"]
    # The backup was retired in an earlier run (`no`); revert restores the live policy.
    assert retained(applied) == {reverted: "no"}
    assert restart(applied, "api") == "unless-stopped"


def test_revert_refuses_when_label_changed(applied, monkeypatch):
    applied.containers["api"]["labels"] = {}
    with pytest.raises(ValueError, match="API changed; refusing rollback"):
        remote("revert", applied, monkeypatch)
    assert applied.calls == [*LOOKUP, HELD, ("inspect", BACKUP)]


def test_revert_accepts_derived_from_older_source(applied, monkeypatch, capsys):
    # A source upgrade changes the digest; the label still marks the container as ours.
    applied.containers["api"]["labels"][LABEL] = "older-digest"
    remote("revert", applied, monkeypatch)
    assert capsys.readouterr().out == (
        "Original API container restored; derived container retained stopped"
        f"; retained containers: api-pages-reverted-{NOW}; run prune to remove retired ones\n"
    )
    assert_original_restored(applied)
    assert retained(applied) == {f"api-pages-reverted-{NOW}": "no"}


def test_revert_without_backup_and_with_label_fails_before_changes(applied, monkeypatch):
    del applied.containers[BACKUP]
    with pytest.raises(subprocess.CalledProcessError):
        remote("revert", applied, monkeypatch)
    assert applied.calls == [*LOOKUP, ("inspect", BACKUP), ("inspect", BACKUP)]


def test_repeat_revert_exits_zero_with_message(applied, monkeypatch, capsys):
    remote("revert", applied, monkeypatch)
    capsys.readouterr()
    applied.calls.clear()
    remote("revert", applied, monkeypatch)
    assert capsys.readouterr().out == "Original API container already restored\n"
    assert applied.calls == [*LOOKUP, ("inspect", BACKUP)]
    assert_original_restored(applied)


def test_revert_finishes_interrupted_revert_with_derived_policy(interrupted, monkeypatch, capsys):
    reverted = f"api-pages-reverted-{NOW - 5}"
    interrupted.containers[reverted]["host_config"]["RestartPolicy"] = {
        "Name": "on-failure",
        "MaximumRetryCount": 3,
    }
    remote("revert", interrupted, monkeypatch)
    assert capsys.readouterr().out == "Original API container restored\n"
    assert interrupted.calls[-4:] == [
        ("rename", BACKUP, "api"),
        ("update", "--restart=on-failure:3", "api"),
        ("start", "api"),
        HEALTH,
    ]
    assert_original_restored(interrupted)
    assert restart(interrupted, "api") == "on-failure"
    assert running(interrupted) == ["api"]
    assert retained(interrupted) == {reverted: "no"}  # Retired: never auto-starts.


def test_interrupted_revert_after_retire_update_uses_default_with_note(
    interrupted, monkeypatch, capsys
):
    interrupted.containers[f"api-pages-reverted-{NOW - 5}"]["host_config"]["RestartPolicy"] = {
        "Name": "no"
    }
    remote("revert", interrupted, monkeypatch)
    assert capsys.readouterr().out == NOTE + "Original API container restored\n"
    assert restart(interrupted, "api") == "unless-stopped"
    assert_original_restored(interrupted)


def test_interrupted_revert_keeps_backup_own_policy(interrupted, monkeypatch, capsys):
    interrupted.containers[f"api-pages-reverted-{NOW - 5}"]["host_config"]["RestartPolicy"] = {
        "Name": "no"
    }
    interrupted.containers[BACKUP]["host_config"]["RestartPolicy"] = {"Name": "always"}
    remote("revert", interrupted, monkeypatch)
    assert capsys.readouterr().out == "Original API container restored\n"
    assert restart(interrupted, "api") == "always"


def test_interrupted_revert_without_reverted_container_uses_default(monkeypatch, capsys):
    fake = FakeDocker()
    fake.add(BACKUP, labels={"owner": "kept"}, running=False, RestartPolicy={"Name": "no"})
    remote("revert", fake, monkeypatch)
    assert capsys.readouterr().out == NOTE + "Original API container restored\n"
    assert restart(fake, "api") == "unless-stopped"
    assert running(fake) == ["api"]


def test_interrupted_revert_health_failure_leaves_original_running(interrupted, monkeypatch):
    interrupted.containers[BACKUP]["healthy"] = False
    with pytest.raises(ValueError, match="API health check failed"):
        remote("revert", interrupted, monkeypatch)
    assert running(interrupted) == ["api"]
    assert interrupted.containers["api"]["labels"] == {"owner": "kept"}


def test_interrupted_revert_survives_failing_ps(interrupted, monkeypatch, capsys):
    interrupted.fail.append(("ps", "-a", "--filter", "name=api-pages-reverted-"))
    remote("revert", interrupted, monkeypatch)
    assert capsys.readouterr().out == NOTE + "Original API container restored\n"
    assert restart(interrupted, "api") == "unless-stopped"
    assert_original_restored(interrupted)


def test_interrupted_revert_consults_only_newest_plain_reverted(interrupted, monkeypatch, capsys):
    older, newest = f"api-pages-reverted-{NOW - 50}", f"api-pages-reverted-{NOW - 5}"
    interrupted.add(older, labels={LABEL: "x"}, running=False)
    interrupted.add(newest + "-aside", running=False, RestartPolicy={"Name": "always"})
    interrupted.containers[newest]["host_config"]["RestartPolicy"] = {"Name": "no"}
    remote("revert", interrupted, monkeypatch)
    assert capsys.readouterr().out == NOTE + "Original API container restored\n"
    assert restart(interrupted, "api") == "unless-stopped"
    assert set(retained(interrupted).values()) == {"no"}


def test_interrupted_revert_ignores_decoy_names(interrupted, monkeypatch):
    interrupted.add(f"xapi-pages-reverted-{NOW}", running=False, RestartPolicy={"Name": "always"})
    remote("revert", interrupted, monkeypatch)
    assert restart(interrupted, "api") == "unless-stopped"
    assert restart(interrupted, f"xapi-pages-reverted-{NOW}") == "always"  # Not ours.


def test_revert_missing_api_with_labelled_backup_still_raises(monkeypatch):
    fake = FakeDocker()
    fake.add(BACKUP, labels={LABEL: DIGEST}, running=False)
    with pytest.raises(subprocess.CalledProcessError):
        remote("revert", fake, monkeypatch)
    assert running(fake) == []
    assert not any(call[0] in ("rename", "start") for call in fake.calls)


def test_revert_missing_api_and_backup_still_raises(monkeypatch):
    fake = FakeDocker()
    with pytest.raises(subprocess.CalledProcessError):
        remote("revert", fake, monkeypatch)


@pytest.mark.parametrize("failure", ["start", "health"])
def test_revert_failure_swaps_back_and_restarts_derived(applied, monkeypatch, failure):
    if failure == "start":
        applied.fail.append(("start", "api"))
    else:
        applied.containers[BACKUP]["healthy"] = False
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        remote("revert", applied, monkeypatch)
    assert_derived_back(applied)
    reverted = f"api-pages-reverted-{NOW}"
    tail = [call for call in applied.calls if call[0] in ("stop", "rename", "start")][-4:]
    assert tail == [
        ("stop", "api"),
        ("rename", "api", BACKUP),
        ("rename", reverted, "api"),
        ("start", "api"),
    ]


def test_restart_policy_round_trips_through_apply_and_revert(host):
    host.containers["api"]["host_config"]["RestartPolicy"] = {
        "Name": "on-failure",
        "MaximumRetryCount": 3,
    }
    deploy("apply", host)
    assert retained(host) == {BACKUP: "no"}
    assert host.containers["api"]["host_config"]["RestartPolicy"]["Name"] == "on-failure"
    deploy("revert", host)
    assert host.containers["api"]["host_config"]["RestartPolicy"] == {
        "Name": "on-failure",
        "MaximumRetryCount": 3,
    }
    assert set(retained(host).values()) == {"no"}


def test_revert_failure_recovery_promotes_with_policy_and_retires_with_no(applied):
    policy_applied(applied)
    applied.containers[BACKUP]["healthy"] = False
    with pytest.raises(ValueError, match="health"):
        deploy("revert", applied)
    assert restart(applied, "api") == "always"
    assert set(retained(applied).values()) == {"no"}


@pytest.mark.parametrize(
    "step", [("rename", "api", BACKUP), ("rename", f"api-pages-reverted-{NOW}", "api")]
)
def test_restore_any_cascade_keeps_retained_off_and_live_policy(applied, step):
    policy_applied(applied)
    applied.containers[BACKUP]["healthy"] = False
    applied.fail += [step] * 2
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        deploy("revert", applied)
    assert restart(applied, "api") == "always"
    assert all(v == "no" for v in retained(applied).values())
    assert not any(n.endswith("-aside") and c["running"] for n, c in applied.containers.items())


def test_recovery_rename_failure_keeps_live_policy_on_running_original(applied):
    policy_applied(applied)
    applied.containers[BACKUP]["healthy"] = False
    applied.fail.append(("rename", "api", BACKUP))
    with pytest.raises(ValueError, match="health"):
        deploy("revert", applied)
    assert applied.containers["api"]["running"]
    assert restart(applied, "api") == "always"
    assert set(retained(applied).values()) == {"no"}


@pytest.mark.parametrize("step", [("stop", "api"), ("rename", "api"), ("rename", BACKUP)])
def test_revert_early_failure_recovers_derived(applied, monkeypatch, step):
    applied.fail.append(step)
    with pytest.raises(subprocess.CalledProcessError):
        remote("revert", applied, monkeypatch)
    assert_derived_back(applied)


@pytest.mark.parametrize(
    "step", [("rename", "api", BACKUP), ("rename", f"api-pages-reverted-{NOW}", "api"), ("start",)]
)
def test_revert_recovery_failure_reraises_original_and_keeps_api_running(applied, step):
    applied.containers[BACKUP]["healthy"] = False
    if step == ("start",):
        applied.fail += [("start", "api")] * 2  # revert start and recovery start both fail
    else:
        applied.fail.append(step)
    with pytest.raises((ValueError, subprocess.CalledProcessError)) as caught:
        deploy("revert", applied)
    assert isinstance(caught.value.__context__, subprocess.CalledProcessError)
    assert "do-not-print" not in repr(caught.value)
    assert applied.containers["api"]["running"]
    running = [n for n, c in applied.containers.items() if c["running"]]
    assert len(running) == 1


@pytest.mark.parametrize("step", [*RECOVERY_STEPS, ("start", "api")])
def test_revert_recovery_step_failing_twice_still_leaves_one_running_api(applied, step):
    applied.containers[BACKUP]["healthy"] = False
    applied.fail += [step] * 2
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        deploy("revert", applied)
    running = [n for n, c in applied.containers.items() if c["running"]]
    assert running and "api" in running and len(running) == 1
    assert not [n for n in applied.containers if n.endswith("-aside")]
    assert BACKUP in applied.containers or not applied.containers["api"]["labels"].get(LABEL)


def test_revert_parked_original_returns_to_backup_name(applied):
    applied.containers[BACKUP]["healthy"] = False
    # revert start fails, recovery rename fails, then the original start fails.
    applied.fail += [("start", "api"), ("rename", "api", BACKUP), ("start", "api")]
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        deploy("revert", applied)
    assert not [n for n in applied.containers if n.endswith("-aside")]
    assert BACKUP in applied.containers
    assert [n for n, c in applied.containers.items() if c["running"]] == ["api"]


def test_revert_recovery_nothing_startable_reraises_original(applied, capsys):
    applied.fail += [("start", "api")] * 5  # revert, recovery, then the cascade starts
    with pytest.raises(subprocess.CalledProcessError) as caught:
        deploy("revert", applied)
    err = capsys.readouterr().err  # PTC-115: a cascade that gives up says so
    assert "Rollback incomplete" in err and "no API is running" in err
    assert "Run revert again" in err
    assert caught.value.__context__ is not None
    assert not any(c["running"] for c in applied.containers.values())
    assert "api" in applied.containers and BACKUP in applied.containers


def test_hung_probe_in_restore_any_is_rollback_incomplete_and_reraises(
    applied, monkeypatch, capsys
):
    # PTC-96 fix round 2: revert recovery fails over to restore_any, whose inspect hangs.
    applied.containers[BACKUP]["healthy"] = False
    applied.fail.append(("rename", "api", BACKUP))
    hang_after(applied, ("rename", "api", BACKUP), ("inspect", "api"), nth=1)
    with pytest.raises(ValueError, match="health"):
        remote("revert", applied, monkeypatch)
    assert ("hung", "inspect", "api") in applied.calls
    assert labelled_running(applied) == []
    assert not applied.containers["api"]["running"]  # unknown state: stopped, never started
    err = capsys.readouterr().err
    assert "Rollback incomplete" in err and "Traceback" not in err


def test_restore_any_rename_failure_is_reported_not_silent(applied, capsys):
    # Revert start, undo start and the first cascade start fail; so does moving api aside.
    reverted = f"api-pages-reverted-{NOW}"
    applied.fail += [("start", "api")] * 3 + [("rename", "api", reverted + "-aside")]
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        deploy("revert", applied)
    assert "Rollback incomplete" in capsys.readouterr().err
