import json
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from plane_toolchain import remote, workspace_rename

SECRETS = ("secret-owner@secret-host", "secretapi", "https://secret-live.example:3000")
ENV = {
    "PLANE_RENAME_FROM": "old-slug",
    "PLANE_RENAME_TO": "new_slug",
    "PLANE_MCP_SSH": "secret-owner@secret-host",
    "PLANE_API_CONTAINER": "secretapi",
    "PLANE_LIVE_URL": "https://secret-live.example:3000",
}
OPTIONAL = (
    "PLANE_RENAME_RECEIPT_DIR",
    "PLANE_API_URL",
    "PLANE_RENAME_ALLOW_UNDRAINED",
    "PLANE_RENAME_SNIPPET_IDLE_SECONDS",
    *workspace_rename.SERVICE_VARIABLES,
)


@pytest.fixture
def run(monkeypatch):
    """Patch subprocess.run; return the call log. Env is cleared then set from ENV."""
    calls = []
    for name in (*ENV, *OPTIONAL):
        monkeypatch.delenv(name, raising=False)
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)

    def fake(*args, **kw):
        calls.append((args, kw))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake)
    return calls


def call(operation="check"):
    with pytest.raises(SystemExit) as stop:
        workspace_rename.main([operation])
    return stop.value.code


@pytest.mark.parametrize("missing", list(ENV))
def test_main_requires_env_rename_from_to_ssh_api_container(run, monkeypatch, missing):
    monkeypatch.delenv(missing)
    assert call() == "Missing environment variable: " + missing
    assert run == []


def test_main_requires_known_operation(run):
    for argv in ([], ["deploy"], ["check", "x"]):
        with pytest.raises(SystemExit):
            workspace_rename.main(argv)
    assert run == []


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PLANE_MCP_SSH", "-oProxyCommand=x"),
        ("PLANE_API_CONTAINER", "api;rm"),
        ("PLANE_API_CONTAINER", "-api"),
        ("PLANE_API_CONTAINER", "api x"),
        ("PLANE_RENAME_FROM", "bad slug"),
        ("PLANE_RENAME_FROM", "bad\n"),
        ("PLANE_RENAME_TO", "a/b"),
        ("PLANE_RENAME_TO", "old-slug"),
        ("PLANE_LIVE_URL", "ftp://live"),
        ("PLANE_LIVE_URL", "http://u:p@live"),
        ("PLANE_API_URL", "http://u:p@api"),
        ("PLANE_WORKER_CONTAINER", "w;x"),
        ("PLANE_RENAME_RECEIPT_DIR", "relative/dir"),
    ],
)
def test_main_rejects_dash_prefixed_host_and_bad_container_and_bad_slugs(
    run, monkeypatch, name, value
):
    monkeypatch.setenv(name, value)
    message = call("apply")
    assert isinstance(message, str) and message
    assert run == []  # refused before any SSH


def test_main_ssh_argv_uses_program_and_stdin_payload(run, monkeypatch):
    monkeypatch.setenv("PLANE_RENAME_RECEIPT_DIR", "/var/lib/rename")
    monkeypatch.setenv("PLANE_API_URL", "http://api:8000")
    monkeypatch.setenv("PLANE_WEB_CONTAINER", "my-web")
    assert call("apply") == 0
    [((argv,), kw)] = run
    assert argv == remote.ssh_argv("secret-owner@secret-host", workspace_rename.REMOTE)
    assert "check" not in argv and "apply" not in argv
    snippet = workspace_rename.snippet_source()
    for secret in (*SECRETS[1:], "old-slug", "new_slug", "/var/lib/rename"):
        assert not any(secret in part for part in argv)
    assert snippet not in " ".join(argv)
    assert kw["text"] is True and kw["check"] is False
    assert json.loads(kw["input"]) == {
        "operation": "apply",
        "from": "old-slug",
        "to": "new_slug",
        "api": "secretapi",
        "snippet": snippet,
        "live_url": "https://secret-live.example:3000",
        "receipt_dir": "/var/lib/rename",
        "api_url": "http://api:8000",
        "services": {"PLANE_WEB_CONTAINER": "my-web"},
    }


@pytest.mark.parametrize("operation", ["check", "apply", "revert"])
def test_main_minimal_payload_per_operation(run, operation):
    call(operation)
    sent = json.loads(run[0][1]["input"])
    assert sent["operation"] == operation
    assert set(sent) == {"operation", "from", "to", "api", "snippet", "live_url"}


@pytest.mark.parametrize("code", [0, 1, 255])
def test_main_passes_remote_exit_code_through(run, monkeypatch, code):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=code))
    assert call() == code


def test_main_keyboard_interrupt_gives_guidance_not_traceback(run, monkeypatch, capsys):
    def boom(*args, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(subprocess, "run", boom)
    assert call("apply") == 130
    err = capsys.readouterr().err
    assert "rerun" in err and "revert" in err and "Traceback" not in err


@pytest.mark.parametrize(
    ("name", "value"),
    [("PLANE_RENAME_FROM", "bad slug"), ("PLANE_API_CONTAINER", "api;rm"), ("PLANE_MCP_SSH", "-x")],
)
def test_main_error_message_leaks_no_config(run, monkeypatch, capsys, name, value):
    monkeypatch.setenv("PLANE_RENAME_RECEIPT_DIR", "/private/receipts")
    monkeypatch.setenv(name, value)
    message = call()
    shown = str(message) + capsys.readouterr().out + capsys.readouterr().err
    for secret in (*SECRETS, "/private/receipts", value):
        assert secret not in shown


def test_entry_wrapper_failure_message_leaks_no_config(run, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["workspace_rename", "check"])
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("secret-host"))
    )
    import runpy

    monkeypatch.delitem(sys.modules, "plane_toolchain.workspace_rename")
    with pytest.raises(SystemExit) as stop:
        runpy.run_module("plane_toolchain.workspace_rename", run_name="__main__")
    assert stop.value.code == 1
    assert "secret" not in capsys.readouterr().err


def test_mise_tasks_declared():
    root = Path(__file__).resolve().parent.parent
    tasks = tomllib.loads((root / "mise.toml").read_text())["tasks"]
    for operation in ("check", "apply", "revert"):
        task = tasks["owner:plane-workspace-rename:" + operation]
        assert task["run"] == (
            "uv run --locked python -m plane_toolchain.workspace_rename " + operation
        )
    # None of the owner tasks may be a dependency of the check pipeline.
    for name, task in tasks.items():
        if not name.startswith("owner:"):
            assert not any("plane-workspace-rename" in str(dep) for dep in task.get("depends", []))
            assert "plane-workspace-rename" not in str(task.get("run", ""))


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


def test_main_passes_remote_exit_code_and_hides_argv(run, monkeypatch, capsys):
    monkeypatch.setenv("PLANE_MCP_SSH", FAKE_HOST)
    failing_ssh(monkeypatch, code=7)
    assert call("apply") == 7
    assert REFUSAL_LINE in assert_no_leaks(capsys).err


def test_main_ssh_failure_and_interrupt_are_quiet(run, monkeypatch, capsys):
    monkeypatch.setenv("PLANE_MCP_SSH", FAKE_HOST)
    failing_ssh(monkeypatch, code=255)
    assert call() == 255
    assert remote.SSH_FAILED in assert_no_leaks(capsys).err
    failing_ssh(monkeypatch, error=KeyboardInterrupt())
    assert call() == 130
    assert workspace_rename.LOCAL_INTERRUPTED in capsys.readouterr().err
