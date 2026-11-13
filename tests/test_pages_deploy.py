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
IMAGE = "plane-toolchain-pages:" + DIGEST[:16]
BACKUP = "api-before-plane-toolchain-pages"
LABEL = "plane-toolchain.pages.source"
LEGACY_BACKUP = "api-before-terrainlink-pages"
# Tripwire lookups: one exact-name listing, then an inspect of each listed container.
PS = ("ps", "-a", "--filter", "name=api", "--format", "{{.Names}}")
LOOKUP = [PS, ("inspect", "api")]
RETAINED = (  # the post-run listing behind "retained containers: ..."
    "ps",
    "-a",
    "--filter",
    "name=^api(-before-plane-toolchain-pages|-pages-(reverted|failed)-)",
    "--format",
    "{{.Names}}",
)
HELD = ("inspect", BACKUP)  # the neutral backup label read, only when it is listed
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


UPSTREAM = "makeplane/plane-backend:v1.2.3"
UPSTREAM_DIGEST = "makeplane/plane-backend@sha256:upstream"
MUTATIONS = {"create", "stop", "start", "restart", "rename", "update", "rm", "kill"}


def build_payload(backend_image=None):
    return dict(
        operation="build",
        worker="plane-worker",
        backend_image=backend_image,
        source=SOURCE,
        digest=DIGEST,
    )


@pytest.fixture
def saltbox():
    """Local derived API image, a worker on the upstream tag and no rollback container."""
    fake = FakeDocker()
    fake.images["sha256:base"] = []
    fake.images[UPSTREAM] = [UPSTREAM_DIGEST]
    fake.add("plane-api", labels={"owner": "kept"})
    fake.add("plane-worker")
    fake.containers["plane-worker"]["image"] = UPSTREAM
    return fake


def build(fake, monkeypatch, backend_image=None):
    run_program(pages_deploy.REMOTE, build_payload(backend_image), fake, monkeypatch)


def test_build_derives_from_upstream_repo_digest_and_prints_tag_and_digest(
    saltbox, monkeypatch, capsys
):
    build(saltbox, monkeypatch)
    assert capsys.readouterr().out == IMAGE + " " + DIGEST + "\n"
    assert saltbox.calls == [
        ("inspect", "plane-worker"),
        ("image", "inspect", UPSTREAM),
        ("build", "-t", IMAGE, "-"),
    ]
    [(tag, files)] = saltbox.builds
    assert tag == IMAGE and files["plane_pages.py"] == SOURCE.encode()
    assert files["Dockerfile"].decode().startswith("FROM " + UPSTREAM_DIGEST + "\n")
    assert saltbox.streams[("build", "-t", IMAGE, "-")] == (subprocess.DEVNULL, subprocess.PIPE)


def test_build_never_mutates_containers(saltbox, monkeypatch):
    before = json.dumps(saltbox.containers, sort_keys=True)
    build(saltbox, monkeypatch)
    assert not {call[0] for call in saltbox.calls} & MUTATIONS
    assert json.dumps(saltbox.containers, sort_keys=True) == before


def test_build_backend_image_override_skips_worker(saltbox, monkeypatch, capsys):
    saltbox.images["custom/backend:9"] = ["custom/backend@sha256:c"]
    build(saltbox, monkeypatch, "custom/backend:9")
    assert saltbox.calls[0] == ("image", "inspect", "custom/backend:9")
    assert saltbox.builds[0][1]["Dockerfile"].decode().startswith("FROM custom/backend@sha256:c\n")
    assert capsys.readouterr().out == IMAGE + " " + DIGEST + "\n"


@pytest.mark.parametrize("digests", [None, []])
def test_build_refuses_upstream_image_missing_or_without_digest(saltbox, monkeypatch, digests):
    if digests is None:
        del saltbox.images[UPSTREAM]
    else:
        saltbox.images[UPSTREAM] = digests
    with pytest.raises(ValueError, match="Upstream image missing or has no registry digest"):
        build(saltbox, monkeypatch)
    assert saltbox.builds == []
    assert not {call[0] for call in saltbox.calls} & MUTATIONS


