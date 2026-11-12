"""Pages deployment remote program: characterised end to end against a fake Docker host."""

import ast
import hashlib
import inspect
import json
import shlex
import subprocess
import sys

import pytest
from fake_docker import NOW, FakeDocker, run_program
from plane_toolchain import pages_deploy, remote as remote_module

SOURCE = "ROUTES = []\n"
DIGEST = hashlib.sha256(SOURCE.encode()).hexdigest()
IMAGE = "terrainlink-plane-pages:" + DIGEST[:16]
BACKUP = "api-before-terrainlink-pages"
HEALTH = (
    "exec",
    "api",
    "python",
    "-c",
    "import urllib.request; "
    "urllib.request.urlopen('http://127.0.0.1:8000/api/instances/',timeout=3)",
)
PAGES = (
    "exec",
    "api",
    "python",
    "-c",
    "import sys, urllib.error, urllib.request\n"
    "try:\n"
    "    urllib.request.urlopen('http://127.0.0.1:8000/api/v1/workspaces/health-probe/projects/"
    "00000000-0000-0000-0000-000000000000/ce-pages/', timeout=3)\n"
    "except urllib.error.HTTPError as error:\n"
    "    sys.exit(0 if error.code in (401, 403) else 1)\n"
    "sys.exit(1)",
)


def payload(operation):
    return dict(
        operation=operation,
        container="api",
        live_url="http://live:3000",
        source=SOURCE,
        digest=DIGEST,
        replacement="import copy\n" + inspect.getsource(pages_deploy.replacement),
    )


def remote(operation, fake, monkeypatch):
    run_program(pages_deploy.REMOTE, payload(operation), fake, monkeypatch)


@pytest.fixture
def host():
    fake = FakeDocker()
    fake.add("api", labels={"owner": "kept"})
    return fake


@pytest.fixture
def applied():
    fake = FakeDocker()
    fake.add(BACKUP, labels={"owner": "kept"}, running=False, RestartPolicy={"Name": "no"})
    fake.add("api", labels={"owner": "kept", "terrainlink.pages.source": DIGEST})
    return fake


def restart(fake, name):
    return fake.containers[name]["host_config"]["RestartPolicy"]["Name"]


def retained(fake):
    """Every container that is not the live `api` must not auto-start."""
    return {n: restart(fake, n) for n in fake.containers if n != "api"}


def assert_original_restored(fake):
    assert fake.containers["api"]["labels"] == {"owner": "kept"}
    assert fake.containers["api"]["running"]
    assert BACKUP not in fake.containers


def test_apply_builds_derived_image_and_retains_original_stopped(host, monkeypatch, capsys):
    remote("apply", host, monkeypatch)
    assert capsys.readouterr().out == (
        "Pages derived image applied; API healthy; original container retained for rollback\n"
    )
    assert host.calls == [
        ("inspect", "api"),
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
    ]
    derived, original = host.containers["api"], host.containers[BACKUP]
    assert derived["image"] == IMAGE and derived["running"]
    assert derived["labels"] == {"owner": "kept", "terrainlink.pages.source": DIGEST}
    assert derived["env"] == ["OPAQUE_SECRET=do-not-print", "CE_PLANE_LIVE_URL=http://live:3000"]
    assert derived["host_config"] == {"RestartPolicy": {"Name": "unless-stopped"}}
    assert original["labels"] == {"owner": "kept"} and not original["running"]
    # Build and health output never reach the SSH terminal.
    quiet = (subprocess.DEVNULL, subprocess.DEVNULL)
    assert host.streams[("build", "-t", IMAGE, "-")] == quiet
    assert host.streams[HEALTH] == quiet
    assert host.streams[PAGES] == quiet
    [(tag, files)] = host.builds
    assert tag == IMAGE
    assert files["plane_pages.py"] == SOURCE.encode()
    dockerfile = files["Dockerfile"].decode()
    assert dockerfile.startswith("FROM registry.example/plane-backend@sha256:abc\n")
    assert "COPY plane_pages.py /code/plane/api/terrainlink_pages.py\n" in dockerfile
    assert "from plane.api.terrainlink_pages import routes" in dockerfile
    assert dockerfile.endswith("RUN python -m py_compile /code/plane/api/terrainlink_pages.py\n")


