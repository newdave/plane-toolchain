"""MCP entry-point remote program: characterised end to end against a fake host."""

import ast
import hashlib
import json
import shlex
import subprocess
import sys

import pytest
from fake_docker import FakeDocker, run_program
from plane_toolchain import mcp_deploy

ORIGINAL = b"from plane_mcp import get_header_mcp\nget_header_mcp().run()\n"
DESIRED = "print('ce tools')\n"
RESTART = ("restart", "plane-mcp")
INSPECT = ("inspect", "plane-mcp")


def mcp_host():
    fake = FakeDocker()
    fake.add("plane-mcp")
    return fake


def sha(value):
    return hashlib.sha256(value).hexdigest()


@pytest.fixture
def root(tmp_path):
    (tmp_path / "server.py").write_bytes(ORIGINAL)
    (tmp_path / "server.py").chmod(0o640)
    return tmp_path


def remote(operation, root, fake, monkeypatch, source=DESIRED):
    data = dict(operation=operation, directory=str(root), container="plane-mcp", source=source)
    run_program(mcp_deploy.REMOTE, data, fake, monkeypatch)


def git_call(root):
    return ("git", "-C", str(root.resolve()), "ls-files", "--error-unmatch", "server.py")


def test_apply_backs_up_original_writes_receipt_and_restarts(root, monkeypatch, capsys):
    fake = mcp_host()
    remote("apply", root, fake, monkeypatch)
    out = capsys.readouterr().out
    assert out == "MCP entry point apply verified; verify authenticated MCP access separately\n"
    assert (root / "server.py").read_bytes() == DESIRED.encode()
    backup = root / "server.py.before-terrainlink-ce"
    assert backup.read_bytes() == ORIGINAL
    assert backup.stat().st_mode & 0o777 == 0o640
    receipt = json.loads((root / "terrainlink-ce-receipt.json").read_text())
    assert receipt == {"installed": sha(DESIRED.encode()), "original": sha(ORIGINAL)}
    assert fake.calls == [git_call(root), RESTART, INSPECT, ("sleep", 1), INSPECT]


def test_apply_is_idempotent(root, monkeypatch, capsys):
    (root / "server.py").write_text(DESIRED)
    fake = mcp_host()
    remote("apply", root, fake, monkeypatch)
    assert capsys.readouterr().out == "MCP compatibility entry point already applied\n"
    assert fake.calls == [git_call(root)]


def test_apply_reuses_backup_when_current_matches_receipt(root, monkeypatch):
    fake = mcp_host()
    remote("apply", root, fake, monkeypatch)
    (root / "server.py").write_text("print('older ce tools')\n")
    receipt = root / "terrainlink-ce-receipt.json"
    receipt.write_text(json.dumps({"installed": sha(b"print('older ce tools')\n")}))
    remote("apply", root, fake, monkeypatch)
    assert (root / "server.py").read_bytes() == DESIRED.encode()
    assert (root / "server.py.before-terrainlink-ce").read_bytes() == ORIGINAL


def test_apply_refuses_changed_host_entry_point(root, monkeypatch):
    fake = mcp_host()
    remote("apply", root, fake, monkeypatch)
    (root / "server.py").write_text("print('host edit')\n")
    with pytest.raises(ValueError, match="preserve it and reconcile"):
        remote("apply", root, fake, monkeypatch, source="print('newer')\n")
    assert (root / "server.py").read_text() == "print('host edit')\n"


@pytest.mark.parametrize(
    "content",
    [b"print('x')\n", b"get_header_mcp\ndef run(): pass\n", b"get_header_mcp(\n", b""],
)
def test_apply_refuses_unexpected_original(root, monkeypatch, content):
    (root / "server.py").write_bytes(content)
    with pytest.raises(ValueError, match="Unexpected original entry point"):
        remote("apply", root, mcp_host(), monkeypatch)
    assert not (root / "server.py.before-terrainlink-ce").exists()


def test_apply_restart_failure_restores_previous_bytes(root, monkeypatch):
    fake = mcp_host()
    fake.fail.append("restart")
    with pytest.raises(subprocess.CalledProcessError):
        remote("apply", root, fake, monkeypatch)
    assert (root / "server.py").read_bytes() == ORIGINAL
    assert fake.calls == [git_call(root), RESTART, RESTART]
    # Current behaviour: backup and receipt remain after a failed apply.
    assert (root / "server.py.before-terrainlink-ce").exists()
    assert (root / "terrainlink-ce-receipt.json").exists()


def test_revert_restores_original(root, monkeypatch, capsys):
    fake = mcp_host()
    remote("apply", root, fake, monkeypatch)
    capsys.readouterr()
    remote("revert", root, fake, monkeypatch)
    out = capsys.readouterr().out
    assert out == "MCP entry point revert verified; verify authenticated MCP access separately\n"
    assert (root / "server.py").read_bytes() == ORIGINAL
    assert RESTART in fake.calls[-5:]
    remote("revert", root, fake, monkeypatch)
    assert capsys.readouterr().out == "Original MCP entry point already restored\n"