def test_build_derives_tag_with_registry_port_worker_image(saltbox, monkeypatch):
    saltbox.containers["plane-worker"]["image"] = "registry:5000/x/plane-backend:v2"
    saltbox.images["makeplane/plane-backend:v2"] = [UPSTREAM_DIGEST]
    build(saltbox, monkeypatch)
    assert saltbox.calls[1] == ("image", "inspect", "makeplane/plane-backend:v2")


def test_build_remote_ships_needed_constants():
    assert "PAGES_IMAGE_PREFIX = " in pages_deploy.REMOTE
    assert "PAGES_LABEL = " in pages_deploy.REMOTE
    compile(pages_deploy.REMOTE, "<remote>", "exec")


def test_main_build_needs_only_ssh_and_ships_worker(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "argv", ["pages_deploy", "build"])
    monkeypatch.setenv("PLANE_MCP_SSH", "owner@host")
    for name in ("PLANE_API_CONTAINER", "PLANE_LIVE_URL", "PLANE_BACKEND_IMAGE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("PLANE_WORKER_CONTAINER", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kw: calls.append((args, kw)))
    pages_deploy.main()
    [(_, kw)] = calls
    sent = json.loads(kw["input"])
    assert sent["operation"] == "build" and sent["worker"] == "plane-worker"
    assert sent["backend_image"] is None and sent["digest"] == sent["digest"].lower()


def test_main_build_rejects_unsafe_worker(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["pages_deploy", "build"])
    monkeypatch.setenv("PLANE_MCP_SSH", "owner@host")
    monkeypatch.setenv("PLANE_WORKER_CONTAINER", "w;rm")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("Must not connect"))
    with pytest.raises(ValueError, match="Invalid deployment destination"):
        pages_deploy.main()


@pytest.fixture
def host():
    fake = FakeDocker()
    fake.add("api", labels={"owner": "kept"})
    return fake


