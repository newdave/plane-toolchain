"""Docker CLI seam routing; output must never leak unless the caller asks for it."""

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
