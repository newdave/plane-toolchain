"""Docker CLI seam routing; output must never leak unless the caller asks for it."""

import ast
import inspect
import re
import signal
import subprocess
import sys
import textwrap

import pytest
from plane_toolchain import remote


@pytest.fixture
def runs(monkeypatch):
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, b"out" if kw.get("capture_output") else None)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_docker_captures_output_by_default(runs):
    assert remote.docker("inspect", "api") == b"out"
    assert runs == [
        (["docker", "inspect", "api"], {"check": True, "capture_output": True, "timeout": 300})
    ]


@pytest.mark.parametrize(
    "routing",
    [
        {"stdout": subprocess.DEVNULL},
        {"stderr": subprocess.DEVNULL},
        {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL},
    ],
)
def test_docker_respects_caller_routing_without_capture(runs, routing):
    assert remote.docker("build", "-", check=False, **routing) == b""
    [(argv, kw)] = runs
    assert kw == {"check": False, "timeout": 1800, **routing}  # build gets the long timeout


def test_docker_ok_discards_output_and_never_raises(monkeypatch):
    calls = []

    def run(argv, **kw):
        calls.append(kw)
        return subprocess.CompletedProcess(argv, 1)

    monkeypatch.setattr(subprocess, "run", run)
    assert remote.docker_ok("inspect", "missing") is False
    assert calls == [{"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "timeout": 300}]


def test_ssh_argv_never_prompts_and_ends_options_before_host():
    assert remote.ssh_argv("owner@host", "print(1)") == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=4",
        "-o",
        "LogLevel=QUIET",
        "--",
        "owner@host",
        "python3 -c 'print(1)'",
    ]


class Closed:
    """An SSH channel that went away: every write fails."""

    def write(self, text):
        raise BrokenPipeError

    def flush(self):
        raise BrokenPipeError


def test_say_ignores_a_closed_channel(capsys):
    remote.say("done")
    assert capsys.readouterr().out == "done\n"
    remote.say("lost", Closed())  # Must not raise mid-operation.


def entry_marker():
    calls.append("entry")  # noqa: F821 - provided by the program namespace


def test_program_ignores_hup_and_pipe_before_entry(monkeypatch):
    calls = []
    monkeypatch.setattr(signal, "signal", lambda number, handler: calls.append((number, handler)))
    source = remote.program(["json"], [entry_marker], "entry_marker()\n")
    namespace = {"__name__": "__main__", "calls": calls}
    exec(compile(source, "<string>", "exec"), namespace)  # noqa: S102 - assembled test program
    assert calls == [(signal.SIGHUP, signal.SIG_IGN), (signal.SIGPIPE, signal.SIG_IGN), "entry"]


def test_program_emits_constants_before_functions():
    source = remote.program(["os"], [remote.say], "say(NAME)\n", {"NAME": "a'b", "N": 3})
    assert source.index('NAME = "a\'b"') < source.index("def say")
    assert "N = 3\n" in source
    compile(source, "<string>", "exec")


@pytest.mark.parametrize("module", ["pages_deploy", "mcp_deploy"])
def test_remote_compiles_standalone_with_legacy_constants(module):
    from plane_toolchain import legacy, mcp_deploy, pages_deploy

    source = {"pages_deploy": pages_deploy, "mcp_deploy": mcp_deploy}[module].REMOTE
    compile(source, "<string>", "exec")
    assert "import plane_toolchain" not in source and "from plane_toolchain" not in source
    names = legacy.PAGES if module == "pages_deploy" else legacy.MCP
    for name, value in names.items():
        assert f"{name} = {value!r}\n" in source


def legacy_names():
    from plane_toolchain import legacy

    return {n for n in vars(legacy) if n.isupper()}


def unshipped(source):
    """Legacy constants read inside functions of `source` but never assigned at module level."""
    tree = ast.parse(source)
    shipped = {
        t.id
        for n in tree.body
        if isinstance(n, ast.Assign)
        for t in n.targets
        if isinstance(t, ast.Name)
    }
    names = legacy_names()
    used = {
        n.id
        for fn in tree.body
        if isinstance(fn, ast.FunctionDef)
        for n in ast.walk(fn)
        if isinstance(n, ast.Name) and n.id in names
    }
    return used - shipped


def test_unshipped_detects_reduced_constants_mapping():
    from plane_toolchain import legacy, pages_deploy

    full = remote.program(["json"], [pages_deploy.source_label], "pass\n", legacy.PAGES)
    reduced = remote.program(["json"], [pages_deploy.source_label], "pass\n", {})
    assert unshipped(full) == set()
    assert unshipped(reduced) == {"NEUTRAL_PAGES_LABEL"}


@pytest.mark.parametrize("module", ["pages_deploy", "mcp_deploy"])
def test_remote_ships_every_legacy_constant_its_functions_read(module):
    from plane_toolchain import mcp_deploy, pages_deploy

    remote_source = {"pages_deploy": pages_deploy, "mcp_deploy": mcp_deploy}[module].REMOTE
    assert unshipped(remote_source) == set()


def test_pages_replacement_namespace_seeds_its_legacy_constants():
    from plane_toolchain import pages_deploy

    def names(node):
        return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} & legacy_names()

    used = names(ast.parse(textwrap.dedent(inspect.getsource(pages_deploy.replacement))))
    apply_tree = ast.parse(textwrap.dedent(inspect.getsource(pages_deploy.derive)))
    seeded = {
        key.value
        for node in ast.walk(apply_tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "namespace" for t in node.targets)
        and isinstance(node.value, ast.Dict)
        for key in node.value.keys
    }
    assert used and used <= seeded