def test_revert_refuses_without_backup_or_after_host_change(root, monkeypatch):
    fake = mcp_host()
    with pytest.raises(ValueError, match="No compatibility backup available"):
        remote("revert", root, fake, monkeypatch)
    remote("apply", root, fake, monkeypatch)
    (root / "server.py").write_text("print('host edit')\n")
    with pytest.raises(ValueError, match="refusing overwrite"):
        remote("revert", root, fake, monkeypatch)
    assert (root / "server.py").read_text() == "print('host edit')\n"


def test_check_compares_entry_point(root, monkeypatch, capsys):
    fake = mcp_host()
    with pytest.raises(ValueError, match="MCP compatibility entry point differs"):
        remote("check", root, fake, monkeypatch)
    (root / "server.py").write_text(DESIRED)
    remote("check", root, fake, monkeypatch)
    assert capsys.readouterr().out == "MCP entry point verified\n"
    assert RESTART not in fake.calls


def test_refuses_tracked_symlinked_or_missing_entry_point(root, tmp_path, monkeypatch):
    fake = mcp_host()
    fake.tracked = True
    with pytest.raises(ValueError, match="Refusing to edit tracked host file"):
        remote("apply", root, fake, monkeypatch)
    link = tmp_path / "linked"
    link.mkdir()
    (link / "server.py").symlink_to(root / "server.py")
    with pytest.raises(ValueError, match="Expected regular MCP entry point"):
        remote("apply", link, mcp_host(), monkeypatch)
    (root / "server.py").unlink()
    with pytest.raises(ValueError, match="Expected regular MCP entry point"):
        remote("check", root, mcp_host(), monkeypatch)


def test_rejects_uncompilable_source_and_unknown_operation(root, monkeypatch):
    with pytest.raises(SyntaxError):
        remote("apply", root, mcp_host(), monkeypatch, source="def (:\n")
    with pytest.raises(ValueError, match="Expected apply, revert or check"):
        remote("rebuild", root, mcp_host(), monkeypatch)
    assert (root / "server.py").read_bytes() == ORIGINAL


def test_main_ships_fixed_program_and_entry_point_over_ssh(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "argv", ["mcp_deploy", "apply"])
    monkeypatch.setenv("PLANE_MCP_SSH", "owner@host")
    monkeypatch.setenv("PLANE_MCP_DIRECTORY", "/srv/mcp")
    monkeypatch.delenv("PLANE_MCP_CONTAINER", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kw: calls.append((args, kw)))
    mcp_deploy.main()
    [((argv,), kw)] = calls
    assert argv == ["ssh", "owner@host", "python3 -c " + shlex.quote(mcp_deploy.REMOTE)]
    assert kw["text"] and kw["check"]
    source = mcp_deploy.Path(mcp_deploy.__file__).with_name("ce_tools.py").read_text()
    assert json.loads(kw["input"]) == dict(
        operation="apply", directory="/srv/mcp", container="plane-mcp", source=source
    )


@pytest.mark.parametrize(
    ("host", "directory", "container"),
    [("-oProxy", "/srv", "c"), ("h", "srv", "c"), ("h", "/srv", "-c")],
)
def test_main_rejects_unsafe_destination(monkeypatch, host, directory, container):
    monkeypatch.setattr(sys, "argv", ["mcp_deploy", "check"])
    monkeypatch.setenv("PLANE_MCP_SSH", host)
    monkeypatch.setenv("PLANE_MCP_DIRECTORY", directory)
    monkeypatch.setenv("PLANE_MCP_CONTAINER", container)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kw: pytest.fail("Must not connect"))
    with pytest.raises(ValueError, match="Invalid MCP deployment destination"):
        mcp_deploy.main()


def test_remote_program_is_stdlib_only():
    tree = ast.parse(mcp_deploy.REMOTE)
    modules = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert modules and modules <= set(sys.stdlib_module_names)
    assert not any(isinstance(node, ast.ImportFrom) for node in ast.walk(tree))


# Injected seams: deploy() with an in-memory Docker host, no process patching.


def deploy(operation, root, fake, source=DESIRED):
    data = dict(operation=operation, directory=str(root), container="plane-mcp", source=source)
    return mcp_deploy.deploy(
        data, fake.docker, lambda path: fake.tracked, sleep=lambda seconds: fake.calls.append("nap")
    )


def test_seam_apply_revert_and_restart_rollback(root):
    fake = mcp_host()
    assert deploy("apply", root, fake).startswith("MCP entry point apply verified")
    assert deploy("check", root, fake) == "MCP entry point verified"
    fake.fail.append("restart")
    with pytest.raises(subprocess.CalledProcessError):
        deploy("revert", root, fake)
    assert (root / "server.py").read_text() == DESIRED
    assert fake.calls[-2:] == [RESTART, RESTART]
    assert deploy("revert", root, fake).startswith("MCP entry point revert verified")
    assert (root / "server.py").read_bytes() == ORIGINAL