def test_apply_is_idempotent_when_label_matches(applied, monkeypatch, capsys):
    remote("apply", applied, monkeypatch)
    assert capsys.readouterr().out == "Pages image already applied; API healthy\n"
    assert applied.calls == [("inspect", "api"), HEALTH, PAGES]


def test_apply_refuses_when_rollback_container_exists(host, monkeypatch):
    host.add(BACKUP, running=False)
    with pytest.raises(ValueError, match="Existing rollback container"):
        remote("apply", host, monkeypatch)
    # The second inspect reads the backup's labels for reconciliation; nothing changes.
    assert host.calls == [("inspect", "api"), ("inspect", BACKUP), ("inspect", BACKUP)]


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


def test_apply_health_failure_retains_failed_derived_and_restores_original(host, monkeypatch):
    host.created_healthy = False
    with pytest.raises(ValueError, match="API health check failed"):
        remote("apply", host, monkeypatch)
    assert host.calls.count(HEALTH) == 45
    assert host.calls.count(("sleep", 1)) == 45
    assert_original_restored(host)
    assert not host.containers[f"api-pages-failed-{NOW}"]["running"]


def test_revert_restores_original_and_retains_derived_stopped(applied, monkeypatch, capsys):
    remote("revert", applied, monkeypatch)
    assert capsys.readouterr().out == (
        "Original API container restored; derived container retained stopped\n"
    )
    reverted = f"api-pages-reverted-{NOW}"
    assert applied.calls == [
        ("inspect", "api"),
        ("inspect", BACKUP),
        ("stop", "api"),
        ("rename", "api", reverted),
        ("update", "--restart=no", reverted),
        ("rename", BACKUP, "api"),
        ("update", "--restart=unless-stopped", "api"),
        ("start", "api"),
        HEALTH,
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
    assert applied.calls == [("inspect", "api"), ("inspect", BACKUP)]


def test_revert_accepts_derived_from_older_source(applied, monkeypatch, capsys):
    # A source upgrade changes the digest; the label still marks the container as ours.
    applied.containers["api"]["labels"]["terrainlink.pages.source"] = "older-digest"
    remote("revert", applied, monkeypatch)
    assert capsys.readouterr().out == (
        "Original API container restored; derived container retained stopped\n"
    )
    assert_original_restored(applied)
    assert retained(applied) == {f"api-pages-reverted-{NOW}": "no"}


def test_revert_without_backup_and_with_label_fails_before_changes(applied, monkeypatch):
    del applied.containers[BACKUP]
    with pytest.raises(subprocess.CalledProcessError):
        remote("revert", applied, monkeypatch)
    assert applied.calls == [("inspect", "api"), ("inspect", BACKUP), ("inspect", BACKUP)]


def test_repeat_revert_exits_zero_with_message(applied, monkeypatch, capsys):
    remote("revert", applied, monkeypatch)
    capsys.readouterr()
    applied.calls.clear()
    remote("revert", applied, monkeypatch)
    assert capsys.readouterr().out == "Original API container already restored\n"
    assert applied.calls == [("inspect", "api"), ("inspect", BACKUP)]
    assert_original_restored(applied)


@pytest.fixture
def interrupted():
    """A revert died after `rename api -> reverted`: no `api`; the original waits as backup."""
    fake = FakeDocker()
    fake.add(BACKUP, labels={"owner": "kept"}, running=False, RestartPolicy={"Name": "no"})
    fake.add(
        f"api-pages-reverted-{NOW - 5}",
        labels={"owner": "kept", "terrainlink.pages.source": DIGEST},
        running=False,
    )
    return fake


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
    interrupted.fail.append("ps")
    remote("revert", interrupted, monkeypatch)
    assert capsys.readouterr().out == NOTE + "Original API container restored\n"
    assert restart(interrupted, "api") == "unless-stopped"
    assert_original_restored(interrupted)


def test_interrupted_revert_consults_only_newest_plain_reverted(interrupted, monkeypatch, capsys):
    older, newest = f"api-pages-reverted-{NOW - 50}", f"api-pages-reverted-{NOW - 5}"
    interrupted.add(older, labels={"terrainlink.pages.source": "x"}, running=False)
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
    fake.add(BACKUP, labels={"terrainlink.pages.source": DIGEST}, running=False)
    with pytest.raises(subprocess.CalledProcessError):
        remote("revert", fake, monkeypatch)
    assert running(fake) == []
    assert not any(call[0] in ("rename", "start") for call in fake.calls)


def test_revert_missing_api_and_backup_still_raises(monkeypatch):
    fake = FakeDocker()
    with pytest.raises(subprocess.CalledProcessError):
        remote("revert", fake, monkeypatch)


def assert_derived_back(fake):
    assert restart(fake, "api") == "unless-stopped"
    assert set(retained(fake).values()) == {"no"}
    assert fake.containers["api"]["labels"]["terrainlink.pages.source"] == DIGEST
    assert fake.containers["api"]["running"]
    assert not fake.containers[BACKUP]["running"]
    assert not [name for name in fake.containers if "pages-reverted" in name]


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


def policy_applied(fake):
    fake.add("api", labels={"terrainlink.pages.source": DIGEST}, RestartPolicy={"Name": "always"})
    fake.containers[BACKUP]["host_config"]["RestartPolicy"] = {"Name": "no"}


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


def test_apply_retire_rename_failure_keeps_live_policy(host):
    host.fail.append(("rename", "api", BACKUP))
    with pytest.raises(subprocess.CalledProcessError):
        deploy("apply", host)
    assert restart(host, "api") == "unless-stopped"
    assert BACKUP not in host.containers
    # The stopped original is started again: a failed retire rename never leaves the API down.
    assert running(host) == ["api"]
    assert host.calls[-3:] == [("stop", "api"), ("rename", "api", BACKUP), ("start", "api")]


def running(fake):
    return [name for name, container in fake.containers.items() if container["running"]]


NOTE = "Restart policy set to unless-stopped; adjust if your deployment differs\n"
APPLIED = "Pages derived image applied; API healthy; original container retained for rollback\n"


def assert_freshly_applied(fake, policy):
    """Final layout after a reconciled apply: one running derived `api`, original retained."""
    assert running(fake) == ["api"]
    assert fake.containers["api"]["image"] == IMAGE
    assert fake.containers["api"]["labels"] == {"owner": "kept", "terrainlink.pages.source": DIGEST}
    assert restart(fake, "api") == policy
    assert fake.containers[BACKUP]["labels"] == {"owner": "kept"}
    assert set(retained(fake).values()) == {"no"}


@pytest.fixture
def retired():
    """A previous apply died after `rename api -> backup`: no container is named `api`."""
    fake = FakeDocker()
    fake.add(BACKUP, labels={"owner": "kept"}, running=False, RestartPolicy={"Name": "no"})
    return fake


def test_apply_reconciles_missing_api_with_default_policy_note(retired, monkeypatch, capsys):
    remote("apply", retired, monkeypatch)
    assert capsys.readouterr().out == NOTE + APPLIED
    assert retired.calls[:7] == [
        ("inspect", "api"),
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
        labels={"owner": "kept", "terrainlink.pages.source": label},
        running=False,
        RestartPolicy={"Name": "always"},
    )
    stale = retired.containers["api"]["id"]
    remote("apply", retired, monkeypatch)
    assert capsys.readouterr().out == APPLIED
    failed = f"api-pages-failed-{NOW}"
    assert retired.calls[:10] == [
        ("inspect", "api"),
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
    retired.add("api", labels={"terrainlink.pages.source": "old"}, running=False)
    retired.fail.append(("rename", BACKUP, "api"))
    with pytest.raises(subprocess.CalledProcessError):
        deploy("apply", retired)
    # Now the missing-`api` layout: the next apply promotes the original.
    assert "api" not in retired.containers and BACKUP in retired.containers
    assert set(retained(retired).values()) == {"no"}
    assert deploy("apply", retired) == APPLIED.rstrip("\n")
    assert capsys.readouterr().out == NOTE
    assert_freshly_applied(retired, "unless-stopped")


def test_reconcile_and_failing_apply_in_same_second_keep_both_failed(retired):
    # now() is constant: reconcile (b) and the failed apply would pick the same name.
    retired.add("api", labels={"terrainlink.pages.source": "old"}, running=False)
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
        retired.add("api", labels={"terrainlink.pages.source": "older-digest"})
    elif layout == "labelled-backup":
        retired.containers[BACKUP]["labels"]["terrainlink.pages.source"] = "older-digest"
    else:
        retired.add("api", labels={"terrainlink.pages.source": DIGEST}, healthy=False)
    before = json.dumps(retired.containers, sort_keys=True)
    with pytest.raises(ValueError, match="Existing rollback container|API health check failed"):
        deploy("apply", retired)
    assert json.dumps(retired.containers, sort_keys=True) == before
    assert not retired.builds


def test_apply_stopped_current_derived_without_backup_still_refuses(host):
    host.add("api", labels={"terrainlink.pages.source": DIGEST}, running=False)
    with pytest.raises(ValueError, match="API health check failed"):
        deploy("apply", host)
    assert not host.builds


def test_remote_ignores_hup_and_pipe_before_any_docker_call(host, monkeypatch, capsys):
    import signal

    monkeypatch.setattr(signal, "signal", lambda number, handler: host.calls.append(number))
    remote("apply", host, monkeypatch)
    assert host.calls[:3] == [signal.SIGHUP, signal.SIGPIPE, ("inspect", "api")]


def test_remote_finishes_apply_when_ssh_channel_is_gone(host, monkeypatch):
    class Closed:
        def write(self, text):
            raise BrokenPipeError

        def flush(self):
            raise BrokenPipeError

    monkeypatch.setattr(sys, "stdout", Closed())
    remote("apply", host, monkeypatch)
    assert running(host) == ["api"]
    assert host.containers["api"]["labels"]["terrainlink.pages.source"] == DIGEST


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


RECOVERY_STEPS = [("rename", "api", BACKUP), ("rename", f"api-pages-reverted-{NOW}", "api")]


@pytest.mark.parametrize("step", [*RECOVERY_STEPS, ("start", "api")])
def test_revert_recovery_step_failing_twice_still_leaves_one_running_api(applied, step):
    applied.containers[BACKUP]["healthy"] = False
    applied.fail += [step] * 2
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        deploy("revert", applied)
    running = [n for n, c in applied.containers.items() if c["running"]]
    assert running and "api" in running and len(running) == 1
    assert not [n for n in applied.containers if n.endswith("-aside")]
    assert BACKUP in applied.containers or not applied.containers["api"]["labels"].get(
        "terrainlink.pages.source"
    )


def test_revert_parked_original_returns_to_backup_name(applied):
    applied.containers[BACKUP]["healthy"] = False
    # revert start fails, recovery rename fails, then the original start fails.
    applied.fail += [("start", "api"), ("rename", "api", BACKUP), ("start", "api")]
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        deploy("revert", applied)
    assert not [n for n in applied.containers if n.endswith("-aside")]
    assert BACKUP in applied.containers
    assert [n for n, c in applied.containers.items() if c["running"]] == ["api"]


def test_revert_recovery_nothing_startable_reraises_original(applied):
    applied.fail += [("start", "api")] * 5  # revert, recovery, then the cascade starts
    with pytest.raises(subprocess.CalledProcessError) as caught:
        deploy("revert", applied)
    assert caught.value.__context__ is not None
    assert not any(c["running"] for c in applied.containers.values())
    assert "api" in applied.containers and BACKUP in applied.containers


def test_check_verifies_label_and_health(applied, monkeypatch, capsys):
    remote("check", applied, monkeypatch)
    assert capsys.readouterr().out == "Pages image and API health verified\n"
    assert applied.calls == [("inspect", "api"), HEALTH, PAGES]


def test_check_rejects_label_mismatch(host, monkeypatch):
    with pytest.raises(ValueError, match="Pages image does not match source"):
        remote("check", host, monkeypatch)
    assert host.calls == [("inspect", "api")]


def test_unknown_operation_and_missing_container_fail(host, monkeypatch):
    with pytest.raises(ValueError, match="Unknown operation"):
        remote("rebuild", host, monkeypatch)
    del host.containers["api"]
    with pytest.raises(subprocess.CalledProcessError):
        remote("check", host, monkeypatch)


def test_remote_never_prints_runtime_configuration(host, monkeypatch, capsys):
    remote("apply", host, monkeypatch)
    remote("check", host, monkeypatch)
    output = capsys.readouterr()
    assert "do-not-print" not in output.out + output.err


def test_remote_program_is_stdlib_only():
    modules = set()
    for node in ast.walk(ast.parse(pages_deploy.REMOTE)):
        if isinstance(node, ast.Import):
            modules |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module.split(".")[0])
    assert modules <= set(sys.stdlib_module_names)


def test_main_ships_fixed_program_and_source_digest_over_ssh(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "argv", ["pages_deploy", "check"])
    monkeypatch.setenv("PLANE_MCP_SSH", "owner@host")
    monkeypatch.setenv("PLANE_API_CONTAINER", "plane-api_1")
    monkeypatch.setenv("PLANE_LIVE_URL", "http://live:3000")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kw: calls.append((args, kw)))
    pages_deploy.main()
    [((argv,), kw)] = calls
    assert argv[0] == "ssh" and argv[1:3] == ["-o", "BatchMode=yes"]
    assert argv[-3:] == ["--", "owner@host", "python3 -c " + shlex.quote(pages_deploy.REMOTE)]
    assert argv == remote_module.ssh_argv("owner@host", pages_deploy.REMOTE)
    assert kw["text"] and kw["check"]
    sent = json.loads(kw["input"])
    source = (pages_deploy.Path(pages_deploy.__file__).with_name("pages_backend.py")).read_text()
    assert sent == dict(
        operation="check",
        container="plane-api_1",
        live_url="http://live:3000",
        source=source,
        digest=hashlib.sha256(source.encode()).hexdigest(),
        replacement="import copy\n" + inspect.getsource(pages_deploy.replacement),
    )


