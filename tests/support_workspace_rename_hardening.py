"""Shared helpers and fixtures for the split test_workspace_rename_hardening files."""

import fcntl
import os
import sys
import time
import pytest
from support_workspace_rename_deploy import Host
from plane_toolchain import workspace_rename as wr


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


def hold_lock(directory):
    """Hold the run lock the way another process would: a separate open file description."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(directory / "run.lock", os.O_RDWR | os.O_CREAT, 0o600)
    handle = os.fdopen(descriptor, "a")
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


@pytest.fixture
def fake():
    from fake_docker import FakeDocker
    from support_workspace_rename_deploy import ALL

    fake = FakeDocker()
    for name in [*ALL, "plane-api"]:
        fake.add(name)
    return fake


def backup_dir(tmp_path):
    directory = wr.secure_receipt_dir(tmp_path / "r")
    wr.new_receipt(directory, "a", "b")
    return directory


def first_index(calls, predicate):
    return next((i for i, call in enumerate(calls) if predicate(call)), None)


def is_kill(call):
    return call[:1] == ("exec",) and "-c" in call and "PLANE_RENAME_RUN" in call[-3]


def is_start(call):
    return call[0] in ("start", "restart") and call[1] != "plane-live"


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(wr, "SNIPPET_IDLE_SECONDS", 0.2)


def snippet_call(host, op="check"):
    return wr.run_snippet(
        "plane-api",
        wr.snippet_source(),
        op,
        {"old": "old-slug", "new": "new-slug"},
        wr.secure_receipt_dir(host.directory),
        host.fake.popen,
    )


def run_killer(root, token, monkeypatch, wait="0"):
    monkeypatch.setattr(sys, "argv", ["-c", token, wait, str(root)])
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    with pytest.raises(SystemExit) as done:
        exec(compile(wr.KILLER, "<killer>", "exec"), {"__name__": "__main__"})  # noqa: S102
    return done.value.code


def fake_proc(root, pid, environ):
    (root / str(pid)).mkdir()
    (root / str(pid) / "environ").write_bytes(b"\0".join(environ) + b"\0")


def probe(orm):
    from plane_toolchain import workspace_rename_backend as backend

    events = []
    ok = backend.run("probe", {}, orm, events.append, None)
    return ok, events


def mismatches(events):
    return {(e["model"], e["field"], e["reason"]): e for e in events if e["event"] == "mismatch"}


def drained_failure(host):
    """An apply that fails in step_rows: past slug_done, workers stopped, jobs left queued."""
    host.shell.live.fail = True
    with pytest.raises(RuntimeError, match="Rename stopped at slug_done"):
        host.deploy()
    assert not host.running()["plane-worker"]
    host.fake.queue_depths = [3]