HOST = "owner@203.0.113.7"
PROGRAM_TEXT = "print('secret-program-text')"
REFUSAL = "ValueError: Legacy-named state found: example"


def failing_run(code, message=REFUSAL, error=None):
    def run(argv, **kw):
        if error:
            raise error
        print(message, file=sys.stderr)
        if kw.get("check", False) and code:
            raise subprocess.CalledProcessError(code, argv)
        return subprocess.CompletedProcess(argv, code)

    return run


def assert_quiet(capsys):
    seen = capsys.readouterr()
    text = seen.out + seen.err
    for leak in ("203.0.113.7", "owner@", PROGRAM_TEXT, "Traceback", "CalledProcessError"):
        assert leak not in text
    return seen


def test_run_remote_failure_keeps_remote_message_and_hides_argv(monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", failing_run(1))
    with pytest.raises(SystemExit) as stop:
        remote.run_remote(HOST, PROGRAM_TEXT, {})
    assert stop.value.code == 1
    assert REFUSAL in assert_quiet(capsys).err


def test_run_remote_ssh_failure_is_generic(monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", failing_run(255, message="ssh failed"))
    with pytest.raises(SystemExit) as stop:
        remote.run_remote(HOST, PROGRAM_TEXT, {})
    assert stop.value.code == 255
    assert remote.SSH_FAILED in assert_quiet(capsys).err


def test_run_remote_interrupt_exits_130_without_traceback(monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", failing_run(0, error=KeyboardInterrupt()))
    with pytest.raises(SystemExit) as stop:
        remote.run_remote(HOST, PROGRAM_TEXT, {})
    assert stop.value.code == 130
    assert remote.INTERRUPTED in assert_quiet(capsys).err


def test_run_remote_success_returns_code_and_prints_nothing(monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0))
    assert remote.run_remote(HOST, PROGRAM_TEXT, {}) == 0
    assert remote.run_remote(HOST, PROGRAM_TEXT, {}, check=False) == 0
    assert capsys.readouterr() == ("", "")


def test_run_remote_unchecked_returns_remote_code(monkeypatch):
    monkeypatch.setattr(subprocess, "run", failing_run(7))
    assert remote.run_remote(HOST, PROGRAM_TEXT, {}, check=False) == 7


def test_guarded_entry_prints_type_and_message_not_traceback():
    source = remote.program([], [], remote.guarded("raise ValueError('Legacy-named state: x')\n"))
    done = subprocess.run(  # noqa: S603 - local interpreter, fixed test program
        [sys.executable, "-I", "-c", source], capture_output=True, text=True
    )
    assert done.returncode == 1
    assert re.fullmatch(
        r"ValueError: Legacy-named state: x \(in <module>, line \d+\)\n", done.stderr
    )


def test_guarded_entry_leaves_exit_codes_alone():
    source = remote.program([], [], remote.guarded("sys.exit(3)\n"))
    done = subprocess.run(  # noqa: S603 - local interpreter, fixed test program
        [sys.executable, "-I", "-c", source], capture_output=True, text=True
    )
    assert (done.returncode, done.stderr) == (3, "")


def test_run_remote_signal_killed_ssh_exits_one(monkeypatch, capsys):
    def run(argv, **kw):
        raise subprocess.CalledProcessError(-9, argv)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(SystemExit) as stop:
        remote.run_remote(HOST, PROGRAM_TEXT, {})
    assert stop.value.code == 1
    assert_quiet(capsys)


# PTC-66: SIGTERM unwinds the shipped Pages and MCP programs through their rollbacks.


def test_guarded_entry_turns_sigterm_into_exit_143_with_one_line():
    body = "import time\nopen(sys.argv[1], 'w').write('up')\ntime.sleep(30)\n"
    source = remote.program(["time"], [], remote.guarded(body))
    done = run_until_marker(source)
    assert done.returncode == 143
    assert done.stderr == "Terminated (SIGTERM); rolling back\n"


def test_guarded_entry_ignores_a_second_sigterm_during_rollback():
    body = (
        "import time\n"
        "try:\n"
        "    open(sys.argv[1], 'w').write('up')\n"
        "    time.sleep(30)\n"
        "except BaseException:\n"
        "    open(sys.argv[1], 'w').write('rolling')\n"
        "    time.sleep(1.5)\n"
        "    print('rollback finished')\n"
        "    raise\n"
    )
    source = remote.program(["time"], [], remote.guarded(body))
    done = run_until_marker(source, second_signal="rolling")
    assert done.returncode == 143 and "rollback finished" in done.stdout


def run_until_marker(source, second_signal=None):
    import pathlib
    import tempfile
    import time

    with tempfile.TemporaryDirectory() as directory:
        marker = pathlib.Path(directory) / "marker"
        process = subprocess.Popen(  # noqa: S603 - this interpreter, a fixed test program
            [sys.executable, "-I", "-c", source, str(marker)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        wait_for(marker, "up", time)
        process.send_signal(signal.SIGTERM)
        if second_signal:
            wait_for(marker, second_signal, time)
            process.send_signal(signal.SIGTERM)
        out, err = process.communicate(timeout=20)
    return subprocess.CompletedProcess(process.args, process.returncode, out, err)


def wait_for(marker, text, time):
    for _ in range(200):
        if marker.exists() and marker.read_text() == text:
            return
        time.sleep(0.05)
    raise AssertionError("program never reached " + text)


def run_harness(tmp_path, config):
    """Run the shipped program in a real process (see sigterm_harness.py), SIGTERM it while it
    waits for health, and return (exit code, the host state its rollback left, stderr)."""
    import json
    import pathlib
    import time

    marker = tmp_path / "marker"
    config = dict(config, marker=str(marker))
    (tmp_path / "config.json").write_text(json.dumps(config))
    harness = pathlib.Path(__file__).with_name("sigterm_harness.py")
    process = subprocess.Popen(  # noqa: S603 - this interpreter, a fixed test script
        [sys.executable, str(harness), str(tmp_path / "config.json")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    wait_for(marker, "waiting", time)
    process.send_signal(signal.SIGTERM)
    out, err = process.communicate(timeout=30)
    return process.returncode, json.loads(out.splitlines()[-1]) if out.strip() else None, err


def test_pages_apply_sigterm_rolls_back_to_the_original(tmp_path):
    from support_pages_deploy import BACKUP, payload

    code, state, err = run_harness(tmp_path, {"kind": "pages", "payload": payload("apply")})
    assert code == 143 and state is not None, err
    assert "Traceback" not in err and "Terminated (SIGTERM)" in err
    assert state["containers"]["api"] == {"running": True, "labelled": False}
    assert ["rename", "api", BACKUP] in state["calls"]  # the swap really happened first
    assert state["calls"].count(["rename", BACKUP, "api"]) == 1  # then it was undone


def test_mcp_apply_sigterm_restores_bytes_and_receipt(tmp_path):
    from test_mcp_deploy import DESIRED, ORIGINAL

    host = tmp_path / "host"
    host.mkdir()
    (host / "server.py").write_bytes(ORIGINAL)
    payload = dict(operation="apply", directory=str(host), container="plane-mcp", source=DESIRED)
    code, state, err = run_harness(tmp_path, {"kind": "mcp", "payload": payload})
    assert code == 143 and state is not None, err
    assert "Traceback" not in err and "Terminated (SIGTERM)" in err
    assert (host / "server.py").read_bytes() == ORIGINAL
    assert not (host / "plane-toolchain-ce-receipt.json").exists()
    assert state["calls"].count(["restart", "plane-mcp"]) == 2  # apply, then the rollback


def test_mcp_sigterm_during_a_failure_triggered_rollback_does_not_abort_it(tmp_path):
    from test_mcp_deploy import DESIRED, ORIGINAL

    host = tmp_path / "host"
    host.mkdir()
    (host / "server.py").write_bytes(ORIGINAL)
    payload = dict(operation="apply", directory=str(host), container="plane-mcp", source=DESIRED)
    config = {"kind": "mcp", "payload": payload, "fail_restart": True, "block": 3}
    code, state, err = run_harness(tmp_path, config)  # SIGTERM lands in the rollback's health wait
    assert state is not None, err
    # The rollback ran to its end (it reports on stderr only after its health wait) ...
    assert "MCP still unhealthy after rollback" in err and "Traceback" not in err
    assert (host / "server.py").read_bytes() == ORIGINAL
    assert not (host / "plane-toolchain-ce-receipt.json").exists()
    assert state["calls"].count(["restart", "plane-mcp"]) == 2
    # ... and the deferred SIGTERM was delivered afterwards, after the failure report.
    assert code == 143 and "Terminated (SIGTERM)" in err
    assert err.index("CalledProcessError") < err.index("Terminated (SIGTERM)")


def test_pages_sigterm_during_a_failure_triggered_rollback_does_not_abort_it(tmp_path):
    from support_pages_deploy import BACKUP, payload

    # The derived container never turns healthy: apply fails and rolls back. SIGTERM lands in
    # the rename that puts the original back.
    config = {
        "kind": "pages",
        "payload": payload("apply"),
        "block_call": ["rename", BACKUP, "api"],
        "block": 3,
    }
    code, state, err = run_harness(tmp_path, config)
    assert state is not None, err
    assert "Traceback" not in err
    # The rollback ran to its end: the original runs as api again ...
    assert state["containers"]["api"] == {"running": True, "labelled": False}
    assert state["calls"].count(["rename", BACKUP, "api"]) == 1
    assert ["start", "api"] in state["calls"][state["calls"].index(["rename", BACKUP, "api"]) :]
    # ... and the deferred SIGTERM was delivered afterwards, after the failure report.
    assert code == 143 and "API health check failed" in err
    assert err.index("API health check failed") < err.index("Terminated (SIGTERM)")


def test_pages_sigterm_right_after_the_retire_rename_does_not_strand_the_api(tmp_path):
    from support_pages_deploy import BACKUP, payload

    # SIGTERM lands in the policy update that follows the rename of the original to its backup.
    config = {
        "kind": "pages",
        "payload": payload("apply"),
        "block_call": ["update", "--restart=no", BACKUP],
        "block": 3,
    }
    code, state, err = run_harness(tmp_path, config)
    assert state is not None, err
    assert "Traceback" not in err
    assert code == 143 and "Terminated (SIGTERM)" in err
    # The swap was undone: the original runs as api again and the backup name is free.
    assert state["containers"]["api"] == {"running": True, "labelled": False}
    assert BACKUP not in state["containers"]
    assert state["calls"].count(["rename", BACKUP, "api"]) == 1


def test_docker_timeout_is_overridable_and_expiry_names_only_the_command(monkeypatch):
    # PTC-96: a hung daemon must not block forever; the error carries no container name.
    seen = []

    def run(argv, **kw):
        seen.append(kw["timeout"])
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(RuntimeError) as caught:
        remote.docker("stop", "secret-container-name")
    assert str(caught.value) == "docker stop timed out after 300 s"
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    with pytest.raises(RuntimeError, match="after 7 s"):
        remote.docker("exec", "secret-container-name", timeout=7)
    # A hung probe must never read as "the container is absent": False means only that the
    # command ran and failed (PTC-96 review).
    with pytest.raises(RuntimeError, match=r"^docker inspect timed out after 300 s$"):
        remote.docker_ok("inspect", "secret-container-name")
    with pytest.raises(RuntimeError, match="after 20 s"):
        remote.docker_ok("inspect", "x", timeout=20)
    assert seen == [300, 7, 300, 20]


def test_run_remote_has_a_default_timeout_and_reports_expiry(monkeypatch, capsys):
    seen = []

    def run(argv, **kw):
        seen.append(kw["timeout"])
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(SystemExit) as stop:
        remote.run_remote(HOST, PROGRAM_TEXT, {})
    assert stop.value.code == 124
    err = assert_quiet(capsys).err
    assert remote.SSH_TIMED_OUT.format(remote.SSH_SECONDS) in err and remote.INTERRUPTED in err
    with pytest.raises(SystemExit):
        remote.run_remote(HOST, PROGRAM_TEXT, {}, timeout=5, interrupted="custom guidance")
    assert "custom guidance" in capsys.readouterr().err
    assert seen == [remote.SSH_SECONDS, 5]
