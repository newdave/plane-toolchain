"""Docker CLI seam routing; output must never leak unless the caller asks for it."""

import ast
import inspect
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
    assert runs == [(["docker", "inspect", "api"], {"check": True, "capture_output": True})]


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
    assert kw == {"check": False, **routing}


def test_docker_ok_discards_output_and_never_raises(monkeypatch):
    calls = []

    def run(argv, **kw):
        calls.append(kw)
        return subprocess.CompletedProcess(argv, 1)

    monkeypatch.setattr(subprocess, "run", run)
    assert remote.docker_ok("inspect", "missing") is False
    assert calls == [{"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}]


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
    assert done.stderr == "ValueError: Legacy-named state: x\n"


def test_guarded_entry_leaves_exit_codes_alone():
    source = remote.program([], [], remote.guarded("sys.exit(3)\n"))
    done = subprocess.run(  # noqa: S603 - local interpreter, fixed test program
        [sys.executable, "-I", "-c", source], capture_output=True, text=True
    )
    assert (done.returncode, done.stderr) == (3, "")