# Health wait, rollback and digest-based detection.


def test_apply_timeout_restores_backup_bytes_and_inode(root, monkeypatch):
    inode = (root / "server.py").stat().st_ino
    fake = mcp_host()
    fake.containers["plane-mcp"]["health"] = "starting"
    with pytest.raises(RuntimeError, match="did not become healthy") as error:
        deploy("apply", root, fake)
    assert "OPAQUE_SECRET" not in str(error.value)
    assert (root / "server.py").read_bytes() == ORIGINAL
    assert (root / "server.py").stat().st_ino == inode
    assert fake.calls.count(INSPECT) == 45
    assert fake.calls.count("nap") >= 44
    assert fake.calls[-1] == RESTART and fake.calls.count(RESTART) == 2
    # Legacy receipt names survive a rolled-back apply.
    assert (root / "server.py.before-terrainlink-ce").read_bytes() == ORIGINAL
    assert (root / "terrainlink-ce-receipt.json").exists()


def test_apply_crash_loop_rolls_back(root):
    fake = mcp_host()
    fake.containers["plane-mcp"]["restart_counts"] = [0, 1, 2, 3]
    with pytest.raises(RuntimeError, match="did not become healthy"):
        deploy("apply", root, fake)
    assert (root / "server.py").read_bytes() == ORIGINAL
    assert fake.calls.count(INSPECT) < 45
    assert fake.calls[-1] == RESTART


def test_restarting_container_is_not_healthy(root):
    fake = mcp_host()
    fake.containers["plane-mcp"]["restarting"] = True
    with pytest.raises(RuntimeError, match="did not become healthy"):
        deploy("apply", root, fake)
    assert (root / "server.py").read_bytes() == ORIGINAL


def test_stopped_container_is_not_healthy(root):
    fake = mcp_host()
    original_execute = fake.execute

    def execute(*args, input=None):
        code, out = original_execute(*args, input=input)
        if args[0] == "restart":
            fake.containers["plane-mcp"]["running"] = False
        return code, out

    fake.execute = execute
    with pytest.raises(RuntimeError, match="did not become healthy"):
        deploy("apply", root, fake)
    assert (root / "server.py").read_bytes() == ORIGINAL


def test_healthy_after_starting_succeeds(root):
    fake = mcp_host()
    fake.containers["plane-mcp"]["health"] = "healthy"
    assert deploy("apply", root, fake).startswith("MCP entry point apply verified")
    assert (root / "server.py").read_text() == DESIRED


def test_revert_timeout_restores_installed_bytes(root):
    fake = mcp_host()
    deploy("apply", root, fake)
    fake.containers["plane-mcp"]["health"] = "unhealthy"
    with pytest.raises(RuntimeError, match="did not become healthy"):
        deploy("revert", root, fake)
    assert (root / "server.py").read_text() == DESIRED


def test_write_failure_restores_previous_bytes(root, monkeypatch):
    real_fsync = mcp_deploy.os.fsync
    calls = []

    def fsync(fd):
        calls.append(fd)
        if len(calls) == 1:
            raise OSError("disk")
        real_fsync(fd)

    monkeypatch.setattr(mcp_deploy.os, "fsync", fsync)
    fake = mcp_host()
    with pytest.raises(OSError, match="disk"):
        deploy("apply", root, fake)
    assert (root / "server.py").read_bytes() == ORIGINAL
    assert RESTART not in fake.calls


def test_write_flushes_and_fsyncs_in_place(root, monkeypatch):
    synced = []
    real_fsync = mcp_deploy.os.fsync
    monkeypatch.setattr(mcp_deploy.os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd)))
    inode = (root / "server.py").stat().st_ino
    deploy("apply", root, mcp_host())
    assert synced
    assert (root / "server.py").stat().st_ino == inode


def test_apply_uses_receipt_original_digest_not_substring(root):
    fake = mcp_host()
    deploy("apply", root, fake)
    (root / "server.py.before-terrainlink-ce").unlink()
    # Receipt records the original digest: a different file is refused even if it looks plain.
    (root / "server.py").write_bytes(b"get_header_mcp\n# edited\n")
    with pytest.raises(ValueError, match="Unexpected original"):
        deploy("apply", root, fake)


def test_apply_without_backup_accepts_recorded_original_digest(root):
    fake = mcp_host()
    deploy("apply", root, fake)
    (root / "server.py.before-terrainlink-ce").unlink()
    (root / "server.py").write_bytes(ORIGINAL)
    assert deploy("apply", root, fake).startswith("MCP entry point apply verified")
    assert (root / "server.py.before-terrainlink-ce").read_bytes() == ORIGINAL


def test_apply_refuses_backup_that_differs_from_recorded_original(root):
    fake = mcp_host()
    deploy("apply", root, fake)
    (root / "server.py.before-terrainlink-ce").write_bytes(b"tampered\n")
    with pytest.raises(ValueError, match="Backup does not match"):
        deploy("apply", root, fake, source="print('newer')\n")