@pytest.fixture
def applied():
    fake = FakeDocker()
    fake.add(BACKUP, labels={"owner": "kept"}, running=False, RestartPolicy={"Name": "no"})
    fake.add("api", labels={"owner": "kept", LABEL: DIGEST})
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
        "Original API container restored; derived container retained stopped"
        f"; retained containers: api-pages-reverted-{NOW}; run prune to remove retired ones\n"
    )
    reverted = f"api-pages-reverted-{NOW}"
    assert applied.calls == [
        *LOOKUP,
        HELD,
        ("inspect", BACKUP),
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


@pytest.fixture
def interrupted():
    """A revert died after `rename api -> reverted`: no `api`; the original waits as backup."""
    fake = FakeDocker()
    fake.add(BACKUP, labels={"owner": "kept"}, running=False, RestartPolicy={"Name": "no"})
    fake.add(
        f"api-pages-reverted-{NOW - 5}",
        labels={"owner": "kept", LABEL: DIGEST},
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


def assert_derived_back(fake):
    assert restart(fake, "api") == "unless-stopped"
    assert set(retained(fake).values()) == {"no"}
    assert fake.containers["api"]["labels"][LABEL] == DIGEST
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
    fake.add("api", labels={LABEL: DIGEST}, RestartPolicy={"Name": "always"})
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
REMIND = "; run revert before any Compose or orchestrator update of this service"
APPLIED = (
    "Pages derived image applied; API healthy; original container retained for rollback"
    "; retained containers: " + BACKUP + REMIND + "\n"
)
APPLIED_WITH_FAILED = (
    "Pages derived image applied; API healthy; original container retained for rollback"
    "; retained containers: "
    + BACKUP
    + ", api-pages-failed-"
    + str(NOW)
    + "; run prune to remove retired ones"
    + REMIND
    + "\n"
)


def assert_freshly_applied(fake, policy):
    """Final layout after a reconciled apply: one running derived `api`, original retained."""
    assert running(fake) == ["api"]
    assert fake.containers["api"]["image"] == IMAGE
    assert fake.containers["api"]["labels"] == {"owner": "kept", LABEL: DIGEST}
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


def test_remote_ignores_hup_and_pipe_before_any_docker_call(host, monkeypatch, capsys):
    import signal

    monkeypatch.setattr(signal, "signal", lambda number, handler: host.calls.append(number))
    remote("apply", host, monkeypatch)
    assert host.calls[:4] == [signal.SIGHUP, signal.SIGPIPE, signal.SIGTERM, PS]


def test_remote_finishes_apply_when_ssh_channel_is_gone(host, monkeypatch):
    class Closed:
        def write(self, text):
            raise BrokenPipeError

        def flush(self):
            raise BrokenPipeError

    monkeypatch.setattr(sys, "stdout", Closed())
    remote("apply", host, monkeypatch)
    assert running(host) == ["api"]
    assert host.containers["api"]["labels"][LABEL] == DIGEST


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
    assert applied.calls == [*LOOKUP, HELD, HEALTH, PAGES]


def test_check_rejects_label_mismatch(host, monkeypatch):
    with pytest.raises(ValueError, match="Pages image does not match source"):
        remote("check", host, monkeypatch)
    assert host.calls == LOOKUP


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


def deploy(operation, fake, later=0):
    return pages_deploy.deploy(payload(operation), now=lambda: NOW + later + 0.5, **seams(fake))


def test_seam_apply_success(host):
    assert deploy("apply", host).startswith("Pages derived image applied")
    assert host.streams[("build", "-t", IMAGE, "-")] == (subprocess.DEVNULL, subprocess.PIPE)
    assert host.containers["api"]["labels"][LABEL] == DIGEST
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


# PTC-30: legacy-named host state is refused, never read.

LEGACY_LABEL = "terrainlink.pages.source"
REFUSAL = (
    "Legacy-named state found: {}. This release no longer reads it. "
    "Migrate first with plane-toolchain at commit 1cb6153: "
    "Pages revert (apply only if check passes there), MCP apply. Or reconcile manually."
)


def snapshot(fake):
    return json.loads(json.dumps(fake.containers, sort_keys=True))


def assert_untouched(fake, before):
    """Refusal changed nothing: no mutating or build call, containers identical."""
    assert not [c for c in fake.calls if c[0] in MUTATIONS | {"build"}]
    assert fake.builds == []
    assert snapshot(fake) == before


def legacy_backup_host(with_api=True):
    fake = FakeDocker()
    fake.add(LEGACY_BACKUP, labels={"owner": "kept"}, running=False, RestartPolicy={"Name": "no"})
    if with_api:
        fake.add("api", labels={"owner": "kept", LEGACY_LABEL: DIGEST})
    return fake


@pytest.mark.parametrize("operation", ["check", "apply", "revert"])
@pytest.mark.parametrize("with_api", [True, False])
@pytest.mark.parametrize("shipped", [False, True])
def test_legacy_backup_container_refused_for_every_operation(
    operation, with_api, shipped, monkeypatch
):
    fake = legacy_backup_host(with_api)
    before = snapshot(fake)
    # Names sort: the container name precedes the label.
    names = LEGACY_BACKUP + (", " + LEGACY_LABEL if with_api else "")
    expected = REFUSAL.format(names)
    with pytest.raises(ValueError) as error:
        if shipped:
            remote(operation, fake, monkeypatch)
        else:
            deploy(operation, fake)
    assert str(error.value) == expected
    assert_untouched(fake, before)


def test_legacy_labelled_running_derived_refused_before_build():
    """A legacy-labelled derived must never be taken for an upstream original and rebuilt."""
    fake = FakeDocker()
    fake.add("api", labels={"owner": "kept", LEGACY_LABEL: DIGEST})
    before = snapshot(fake)
    with pytest.raises(ValueError) as error:
        deploy("apply", fake)
    assert str(error.value) == REFUSAL.format(LEGACY_LABEL)
    assert_untouched(fake, before)
    assert fake.containers["api"]["running"]


def test_legacy_labelled_neutral_backup_refused():
    fake = FakeDocker()
    fake.add(BACKUP, labels={LEGACY_LABEL: DIGEST}, running=False, RestartPolicy={"Name": "no"})
    before = snapshot(fake)
    with pytest.raises(ValueError) as error:
        deploy("apply", fake)
    assert str(error.value) == REFUSAL.format(LEGACY_LABEL)
    assert_untouched(fake, before)


def test_mixed_legacy_and_neutral_refused(applied):
    applied.add(
        LEGACY_BACKUP, labels={"owner": "kept"}, running=False, RestartPolicy={"Name": "no"}
    )
    before = snapshot(applied)
    for operation in ("check", "apply", "revert"):
        with pytest.raises(ValueError) as error:
            deploy(operation, applied)
        assert str(error.value) == REFUSAL.format(LEGACY_BACKUP)
    assert_untouched(applied, before)


def test_neutral_host_unaffected(applied, host):
    assert deploy("check", applied) == "Pages image and API health verified"
    assert deploy("apply", host).startswith("Pages derived image applied")
    assert deploy("revert", host, later=10).startswith("Original API container restored")


def test_remote_ships_tripwire_and_no_legacy_reads():
    compile(pages_deploy.REMOTE, "<remote>", "exec")
    assert "def legacy_state" in pages_deploy.REMOTE and "def refusal" in pages_deploy.REMOTE
    assert "def migrate" not in pages_deploy.REMOTE
    assert "def backup_for" not in pages_deploy.REMOTE
    assert "NEUTRAL_PAGES_LABEL = " in pages_deploy.REMOTE


# PTC-30: the tripwire fails closed; an unreadable host never reads as clean.


def only_reads(fake):
    assert not [c for c in fake.calls if c[0] in MUTATIONS | {"build"}]
    assert fake.builds == []


@pytest.mark.parametrize("operation", ["check", "apply", "revert"])
@pytest.mark.parametrize("shipped", [False, True])
def test_one_shot_ps_failure_refuses(operation, shipped, applied, monkeypatch):
    applied.fail.append("ps")
    before = snapshot(applied)
    with pytest.raises(subprocess.CalledProcessError):
        if shipped:
            remote(operation, applied, monkeypatch)
        else:
            deploy(operation, applied)
    only_reads(applied)
    assert snapshot(applied) == before


@pytest.mark.parametrize("operation", ["check", "apply", "revert"])
@pytest.mark.parametrize("shipped", [False, True])
def test_one_shot_inspect_failure_on_listed_neutral_backup_refuses(
    operation, shipped, applied, monkeypatch
):
    applied.fail.append(("inspect", BACKUP))
    before = snapshot(applied)
    with pytest.raises(subprocess.CalledProcessError):
        if shipped:
            remote(operation, applied, monkeypatch)
        else:
            deploy(operation, applied)
    only_reads(applied)
    assert snapshot(applied) == before


def test_one_shot_inspect_failure_on_listed_api_refuses(applied):
    applied.fail.append(("inspect", "api"))
    with pytest.raises(subprocess.CalledProcessError):
        deploy("apply", applied)
    only_reads(applied)


def test_retired_legacy_labelled_reverted_container_stays_allowed(applied):
    applied.add(
        f"api-pages-reverted-{NOW}",
        labels={LEGACY_LABEL: DIGEST},
        running=False,
        RestartPolicy={"Name": "no"},
    )
    assert deploy("check", applied) == "Pages image and API health verified"


# PTC-57: a failing remote prints its refusal line only; no host, program text or traceback.
FAKE_HOST = "owner@203.0.113.7"
REFUSAL_LINE = "ValueError: Legacy-named state found: example"


def failing_ssh(monkeypatch, code=1, error=None):
    def run(argv, **kw):
        if error:
            raise error
        print(REFUSAL_LINE, file=sys.stderr)
        if kw.get("check"):
            raise subprocess.CalledProcessError(code, argv)
        return subprocess.CompletedProcess(argv, code)

    monkeypatch.setattr(subprocess, "run", run)


def assert_no_leaks(capsys):
    seen = capsys.readouterr()
    text = seen.out + seen.err
    for leak in ("203.0.113.7", "owner@", "python3 -c", "Traceback", "CalledProcessError"):
        assert leak not in text
    return seen


def pages_env(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["pages_deploy", "check"])
    monkeypatch.setenv("PLANE_MCP_SSH", FAKE_HOST)
    monkeypatch.setenv("PLANE_API_CONTAINER", "plane-api_1")
    monkeypatch.setenv("PLANE_LIVE_URL", "http://live:3000")


def test_main_remote_refusal_prints_only_the_refusal_line(monkeypatch, capsys):
    pages_env(monkeypatch)
    failing_ssh(monkeypatch)
    with pytest.raises(SystemExit) as stop:
        pages_deploy.main()
    assert stop.value.code == 1
    assert REFUSAL_LINE in assert_no_leaks(capsys).err


def test_main_ssh_failure_and_interrupt_are_quiet(monkeypatch, capsys):
    pages_env(monkeypatch)
    failing_ssh(monkeypatch, code=255)
    with pytest.raises(SystemExit) as stop:
        pages_deploy.main()
    assert stop.value.code == 255
    assert remote_module.SSH_FAILED in assert_no_leaks(capsys).err
    failing_ssh(monkeypatch, error=KeyboardInterrupt())
    with pytest.raises(SystemExit) as stop:
        pages_deploy.main()
    assert stop.value.code == 130
    assert_no_leaks(capsys)


def test_remote_entry_reports_unexpected_failure_without_traceback():
    assert "sys.excepthook = quiet_failure" in pages_deploy.REMOTE


# PTC-68: diagnostics and housekeeping.


class FakeSocket:
    """AF_UNIX socket stand-in serving one canned HTTP reply to the real http.client."""

    instances = []

    def __init__(self, *args):
        self.timeout = "unset"
        self.closed = False
        self.sent = b""
        self.reply = b"HTTP/1.1 201 Created\r\nContent-Length: 0\r\n\r\n"
        self.connect_error = None
        FakeSocket.instances.append(self)

    def settimeout(self, seconds):
        self.timeout = seconds

    def connect(self, address):
        if self.connect_error:
            raise self.connect_error

    def sendall(self, data):
        self.sent += data

    def makefile(self, *args, **kw):
        import io

        return io.BytesIO(self.reply)

    def close(self):
        self.closed = True


@pytest.fixture
def sockets(monkeypatch):
    import socket

    FakeSocket.instances = []
    monkeypatch.setattr(socket, "socket", FakeSocket)
    return FakeSocket


def test_docker_create_sets_a_timeout_and_closes_after_success(sockets):
    pages_deploy.docker_create("api", {"Image": "x"})
    [conn] = sockets.instances
    assert isinstance(conn.timeout, (int, float)) and 0 < conn.timeout <= 60
    assert conn.closed


def test_docker_create_failure_names_the_http_status_and_still_closes(sockets, monkeypatch):
    import socket

    def refuse(*args):
        conn = FakeSocket()
        conn.reply = b"HTTP/1.1 409 Conflict\r\nContent-Length: 4\r\n\r\nname"
        return conn

    monkeypatch.setattr(socket, "socket", refuse)
    with pytest.raises(ValueError, match=r"Derived API container creation failed \(HTTP 409\)"):
        pages_deploy.docker_create("api", {"Image": "x", "Env": ["OPAQUE_SECRET=do-not-print"]})
    [conn] = sockets.instances
    assert conn.closed


def test_docker_create_timeout_is_reported_by_kind_and_closes(sockets, monkeypatch):
    import socket

    def slow(*args):
        conn = FakeSocket()
        conn.connect_error = TimeoutError("timed out")
        return conn

    monkeypatch.setattr(socket, "socket", slow)
    with pytest.raises(ValueError, match=r"creation failed \(TimeoutError\)") as error:
        pages_deploy.docker_create("api", {"Image": "x"})
    assert "/var/run" not in str(error.value)
    assert sockets.instances[0].closed


BUILD_NOISE = "\n".join("step %d" % n for n in range(40)) + "\nERROR: boom\n"


def test_build_failure_reports_a_bounded_stderr_tail_without_config(saltbox, monkeypatch):
    saltbox.error_output = BUILD_NOISE.encode()
    saltbox.fail.append("build")
    with pytest.raises(ValueError, match="Image build failed") as error:
        build(saltbox, monkeypatch)
    message = str(error.value)
    assert "ERROR: boom" in message and "step 39" in message
    assert "step 5" not in message and len(message) < 1200
    assert "\n" not in message


def test_derive_failure_in_apply_uses_the_same_build_helper_and_leaves_api_untouched(host):
    host.error_output = b"ERROR: boom\n"
    host.fail.append("build")
    with pytest.raises(ValueError, match="Image build failed.*ERROR: boom"):
        deploy("apply", host)
    assert_original_restored(host)
    assert not {call[0] for call in host.calls} & {"stop", "rename", "create"}


def test_build_streams_stdout_to_devnull_and_captures_stderr(host):
    deploy("apply", host)
    assert host.streams[("build", "-t", IMAGE, "-")] == (subprocess.DEVNULL, subprocess.PIPE)


def test_build_image_is_a_single_shipped_helper():
    source = inspect.getsource(pages_deploy.derive) + inspect.getsource(pages_deploy.build)
    assert source.count('"build"') == 0
    assert pages_deploy.build_image.__name__ in pages_deploy.REMOTE


def park_retired(fake):
    for name in ("api-pages-reverted-1", "api-pages-failed-2", "api-pages-failed-2-1"):
        fake.add(name, labels={LABEL: DIGEST}, running=False, RestartPolicy={"Name": "no"})


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


def prune(fake, confirm=False):
    data = dict(payload("prune"), confirm=confirm)
    return pages_deploy.deploy(data, now=lambda: NOW + 0.5, **seams(fake))


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
    assert set(host.containers) == {"api", BACKUP, "api-pages-failed-3", "api-other-pages-failed-9"}
    assert "Pruned: " in message and "api-pages-reverted-1" in message
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
    assert set(applied.containers) == {"api", BACKUP}
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
    assert "api-pages-reverted-1" not in host.containers


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


def test_health_wait_stops_at_the_time_budget(host):
    host.containers["api"]["healthy"] = False
    clock = iter(range(0, 10000, 40))
    sleeps = []
    with pytest.raises(ValueError, match="API health check failed"):
        pages_deploy.healthy(
            "api", host.probe, sleeps.append, attempts=45, budget=120, clock=lambda: next(clock)
        )
    assert 1 <= len(sleeps) < 10


def test_health_wait_default_budget_is_bounded():
    defaults = inspect.signature(pages_deploy.healthy).parameters
    assert 0 < defaults["budget"].default <= 150


def test_shipped_pages_and_mcp_programs_define_every_global_they_use():
    import builtins
    import symtable

    from plane_toolchain import mcp_deploy

    for source in (pages_deploy.REMOTE, mcp_deploy.REMOTE):
        tree = ast.parse(source)
        defined = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        for node in tree.body:
            if isinstance(node, ast.Import):
                defined |= {alias.asname or alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.Assign):
                defined |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        top = symtable.symtable(source, "<program>", "exec")
        used = set()
        stack = [top]
        while stack:
            table = stack.pop()
            stack.extend(table.get_children())
            used |= {
                s.get_name()
                for s in table.get_symbols()
                if s.is_global() and s.is_referenced() and table is not top
            }
        assert not used - defined - set(dir(builtins)), sorted(used - defined - set(dir(builtins)))


# PTC-67: Compose labels. Decision (verified on Docker Compose v5.5.0, see docs/extensions.md):
# Docker labels are immutable, so the retained original keeps its com.docker.compose.* labels
# whatever we do. The derived container keeps the same ones, so a `compose up` converges on one
# container per service (it removes the derived and starts the retained original) instead of
# leaving two running. These tests pin the label sets so a change here is deliberate.

COMPOSE = {
    "com.docker.compose.project": "plane",
    "com.docker.compose.service": "api",
    "com.docker.compose.container-number": "1",
    "com.docker.compose.config-hash": "abc123",
    "com.docker.compose.version": "2.0",
    "owner": "kept",
}


@pytest.fixture
def composed():
    fake = FakeDocker()
    fake.add("api", labels=dict(COMPOSE))
    return fake


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


def test_build_failure_hides_the_base_image_registry(host):
    host.error_output = (
        b"ERROR: failed to pull registry.example/plane-backend@sha256:abc\n"
        b"manifest for registry.example/plane-backend unknown\n"
    )
    host.fail.append("build")
    host.images["sha256:base"] = ["registry.example/plane-backend@sha256:abc"]
    with pytest.raises(ValueError, match="Image build failed") as error:
        deploy("apply", host)
    assert "registry.example" not in str(error.value) and "<base image>" in str(error.value)


def test_health_probe_exec_has_a_timeout_in_the_shipped_entry():
    assert "timeout=20" in pages_deploy.REMOTE