@pytest.mark.parametrize(
    ("host", "container"), [("-oProxy", "api"), ("h", "api;rm"), ("h", "api x")]
)
def test_main_rejects_unsafe_destination(monkeypatch, host, container):
    monkeypatch.setattr(sys, "argv", ["pages_deploy", "check"])
    monkeypatch.setenv("PLANE_MCP_SSH", host)
    monkeypatch.setenv("PLANE_API_CONTAINER", container)
    monkeypatch.setenv("PLANE_LIVE_URL", "http://live:3000")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kw: pytest.fail("Must not connect"))
    with pytest.raises(ValueError, match="Invalid deployment destination"):
        pages_deploy.main()


ENV = {
    "PLANE_MCP_SSH": "secret-owner@secret-host",
    "PLANE_API_CONTAINER": "secretapi",
    "PLANE_LIVE_URL": "https://secret-live.example:3000",
}


def main_exit(monkeypatch, capsys, env):
    monkeypatch.setattr(sys, "argv", ["pages_deploy", "check"])
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kw: pytest.fail("Must not connect"))
    with pytest.raises(SystemExit) as caught:
        pages_deploy.main()
    output = capsys.readouterr()
    assert output.out == "" and "secret" not in str(caught.value.code) + output.err
    return caught.value.code


@pytest.mark.parametrize("missing", list(ENV))
@pytest.mark.parametrize("empty", [False, True])
def test_main_names_missing_environment_variable_only(monkeypatch, capsys, missing, empty):
    env = {name: value for name, value in ENV.items() if name != missing}
    if empty:
        env[missing] = ""
    assert main_exit(monkeypatch, capsys, env) == "Missing environment variable: " + missing


