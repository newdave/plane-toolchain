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
from plane_toolchain import pages_deploy

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
    fake.add(BACKUP, labels={"owner": "kept"}, running=False)
    fake.add("api", labels={"owner": "kept", "terrainlink.pages.source": DIGEST})
    return fake


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
        ("create", "api"),
        ("start", "api"),
        HEALTH,
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
    assert applied.calls == [("inspect", "api"), HEALTH]


def test_apply_refuses_when_rollback_container_exists(host, monkeypatch):
    host.add(BACKUP, running=False)
    with pytest.raises(ValueError, match="Existing rollback container"):
        remote("apply", host, monkeypatch)
    assert host.calls == [("inspect", "api"), ("inspect", BACKUP)]


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
    assert host.calls[-4:] == [
        ("create", "api"),
        ("inspect", "api"),
        ("rename", BACKUP, "api"),
        ("start", "api"),
    ]
    assert_original_restored(host)
    assert set(host.containers) == {"api"}


def test_apply_start_failure_retains_failed_derived_and_restores_original(host, monkeypatch):
    host.fail.append("start")
    with pytest.raises(subprocess.CalledProcessError):
        remote("apply", host, monkeypatch)
    failed = f"api-pages-failed-{NOW}"
    assert host.calls[-6:] == [
        ("start", "api"),
        ("inspect", "api"),
        ("stop", "api"),
        ("rename", "api", failed),
        ("rename", BACKUP, "api"),
        ("start", "api"),
    ]
    assert_original_restored(host)
    assert host.containers[failed]["image"] == IMAGE
    assert not host.containers[failed]["running"]


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
        ("rename", BACKUP, "api"),
        ("start", "api"),
        HEALTH,
    ]
    assert_original_restored(applied)
    assert not applied.containers[reverted]["running"]


def test_revert_refuses_when_label_changed(applied, monkeypatch):
    applied.containers["api"]["labels"] = {}
    with pytest.raises(ValueError, match="API changed; refusing rollback"):
        remote("revert", applied, monkeypatch)
    assert applied.calls == [("inspect", "api"), ("inspect", BACKUP)]


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


def assert_derived_back(fake):
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


def test_revert_recovery_nothing_startable_reraises_original(applied):
    applied.fail += [("start", "api")] * 4  # revert start, recovery, derived, original
    with pytest.raises(subprocess.CalledProcessError) as caught:
        deploy("revert", applied)
    assert caught.value.__context__ is not None
    assert not any(c["running"] for c in applied.containers.values())
    assert "api" in applied.containers and BACKUP in applied.containers


def test_check_verifies_label_and_health(applied, monkeypatch, capsys):
    remote("check", applied, monkeypatch)
    assert capsys.readouterr().out == "Pages image and API health verified\n"
    assert applied.calls == [("inspect", "api"), HEALTH]


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
    assert argv == ["ssh", "owner@host", "python3 -c " + shlex.quote(pages_deploy.REMOTE)]
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


@pytest.mark.parametrize(("host", "container"), [("-oProxy", "api"), ("h", "api;rm"), ("h", "")])
def test_main_rejects_unsafe_destination(monkeypatch, host, container):
    monkeypatch.setattr(sys, "argv", ["pages_deploy", "check"])
    monkeypatch.setenv("PLANE_MCP_SSH", host)
    monkeypatch.setenv("PLANE_API_CONTAINER", container)
    monkeypatch.setenv("PLANE_LIVE_URL", "http://live:3000")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kw: pytest.fail("Must not connect"))
    with pytest.raises(ValueError, match="Invalid deployment destination"):
        pages_deploy.main()


# Injected seams: deploy() with an in-memory Docker host, no process patching.


def seams(fake):
    def health(name):
        pages_deploy.healthy(name, fake.probe, lambda seconds: None, attempts=3)

    return dict(docker=fake.docker, probe=fake.probe, create=fake.create, health=health)


def deploy(operation, fake):
    return pages_deploy.deploy(payload(operation), now=lambda: NOW + 0.5, **seams(fake))


def test_seam_apply_success(host):
    assert deploy("apply", host).startswith("Pages derived image applied")
    assert host.streams[("build", "-t", IMAGE, "-")] == (subprocess.DEVNULL, subprocess.DEVNULL)
    assert host.containers["api"]["labels"]["terrainlink.pages.source"] == DIGEST
    assert host.containers["api"]["running"]
    assert host.containers[BACKUP]["host_config"] == host.containers["api"]["host_config"]
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
    answers = iter([False, True])
    pages_deploy.healthy("api", lambda *args: next(answers), sleeps.append)


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
