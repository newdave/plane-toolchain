"""owner:plane-mcp-mounts:check is read-only, validated and prints no secrets."""

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/plane_mcp_mounts.py"
ENV = {"PLANE_MCP_SSH": "owner@host.example", "PLANE_MCP_CONTAINER": "plane-mcp"}
OUT = "/srv/mcp\t/app/mcp\tro\n/srv/data\t/data\trw\nSECRET=hunter2\n"


@pytest.fixture
def run(tmp_path):
    """Run the script with a fake `ssh` first on PATH; return (result, recorded argv or None)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "argv.json"
    ssh = bindir / "ssh"
    ssh.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        f"open({str(log)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
        f"sys.stdout.write({OUT!r})\nsys.exit(int(os.environ['FAKE_RC']))\n"
    )
    ssh.chmod(0o755)

    def go(env, rc=0):
        full = {"PATH": f"{bindir}:{os.environ['PATH']}", "FAKE_RC": str(rc), **env}
        result = subprocess.run(  # noqa: S603 - test runs the script under test
            [sys.executable, str(SCRIPT)], env=full, capture_output=True, text=True, timeout=30
        )
        return result, json.loads(log.read_text()) if log.exists() else None

    return go


@pytest.mark.parametrize("missing", ["PLANE_MCP_SSH", "PLANE_MCP_CONTAINER"])
def test_unset_variable_fails_clearly(run, missing):
    result, argv = run({k: v for k, v in ENV.items() if k != missing})
    assert result.returncode == 1
    assert f"Missing environment variable: {missing}" in result.stderr
    assert argv is None


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PLANE_MCP_SSH", "-oProxyCommand=x"),
        ("PLANE_MCP_SSH", "host with space"),
        ("PLANE_MCP_CONTAINER", "-rm"),
        ("PLANE_MCP_CONTAINER", "a;b"),
        ("PLANE_MCP_CONTAINER", "a b"),
        ("PLANE_MCP_CONTAINER", "ünï"),
    ],
)
def test_invalid_input_refused_before_ssh(run, name, value):
    result, argv = run({**ENV, name: value})
    assert result.returncode == 1
    assert f"Invalid {name}" in result.stderr
    assert argv is None


def test_prints_only_bind_mount_rows_read_only(run):
    result, argv = run(ENV)
    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        "source\tdestination\tmode",
        "/srv/mcp\t/app/mcp\tro",
        "/srv/data\t/data\trw",
    ]
    assert "hunter2" not in result.stdout + result.stderr
    assert argv[argv.index("--") + 1] == "owner@host.example"
    assert "BatchMode=yes" in argv
    remote = argv[-1]
    assert remote.startswith("docker inspect --format ")
    assert remote.endswith(" plane-mcp")
    assert ".Mounts" in remote
    for word in (".Config", ".Env", "Labels", "rm ", "exec", "restart"):
        assert word not in remote


def test_ssh_failure_reports_without_output(run):
    result, _ = run(ENV, rc=255)
    assert result.returncode == 1
    assert "failed (exit 255)" in result.stderr
    assert "hunter2" not in result.stdout + result.stderr


def test_timeout_is_reported(monkeypatch):
    module = runpy.run_path(str(SCRIPT))

    def hang(*args, **kw):
        raise subprocess.TimeoutExpired("ssh", kw["timeout"])

    monkeypatch.setattr(module["subprocess"], "run", hang)
    with pytest.raises(SystemExit) as info:
        module["main"](ENV)
    assert "timed out after 60 s" in str(info.value)


def test_no_bind_mounts_fails(monkeypatch):
    module = runpy.run_path(str(SCRIPT))
    assert module["mounts"]("junk\nvolume-only\n") == []
    monkeypatch.setattr(
        module["subprocess"],
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, stdout="\n", stderr=""),
    )
    with pytest.raises(SystemExit) as info:
        module["main"](ENV)
    assert "No bind mounts" in str(info.value)


def test_malformed_output_reports_unparseable(monkeypatch):
    module = runpy.run_path(str(SCRIPT))
    monkeypatch.setattr(
        module["subprocess"],
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, stdout="junk\n", stderr=""),
    )
    with pytest.raises(SystemExit) as info:
        module["main"](ENV)
    assert "No parseable bind mounts" in str(info.value)


def test_container_is_shell_quoted():
    module = runpy.run_path(str(SCRIPT))
    module["CONTAINER"] = __import__("re").compile(r".+")  # bypass validation: quoting must hold
    remote = module["ssh_argv"]("h", "a;b")[-1]
    assert remote.endswith(" 'a;b'")