@pytest.mark.parametrize(
    "url",
    [
        "ftp://secret-live:3000",
        "secret-live:3000",
        "http://",
        "https://user:secret@live",
        "https://secret@live",
        "https://live/?token=secret",
        "https://live/#secret",
        "https://live:secret",
        "https://secret live",
        "https://live\\secret",
    ],
)
def test_main_rejects_invalid_live_url_without_echoing_it(monkeypatch, capsys, url):
    assert main_exit(monkeypatch, capsys, ENV | {"PLANE_LIVE_URL": url}) == "Invalid PLANE_LIVE_URL"


# Injected seams: deploy() with an in-memory Docker host, no process patching.


def seams(fake):
    def health(name, pages=True):
        pages_deploy.healthy(name, fake.probe, lambda seconds: None, attempts=3, pages=pages)

    return dict(docker=fake.docker, probe=fake.probe, create=fake.create, health=health)


def deploy(operation, fake):
    return pages_deploy.deploy(payload(operation), now=lambda: NOW + 0.5, **seams(fake))


def test_seam_apply_success(host):
    assert deploy("apply", host).startswith("Pages derived image applied")
    assert host.streams[("build", "-t", IMAGE, "-")] == (subprocess.DEVNULL, subprocess.DEVNULL)
    assert host.containers["api"]["labels"]["terrainlink.pages.source"] == DIGEST
    assert host.containers["api"]["running"]
    assert restart(host, BACKUP) == "no"
    assert restart(host, "api") == "unless-stopped"
    assert not host.containers[BACKUP]["running"]


