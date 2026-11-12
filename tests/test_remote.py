"""Docker CLI seam routing; output must never leak unless the caller asks for it."""

import signal
import subprocess

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
