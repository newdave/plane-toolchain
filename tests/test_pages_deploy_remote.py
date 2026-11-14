"""Pages deploy: remote program, entry points, seams, health probes."""

import ast
import inspect
import json
import shlex
import subprocess
import sys
import pytest
from fake_docker import NOW, FakeDocker
from plane_toolchain import pages_deploy, remote as remote_module
from support_pages_deploy import (
    BACKUP,
    DIGEST,
    ENV,
    FakeSocket,
    HEALTH,
    HELD,
    IMAGE,
    LABEL,
    LOOKUP,
    PAGES,
    PS,
    REFUSAL_LINE,
    assert_no_leaks,
    assert_original_restored,
    deploy,
    failing_ssh,
    main_exit,
    pages_env,
    remote,
    restart,
    run_health,
    payload,
    seams,
    running,
)
from support_pages_deploy import applied  # noqa: F401
from support_pages_deploy import host  # noqa: F401
from support_pages_deploy import sockets  # noqa: F401


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


def test_check_verifies_label_and_health(applied, monkeypatch, capsys):
    remote("check", applied, monkeypatch)
    assert (
        capsys.readouterr().out == "Pages image and API health verified (digest " + DIGEST + ")\n"
    )
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
        digest=pages_deploy.shipped_digest(source),
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
    assert deploy("check", applied) == "Pages image and API health verified (digest " + DIGEST + ")"
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


def test_seam_apply_rollback_rename_failure_still_restores_original(host):
    # The recovery restores the original when the rollback itself fails (PTC-88).
    host.fail += ["start", ("rename", BACKUP, "api")]
    with pytest.raises(subprocess.CalledProcessError):
        deploy("apply", host)
    assert_original_restored(host)
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


def test_health_probe_exec_has_a_timeout_in_the_shipped_entry():
    assert "timeout=20" in pages_deploy.REMOTE


@pytest.mark.parametrize("name", ["--help", "-x", "éapi", "api١", "a b", "a;b", "", "a/b"])
def test_validate_container_name_rejects_unsafe_names(name):
    with pytest.raises(ValueError, match="Invalid deployment destination"):
        pages_deploy.validate_container_name(name)


@pytest.mark.parametrize("name", ["api", "plane-api_1", "plane.api-1", "9lives"])
def test_validate_container_name_accepts_docker_names(name):
    assert pages_deploy.validate_container_name(name) == name


@pytest.mark.parametrize("operation", ["apply", "check", "revert", "prune"])
def test_remote_deploy_rejects_an_unsafe_container_before_any_docker_call(host, operation):
    data = dict(payload(operation), container="--help")
    with pytest.raises(ValueError, match="Invalid deployment destination"):
        pages_deploy.deploy(data, now=lambda: NOW, **seams(host))
    assert host.calls == []


def test_remote_build_rejects_an_unsafe_worker_before_any_docker_call(host):
    data = dict(payload("build"), worker="--help")
    with pytest.raises(ValueError, match="Invalid deployment destination"):
        pages_deploy.deploy(data, now=lambda: NOW, **seams(host))
    assert host.calls == []


def test_docker_create_url_encodes_the_name(sockets):
    pages_deploy.docker_create("a b&c", {})
    [conn] = sockets.instances
    assert conn.sent.startswith(b"POST /containers/create?name=a%20b%26c ")