@pytest.mark.parametrize("failure", ["create", "start", "health"])
def test_seam_apply_failure_restores_original(host, failure):
    if failure == "health":
        host.created_healthy = False
    else:
        host.fail.append(failure)
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        deploy("apply", host)
    assert_original_restored(host)
    failed = f"api-pages-failed-{NOW}"
    assert (failed in host.containers) == (failure != "create")


def test_seam_apply_idempotent_and_refuses_existing_backup(applied, host):
    assert deploy("apply", applied) == "Pages image already applied; API healthy"
    assert ("build", "-t", IMAGE, "-") not in applied.calls
    host.add(BACKUP, running=False)
    with pytest.raises(ValueError, match="Existing rollback container"):
        deploy("apply", host)
    assert host.containers["api"]["running"]


def test_seam_revert_success_and_check_label_mismatch(applied):
    assert deploy("check", applied) == "Pages image and API health verified"
    assert deploy("revert", applied).startswith("Original API container restored")
    assert_original_restored(applied)
    with pytest.raises(ValueError, match="does not match source"):
        deploy("check", applied)


def test_healthy_retries_then_raises():
    attempts, sleeps = [], []
    with pytest.raises(ValueError, match="API health check failed"):
        pages_deploy.healthy("api", lambda *args: attempts.append(args) or False, sleeps.append)
    assert len(attempts) == 45 and sleeps == [1] * 45
    answers = iter([False, True, True])
    pages_deploy.healthy("api", lambda *args: next(answers), sleeps.append)


def run_health(fake, attempts=2):
    pages_deploy.healthy("api", fake.probe, lambda seconds: None, attempts=attempts)


@pytest.mark.parametrize("status", [401, 403])
def test_healthy_accepts_auth_required_pages_route(host, status):
    host.containers["api"]["pages_status"] = status
    run_health(host)
    assert host.calls == [HEALTH, PAGES]


@pytest.mark.parametrize("status", [404, 200, 500, 502])
def test_healthy_rejects_other_pages_statuses(host, status):
    host.containers["api"]["pages_status"] = status
    with pytest.raises(ValueError, match="API health check failed"):
        run_health(host)
    assert host.calls == [HEALTH, PAGES] * 2


def test_healthy_skips_pages_probe_while_instance_check_fails(host):
    host.containers["api"]["healthy"] = False
    with pytest.raises(ValueError, match="API health check failed"):
        run_health(host)
    assert host.calls == [HEALTH] * 2


def test_healthy_needs_both_probes_in_one_attempt(host):
    answers = iter([True, False, False, True, True])
    calls = []

    def probe(*args):
        calls.append(args[-1])
        return next(answers)

    pages_deploy.healthy("api", probe, lambda seconds: None, attempts=3)
    # Attempt 1: pages bad; attempt 2: instance bad; attempt 3: both good.
    assert len(calls) == 5


def test_seam_apply_rollback_rename_failure_leaves_original_as_backup(host):
    # Current behaviour: no further recovery when restoring the original fails.
    host.fail += ["start", ("rename", BACKUP, "api")]
    with pytest.raises(subprocess.CalledProcessError):
        deploy("apply", host)
    assert not host.containers[BACKUP]["running"]
    assert "api" not in host.containers
    assert not host.containers[f"api-pages-failed-{NOW}"]["running"]


def test_fake_fails_only_matching_call_once():
    fake = FakeDocker()
    fake.add("a")
    fake.add("b")
    fake.fail.append(("rename", "b"))
    assert fake.probe("rename", "a", "c")
    assert not fake.probe("rename", "b", "d")
    assert fake.probe("rename", "b", "d")
    assert fake.fail == []
