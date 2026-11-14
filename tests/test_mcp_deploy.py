"""MCP entry-point remote program: characterised end to end against a fake host."""

import ast
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fake_docker import FakeDocker, run_program
from plane_toolchain import mcp_deploy, remote as remote_module
from plane_toolchain.legacy import MCP_BACKUP_NAME, MCP_RECEIPT_NAME

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
    backup = root / "server.py.before-plane-toolchain-ce"
    assert backup.read_bytes() == ORIGINAL
    assert backup.stat().st_mode & 0o777 == 0o640
    receipt = json.loads((root / "plane-toolchain-ce-receipt.json").read_text())
    assert receipt == {"installed": sha(DESIRED.encode()), "original": sha(ORIGINAL)}
    expected = [git_call(root), RESTART, INSPECT]
    for _ in range(5):
        expected += [("sleep", 1), INSPECT]
    assert fake.calls == expected


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
    receipt = root / "plane-toolchain-ce-receipt.json"
    receipt.write_text(json.dumps({"installed": sha(b"print('older ce tools')\n")}))
    remote("apply", root, fake, monkeypatch)
    assert (root / "server.py").read_bytes() == DESIRED.encode()
    assert (root / "server.py.before-plane-toolchain-ce").read_bytes() == ORIGINAL


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
    assert not (root / "server.py.before-plane-toolchain-ce").exists()


def test_apply_restart_failure_restores_previous_bytes(root, monkeypatch):
    fake = mcp_host()
    fake.fail.append("restart")
    with pytest.raises(subprocess.CalledProcessError):
        remote("apply", root, fake, monkeypatch)
    assert (root / "server.py").read_bytes() == ORIGINAL
    assert fake.calls[:3] == [git_call(root), RESTART, RESTART]
    # The backup remains after a failed first apply; no receipt was written.
    assert (root / "server.py.before-plane-toolchain-ce").exists()
    assert not (root / "plane-toolchain-ce-receipt.json").exists()


def test_revert_restores_original(root, monkeypatch, capsys):
    fake = mcp_host()
    remote("apply", root, fake, monkeypatch)
    capsys.readouterr()
    remote("revert", root, fake, monkeypatch)
    out = capsys.readouterr().out
    assert out == "MCP entry point revert verified; verify authenticated MCP access separately\n"
    assert (root / "server.py").read_bytes() == ORIGINAL
    assert RESTART in fake.calls
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
    assert argv == [
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
        "python3 -c " + shlex.quote(mcp_deploy.REMOTE),
    ]
    assert kw["text"] and kw["check"]
    source = mcp_deploy.Path(mcp_deploy.__file__).with_name("ce_tools.py").read_text()
    assert json.loads(kw["input"]) == dict(
        operation="apply", directory="/srv/mcp", container="plane-mcp", source=source
    )


@pytest.mark.parametrize("missing", ["PLANE_MCP_SSH", "PLANE_MCP_DIRECTORY"])
def test_main_names_missing_environment_variable_only(monkeypatch, capsys, missing):
    monkeypatch.setattr(sys, "argv", ["mcp_deploy", "check"])
    monkeypatch.setenv("PLANE_MCP_SSH", "secret-owner@secret-host")
    monkeypatch.setenv("PLANE_MCP_DIRECTORY", "/srv/secret")
    monkeypatch.delenv(missing)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kw: pytest.fail("Must not connect"))
    with pytest.raises(SystemExit) as caught:
        mcp_deploy.main()
    assert caught.value.code == "Missing environment variable: " + missing
    assert "secret" not in capsys.readouterr().err


def test_remote_ignores_hup_and_pipe_before_any_docker_call(root, monkeypatch, capsys):
    import signal

    fake = mcp_host()
    monkeypatch.setattr(signal, "signal", lambda number, handler: fake.calls.append(number))
    remote("apply", root, fake, monkeypatch)
    assert fake.calls[:4] == [signal.SIGHUP, signal.SIGPIPE, signal.SIGTERM, git_call(root)]


def test_closed_stderr_does_not_mask_interrupt(root, monkeypatch):
    class Closed:
        def write(self, text):
            raise BrokenPipeError

        def flush(self):
            raise BrokenPipeError

    fake = mcp_host()
    fake.containers["plane-mcp"]["health"] = "starting"
    naps = []

    def sleep(seconds):
        naps.append(seconds)
        if len(naps) == 1:
            raise KeyboardInterrupt

    monkeypatch.setattr(sys, "stderr", Closed())
    data = dict(operation="apply", directory=str(root), container="plane-mcp", source=DESIRED)
    with pytest.raises(KeyboardInterrupt):
        mcp_deploy.deploy(data, fake.docker, lambda path: False, sleep=sleep)
    assert (root / "server.py").read_bytes() == ORIGINAL


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
    assert fake.calls.count(RESTART) >= 2
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
    assert fake.calls.count(INSPECT) == 90  # 45 before and 45 after the rollback restart
    assert fake.calls.count(RESTART) == 2
    assert "still unhealthy" in str(error.value)
    # A rolled-back first apply keeps the neutral backup and writes no receipt.
    assert (root / "server.py.before-plane-toolchain-ce").read_bytes() == ORIGINAL
    assert not (root / "plane-toolchain-ce-receipt.json").exists()


def test_apply_crash_loop_rolls_back(root):
    inode = (root / "server.py").stat().st_ino
    fake = mcp_host()
    fake.containers["plane-mcp"]["restart_counts"] = [0, 1, 2, 3]
    with pytest.raises(RuntimeError, match="did not become healthy"):
        deploy("apply", root, fake)
    assert (root / "server.py").read_bytes() == ORIGINAL
    assert (root / "server.py").stat().st_ino == inode
    assert fake.calls.count(INSPECT) < 90
    assert fake.calls.count(RESTART) == 2
    assert not (root / "plane-toolchain-ce-receipt.json").exists()


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
    (root / "server.py.before-plane-toolchain-ce").unlink()
    # Receipt records the original digest: a different file is refused even if it looks plain.
    (root / "server.py").write_bytes(b"get_header_mcp\n# edited\n")
    with pytest.raises(ValueError, match="Unexpected original"):
        deploy("apply", root, fake)


def test_apply_without_backup_accepts_recorded_original_digest(root):
    fake = mcp_host()
    deploy("apply", root, fake)
    (root / "server.py.before-plane-toolchain-ce").unlink()
    (root / "server.py").write_bytes(ORIGINAL)
    assert deploy("apply", root, fake).startswith("MCP entry point apply verified")
    assert (root / "server.py.before-plane-toolchain-ce").read_bytes() == ORIGINAL


def test_apply_refuses_backup_that_differs_from_recorded_original(root):
    fake = mcp_host()
    deploy("apply", root, fake)
    (root / "server.py.before-plane-toolchain-ce").write_bytes(b"tampered\n")
    with pytest.raises(ValueError, match="Backup does not match"):
        deploy("apply", root, fake, source="print('newer')\n")


V1 = "print('v1')\n"
V2 = "print('v2')\n"


def upgraded_host(root):
    fake = mcp_host()
    deploy("apply", root, fake, source=V1)
    return fake, (root / "plane-toolchain-ce-receipt.json").read_bytes()


def test_failed_upgrade_keeps_receipt_so_revert_and_reapply_work(root):
    fake, receipt = upgraded_host(root)
    fake.containers["plane-mcp"]["health"] = "starting"
    with pytest.raises(RuntimeError, match="did not become healthy"):
        deploy("apply", root, fake, source=V2)
    assert (root / "server.py").read_text() == V1
    assert (root / "plane-toolchain-ce-receipt.json").read_bytes() == receipt
    fake.containers["plane-mcp"]["health"] = "healthy"
    # Revert works from the rolled-back state...
    assert deploy("revert", root, fake).startswith("MCP entry point revert verified")
    assert (root / "server.py").read_bytes() == ORIGINAL
    # ...and re-applying v2 succeeds, then records the new receipt.
    assert deploy("apply", root, fake, source=V2).startswith("MCP entry point apply verified")
    new = json.loads((root / "plane-toolchain-ce-receipt.json").read_text())
    assert new["installed"] == sha(V2.encode())


def test_failed_upgrade_then_reapply_without_revert(root):
    fake, receipt = upgraded_host(root)
    fake.containers["plane-mcp"]["health"] = "starting"
    with pytest.raises(RuntimeError):
        deploy("apply", root, fake, source=V2)
    fake.containers["plane-mcp"]["health"] = "healthy"
    assert deploy("apply", root, fake, source=V2).startswith("MCP entry point apply verified")


def test_failed_upgrade_write_failure_keeps_receipt(root, monkeypatch):
    fake, receipt = upgraded_host(root)
    monkeypatch.setattr(mcp_deploy.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        deploy("apply", root, fake, source=V2)
    assert (root / "plane-toolchain-ce-receipt.json").read_bytes() == receipt


def test_check_ignores_corrupt_receipt(root):
    (root / "server.py").write_text(DESIRED)
    (root / "plane-toolchain-ce-receipt.json").write_text("not json")
    assert deploy("check", root, mcp_host()) == "MCP entry point verified"


def test_old_format_receipt_without_original_still_works(root):
    fake, _ = upgraded_host(root)
    (root / "plane-toolchain-ce-receipt.json").write_text(
        json.dumps({"installed": sha(V1.encode())})
    )
    assert deploy("apply", root, fake, source=V2).startswith("MCP entry point apply verified")


def test_rollback_that_recovers_reports_restored_only(root):
    fake, _ = upgraded_host(root)
    fake.containers["plane-mcp"]["restart_counts"] = [0, 1, 1]
    with pytest.raises(RuntimeError) as error:
        deploy("apply", root, fake, source=V2)
    assert str(error.value) == "MCP did not become healthy; previous entry point restored"


# Interrupted deploys (SIGKILL, OOM, reboot) leave a pending receipt; recovery must still work.

V3 = "print('v3')\n"


def pending(installed, previous, original=ORIGINAL):
    return json.dumps(
        {
            "installed": sha(installed),
            "previous": previous,
            "original": sha(original),
            "pending": True,
        }
    )


def watch_receipt(root, fake, source, seen):
    receipt = root / "plane-toolchain-ce-receipt.json"
    data = dict(operation="apply", directory=str(root), container="plane-mcp", source=source)
    return mcp_deploy.deploy(
        data,
        fake.docker,
        lambda path: False,
        sleep=lambda seconds: seen.append(json.loads(receipt.read_text())),
    )


def test_pending_receipt_is_written_before_restart_and_final_after(root):
    fake, _ = upgraded_host(root)
    seen = []
    watch_receipt(root, fake, V2, seen)
    assert seen[0] == {
        "installed": sha(V2.encode()),
        "previous": sha(V1.encode()),
        "original": sha(ORIGINAL),
        "pending": True,
    }
    receipt = root / "plane-toolchain-ce-receipt.json"
    assert json.loads(receipt.read_text()) == {
        "installed": sha(V2.encode()),
        "original": sha(ORIGINAL),
    }
    assert not (root / "plane-toolchain-ce-receipt.json.tmp").exists()


def test_first_apply_pending_receipt_has_null_previous(root):
    seen = []
    watch_receipt(root, mcp_host(), DESIRED, seen)
    assert seen[0]["previous"] is None and seen[0]["pending"] is True


def stop_first_apply(root):
    (root / "server.py.before-plane-toolchain-ce").write_bytes(ORIGINAL)
    (root / "server.py").write_text(V1)
    (root / "plane-toolchain-ce-receipt.json").write_text(pending(V1.encode(), None))


def stop_upgrade(root, fake):
    deploy("apply", root, fake, source=V1)
    (root / "server.py").write_text(V2)
    (root / "plane-toolchain-ce-receipt.json").write_text(pending(V2.encode(), sha(V1.encode())))


def test_stopped_first_apply_can_revert(root):
    stop_first_apply(root)
    assert deploy("revert", root, mcp_host()).startswith("MCP entry point revert verified")
    assert (root / "server.py").read_bytes() == ORIGINAL


def test_stopped_first_apply_can_apply_again_and_repairs_receipt(root):
    stop_first_apply(root)
    fake = mcp_host()
    message = deploy("apply", root, fake, source=V1)
    assert message == "MCP compatibility entry point already applied"
    assert json.loads((root / "plane-toolchain-ce-receipt.json").read_text()) == {
        "installed": sha(V1.encode()),
        "original": sha(ORIGINAL),
    }
    assert deploy("apply", root, fake, source=V2).startswith("MCP entry point apply verified")


def test_stopped_first_apply_missing_receipt_is_repaired(root):
    stop_first_apply(root)
    (root / "plane-toolchain-ce-receipt.json").unlink()
    deploy("apply", root, mcp_host(), source=V1)
    assert (root / "plane-toolchain-ce-receipt.json").exists()


def test_stopped_upgrade_can_revert(root):
    fake = mcp_host()
    stop_upgrade(root, fake)
    assert deploy("revert", root, fake).startswith("MCP entry point revert verified")
    assert (root / "server.py").read_bytes() == ORIGINAL


def test_stopped_upgrade_can_apply_next_version(root):
    fake = mcp_host()
    stop_upgrade(root, fake)
    assert deploy("apply", root, fake, source=V3).startswith("MCP entry point apply verified")
    assert (root / "server.py").read_text() == V3


def test_stopped_upgrade_before_rewrite_still_recovers(root):
    # File still v1 while the pending receipt names v2: previous digest is accepted.
    fake = mcp_host()
    stop_upgrade(root, fake)
    (root / "server.py").write_text(V1)
    assert deploy("apply", root, fake, source=V3).startswith("MCP entry point apply verified")


def test_pending_receipt_does_not_accept_foreign_file(root):
    fake = mcp_host()
    stop_upgrade(root, fake)
    (root / "server.py").write_text("print('host edit')\n")
    with pytest.raises(ValueError, match="refusing overwrite"):
        deploy("revert", root, fake)
    with pytest.raises(ValueError, match="preserve it and reconcile"):
        deploy("apply", root, fake, source=V3)


def interrupt(seconds):
    raise KeyboardInterrupt


def interrupted(root, fake, source):
    data = dict(operation="apply", directory=str(root), container="plane-mcp", source=source)
    with pytest.raises(KeyboardInterrupt):
        mcp_deploy.deploy(data, fake.docker, lambda path: False, sleep=interrupt)


def test_interrupt_during_health_wait_rolls_back_receipt_and_bytes(root):
    fake, receipt = upgraded_host(root)
    interrupted(root, fake, V2)
    assert (root / "server.py").read_text() == V1
    assert (root / "plane-toolchain-ce-receipt.json").read_bytes() == receipt


def test_interrupt_on_first_apply_removes_pending_receipt(root):
    interrupted(root, mcp_host(), V1)
    assert (root / "server.py").read_bytes() == ORIGINAL
    assert not (root / "plane-toolchain-ce-receipt.json").exists()


def test_interrupt_with_unhealthy_rollback_notes_on_stderr(root, capsys):
    fake = mcp_host()
    fake.containers["plane-mcp"]["health"] = "starting"
    calls = []

    def sleep(seconds):
        calls.append(seconds)
        if len(calls) == 1:
            raise KeyboardInterrupt

    data = dict(operation="apply", directory=str(root), container="plane-mcp", source=V1)
    with pytest.raises(KeyboardInterrupt):
        mcp_deploy.deploy(data, fake.docker, lambda path: False, sleep=sleep)
    assert "MCP still unhealthy after rollback" in capsys.readouterr().err


def test_already_applied_does_not_repair_receipt_over_mismatched_backup(root):
    fake, receipt = upgraded_host(root)
    (root / "server.py.before-plane-toolchain-ce").write_bytes(b"tampered\n")
    with pytest.raises(ValueError, match="Backup does not match"):
        deploy("apply", root, fake, source=V1)
    assert (root / "plane-toolchain-ce-receipt.json").read_bytes() == receipt


def test_receipt_write_fsyncs_directory(root, monkeypatch):
    kinds = []
    real = mcp_deploy.os.fsync
    monkeypatch.setattr(mcp_deploy.os, "fsync", lambda fd: (kinds.append(fd), real(fd)))
    opened = []
    real_open = mcp_deploy.os.open
    monkeypatch.setattr(mcp_deploy.os, "open", lambda *a: opened.append(a) or real_open(*a))
    deploy("apply", root, mcp_host())
    assert any(call[0] == root.resolve() or call[0] == root for call in opened)


# PTC-30: legacy-named host state is refused, never read.

REFUSAL = (
    "Legacy-named state found: {}. This release no longer reads it. "
    "Migrate first with plane-toolchain at commit 1cb6153: "
    "Pages revert (apply only if check passes there), MCP apply. Or reconcile manually."
)
NEUTRAL_BACKUP = "server.py.before-plane-toolchain-ce"
NEUTRAL_RECEIPT = "plane-toolchain-ce-receipt.json"


def snapshot(root):
    return {p.name: p.read_bytes() for p in sorted(root.iterdir())}


def legacy_receipt(installed=V1):
    return json.dumps({"installed": sha(installed.encode()), "original": sha(ORIGINAL)})


def assert_refused(root, operation, names, monkeypatch=None, shipped=False):
    """The operation raises the exact refusal; no file changes and no container call happens."""
    fake = mcp_host()
    before = snapshot(root)
    with pytest.raises(ValueError) as error:
        if shipped:
            remote(operation, root, fake, monkeypatch)
        else:
            deploy(operation, root, fake)
    assert str(error.value) == REFUSAL.format(", ".join(names))
    assert snapshot(root) == before
    assert fake.calls == []


@pytest.mark.parametrize("operation", ["check", "apply", "revert"])
@pytest.mark.parametrize("shipped", [False, True])
def test_legacy_backup_refused_for_every_operation(root, operation, shipped, monkeypatch):
    (root / MCP_BACKUP_NAME).write_bytes(ORIGINAL)
    assert_refused(root, operation, [MCP_BACKUP_NAME], monkeypatch, shipped)


@pytest.mark.parametrize("operation", ["check", "apply", "revert"])
def test_legacy_receipt_refused(root, operation):
    (root / MCP_BACKUP_NAME).write_bytes(ORIGINAL)
    (root / "server.py").write_text(V1)
    (root / MCP_RECEIPT_NAME).write_text(legacy_receipt())
    assert_refused(root, operation, [MCP_BACKUP_NAME, MCP_RECEIPT_NAME])


@pytest.mark.parametrize("operation", ["check", "apply", "revert"])
def test_legacy_receipt_tmp_alone_refused(root, operation):
    (root / (MCP_RECEIPT_NAME + ".tmp")).write_text("{")
    assert_refused(root, operation, [MCP_RECEIPT_NAME + ".tmp"])


@pytest.mark.parametrize("operation", ["check", "apply", "revert"])
def test_mixed_legacy_and_neutral_refused(root, operation):
    (root / NEUTRAL_BACKUP).write_bytes(ORIGINAL)
    (root / NEUTRAL_RECEIPT).write_text(legacy_receipt(DESIRED))
    (root / "server.py").write_text(DESIRED)
    (root / MCP_RECEIPT_NAME).write_text(legacy_receipt())
    assert_refused(root, operation, [MCP_RECEIPT_NAME])


def test_neutral_host_unaffected(root):
    fake = mcp_host()
    assert deploy("apply", root, fake).startswith("MCP entry point apply verified")
    assert deploy("check", root, fake) == "MCP entry point verified"
    assert deploy("revert", root, fake).startswith("MCP entry point revert verified")
    assert (root / NEUTRAL_BACKUP).exists() and not (root / MCP_BACKUP_NAME).exists()


def test_remote_ships_tripwire_and_no_legacy_reads():
    compile(mcp_deploy.REMOTE, "<remote>", "exec")
    assert "def legacy_state" in mcp_deploy.REMOTE and "def refusal" in mcp_deploy.REMOTE
    assert "def migrate" not in mcp_deploy.REMOTE and "def locate" not in mcp_deploy.REMOTE


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


def mcp_env(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["mcp_deploy", "check"])
    monkeypatch.setenv("PLANE_MCP_SSH", FAKE_HOST)
    monkeypatch.setenv("PLANE_MCP_DIRECTORY", "/srv/mcp")


def test_main_remote_refusal_prints_only_the_refusal_line(monkeypatch, capsys):
    mcp_env(monkeypatch)
    failing_ssh(monkeypatch)
    with pytest.raises(SystemExit) as stop:
        mcp_deploy.main()
    assert stop.value.code == 1
    assert REFUSAL_LINE in assert_no_leaks(capsys).err


def test_main_ssh_failure_and_interrupt_are_quiet(monkeypatch, capsys):
    mcp_env(monkeypatch)
    failing_ssh(monkeypatch, code=255)
    with pytest.raises(SystemExit) as stop:
        mcp_deploy.main()
    assert stop.value.code == 255
    assert remote_module.SSH_FAILED in assert_no_leaks(capsys).err
    failing_ssh(monkeypatch, error=KeyboardInterrupt())
    with pytest.raises(SystemExit) as stop:
        mcp_deploy.main()
    assert stop.value.code == 130
    assert_no_leaks(capsys)


def test_remote_entry_reports_unexpected_failure_without_traceback():
    assert "sys.excepthook = quiet_failure" in mcp_deploy.REMOTE


def test_module_entry_prints_one_line_for_invalid_destination(monkeypatch, capsys):
    import runpy

    monkeypatch.delitem(sys.modules, "plane_toolchain.mcp_deploy")
    monkeypatch.setattr(sys, "argv", ["mcp_deploy", "check"])
    monkeypatch.setenv("PLANE_MCP_SSH", "-oProxyCommand=x")
    monkeypatch.setenv("PLANE_MCP_DIRECTORY", "/srv/mcp")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("Must not connect"))
    with pytest.raises(SystemExit) as stop:
        runpy.run_module("plane_toolchain.mcp_deploy", run_name="__main__")
    assert stop.value.code == 1
    seen = capsys.readouterr()
    assert seen.err.count("\n") == 1 and "Traceback" not in seen.err
    assert "ProxyCommand" not in seen.err


# PTC-65: kill injection. A SIGKILL leaves the directory exactly as it was at that instant, so
# each test snapshots the directory at the kill point, raises, then puts the snapshot back.


class Killed(BaseException):
    """Stands in for SIGKILL: rollbacks that catch BaseException must not be trusted."""


def freeze(root):
    return {p.name: (p.read_bytes(), p.stat().st_mode & 0o777) for p in root.iterdir()}


def thaw(root, state):
    for path in root.iterdir():
        path.unlink()
    for name, (content, mode) in state.items():
        (root / name).write_bytes(content)
        (root / name).chmod(mode)


def kill_at_call(root, monkeypatch, kind, nth, operation="apply", source=DESIRED):
    """Run the operation until the nth os.`kind` call, restore the frozen directory; False if none."""
    fake = mcp_host()
    seen = {"n": 0}
    real = getattr(mcp_deploy.os, kind)

    def hook(*args, **kw):
        seen["n"] += 1
        if seen["n"] == nth:
            seen["state"] = freeze(root)
            raise Killed
        return real(*args, **kw)

    monkeypatch.setattr(mcp_deploy.os, kind, hook)
    try:
        deploy(operation, root, fake, source=source)
    except Killed:
        pass
    finally:
        monkeypatch.setattr(mcp_deploy.os, kind, real)
    if "state" not in seen:
        return False
    thaw(root, seen["state"])
    return True


def kill_rewrite(root, monkeypatch, cut, operation="apply", source=DESIRED):
    """Kill in the middle of the in-place write of server.py after `cut` bytes."""
    real = mcp_deploy.rewrite
    state = {}

    def partial(target, content):
        if state:  # the process is dead: rollback handlers in this test must not write
            raise Killed
        with open(target, "r+b") as handle:
            handle.truncate(0)
            handle.write(content[:cut])
        state["state"] = freeze(root)
        raise Killed

    monkeypatch.setattr(mcp_deploy, "rewrite", partial)
    try:
        deploy(operation, root, mcp_host(), source=source)
    except Killed:
        pass
    finally:
        monkeypatch.setattr(mcp_deploy, "rewrite", real)
    thaw(root, state["state"])


def assert_recovers(root):
    """From a killed state: apply completes, then revert restores the original."""
    fake = mcp_host()
    assert deploy("apply", root, fake).startswith(("MCP entry point apply", "MCP compat"))
    assert (root / "server.py").read_text() == DESIRED
    receipt = json.loads((root / "plane-toolchain-ce-receipt.json").read_text())
    assert receipt == {"installed": sha(DESIRED.encode()), "original": sha(ORIGINAL)}
    assert deploy("revert", root, fake).startswith("MCP entry point revert verified")
    assert (root / "server.py").read_bytes() == ORIGINAL


@pytest.mark.parametrize("kind", ["fsync", "replace"])
@pytest.mark.parametrize("nth", range(1, 9))
def test_kill_at_each_apply_write_step_recovers(root, monkeypatch, kind, nth):
    if not kill_at_call(root, monkeypatch, kind, nth):
        pytest.skip("fewer steps than this index")
    assert (root / "server.py").read_bytes() in (ORIGINAL, DESIRED.encode())
    assert_recovers(root)


@pytest.mark.parametrize("cut", [0, 5, len(DESIRED) - 1])
def test_kill_mid_server_write_leaves_empty_or_prefix_and_apply_recovers(root, monkeypatch, cut):
    kill_rewrite(root, monkeypatch, cut)
    assert (root / "server.py").read_bytes() == DESIRED.encode()[:cut]
    assert_recovers(root)


@pytest.mark.parametrize("cut", [0, 5, len(DESIRED) - 1])
def test_kill_mid_server_write_then_revert_recovers(root, monkeypatch, cut):
    kill_rewrite(root, monkeypatch, cut)
    assert deploy("revert", root, mcp_host()).startswith("MCP entry point revert verified")
    assert (root / "server.py").read_bytes() == ORIGINAL


@pytest.mark.parametrize("cut", [0, 7, len(ORIGINAL) - 1])
def test_kill_mid_revert_write_leaves_prefix_of_backup_and_both_recover(root, monkeypatch, cut):
    deploy("apply", root, mcp_host())
    kill_rewrite(root, monkeypatch, cut, operation="revert")
    assert (root / "server.py").read_bytes() == ORIGINAL[:cut]
    assert deploy("revert", root, mcp_host()).startswith("MCP entry point revert verified")
    assert (root / "server.py").read_bytes() == ORIGINAL


@pytest.mark.parametrize("cut", [0, 7, len(ORIGINAL) - 1])
def test_kill_mid_revert_write_then_apply_recovers(root, monkeypatch, cut):
    deploy("apply", root, mcp_host())
    kill_rewrite(root, monkeypatch, cut, operation="revert")
    deploy("apply", root, mcp_host())
    assert (root / "server.py").read_text() == DESIRED


def test_recovery_still_refuses_a_foreign_file_after_a_partial_write(root, monkeypatch):
    kill_rewrite(root, monkeypatch, 5)
    (root / "server.py").write_text("print('host edit')\n")
    with pytest.raises(ValueError, match="reconcile"):
        deploy("apply", root, mcp_host())
    with pytest.raises(ValueError, match="refusing overwrite"):
        deploy("revert", root, mcp_host())


def test_failed_health_after_recovery_does_not_write_the_partial_bytes_back(root, monkeypatch):
    kill_rewrite(root, monkeypatch, 5)
    fake = mcp_host()
    fake.containers["plane-mcp"]["health"] = "starting"
    with pytest.raises(RuntimeError, match="did not become healthy"):
        deploy("apply", root, fake)
    assert (root / "server.py").read_bytes() == ORIGINAL
    assert_recovers(root)


def test_backup_is_written_atomically_with_the_entry_point_mode(root, monkeypatch):
    seen = []
    real = mcp_deploy.os.replace
    backup = root / "server.py.before-plane-toolchain-ce"

    def replace(source, destination):
        if str(destination) == str(backup):
            seen.append(
                (
                    str(source),
                    (root / "server.py").read_bytes(),
                    oct(os.stat(source).st_mode & 0o777),
                )
            )
        real(source, destination)

    monkeypatch.setattr(mcp_deploy.os, "replace", replace)
    deploy("apply", root, mcp_host())
    assert len(seen) == 1
    source, server_then, mode = seen[0]
    assert source.startswith(str(backup)) and source != str(backup)  # temp beside it
    assert server_then == ORIGINAL  # server.py untouched until the backup is whole
    assert mode == "0o640"
    assert not list(root.glob("*.tmp"))
    assert backup.read_bytes() == ORIGINAL and backup.stat().st_mode & 0o777 == 0o640


# Fix round 1: the backup-prefix rule needs a pending or reverting marker.


def test_a_shortened_server_py_with_a_final_receipt_is_refused_not_overwritten(root):
    deploy("apply", root, mcp_host())
    (root / "server.py").write_bytes(ORIGINAL[: len(ORIGINAL) // 2])
    with pytest.raises(ValueError, match="reconcile"):
        deploy("apply", root, mcp_host())
    with pytest.raises(ValueError, match="refusing overwrite"):
        deploy("revert", root, mcp_host())
    assert (root / "server.py").read_bytes() == ORIGINAL[: len(ORIGINAL) // 2]


def test_revert_marks_the_receipt_before_its_rewrite_and_clears_it_after(root):
    fake = mcp_host()
    deploy("apply", root, fake)
    final = (root / "plane-toolchain-ce-receipt.json").read_text()
    seen = []
    real = mcp_deploy.rewrite
    mcp_deploy.rewrite = lambda t, c: (
        seen.append(json.loads((root / "plane-toolchain-ce-receipt.json").read_text())),
        real(t, c),
    )
    try:
        deploy("revert", root, fake)
    finally:
        mcp_deploy.rewrite = real
    assert seen[0].get("reverting") is True and seen[0]["installed"] == sha(DESIRED.encode())
    assert (root / "plane-toolchain-ce-receipt.json").read_text() == final


def test_failed_revert_restores_the_unmarked_receipt(root):
    fake = mcp_host()
    deploy("apply", root, fake)
    final = (root / "plane-toolchain-ce-receipt.json").read_text()
    fake.containers["plane-mcp"]["health"] = "starting"
    with pytest.raises(RuntimeError, match="did not become healthy"):
        deploy("revert", root, fake)
    assert (root / "plane-toolchain-ce-receipt.json").read_text() == final
    assert (root / "server.py").read_text() == DESIRED


def test_revert_killed_mid_write_then_apply_and_revert_both_recover(root, monkeypatch):
    deploy("apply", root, mcp_host())
    kill_rewrite(root, monkeypatch, 9, operation="revert")
    receipt = json.loads((root / "plane-toolchain-ce-receipt.json").read_text())
    assert receipt["reverting"] is True
    assert deploy("revert", root, mcp_host()).startswith("MCP entry point revert verified")
    assert (root / "server.py").read_bytes() == ORIGINAL
    assert "reverting" not in json.loads((root / "plane-toolchain-ce-receipt.json").read_text())


def test_idempotent_revert_clears_a_stale_reverting_marker(root):
    deploy("apply", root, mcp_host())
    final = (root / "plane-toolchain-ce-receipt.json").read_text()
    # A revert killed after the rewrite but before the marker was cleared.
    (root / "server.py").write_bytes(ORIGINAL)
    marked = dict(json.loads(final), reverting=True)
    (root / "plane-toolchain-ce-receipt.json").write_text(json.dumps(marked))
    assert deploy("revert", root, mcp_host()) == "Original MCP entry point already restored"
    assert (root / "plane-toolchain-ce-receipt.json").read_text() == final


RELEASED = b"print('shipped in a past release')\n"


@pytest.fixture
def stale_receipt(root, monkeypatch):
    """Receipt names an older install, but server.py holds content shipped in a past release."""
    monkeypatch.setattr(
        mcp_deploy,
        "RELEASED_ENTRY_POINT_DIGESTS",
        (*mcp_deploy.RELEASED_ENTRY_POINT_DIGESTS, sha(RELEASED)),
    )
    fake = mcp_host()
    deploy("apply", root, fake, source="print('older ce tools')\n")
    (root / "server.py").write_bytes(RELEASED)
    return fake


def test_apply_accepts_released_content_under_stale_receipt(root, stale_receipt):
    backup = root / "server.py.before-plane-toolchain-ce"
    assert deploy("apply", root, stale_receipt).startswith("MCP entry point apply verified")
    assert (root / "server.py").read_bytes() == DESIRED.encode()
    assert backup.read_bytes() == ORIGINAL
    receipt = json.loads((root / "plane-toolchain-ce-receipt.json").read_text())
    assert receipt == {"installed": sha(DESIRED.encode()), "original": sha(ORIGINAL)}


def test_revert_accepts_released_content_under_stale_receipt(root, stale_receipt):
    assert deploy("revert", root, stale_receipt).startswith("MCP entry point revert verified")
    assert (root / "server.py").read_bytes() == ORIGINAL


def test_stale_receipt_still_refuses_foreign_content(root, stale_receipt):
    (root / "server.py").write_bytes(RELEASED + b"# host edit\n")
    with pytest.raises(ValueError, match="preserve it and reconcile"):
        deploy("apply", root, stale_receipt)
    with pytest.raises(ValueError, match="refusing overwrite"):
        deploy("revert", root, stale_receipt)
    assert (root / "server.py").read_bytes() == RELEASED + b"# host edit\n"


def test_check_ignores_released_digests(root, stale_receipt):
    with pytest.raises(ValueError, match="differs"):
        deploy("check", root, stale_receipt)


def test_remote_program_ships_released_digests():
    for digest in mcp_deploy.RELEASED_ENTRY_POINT_DIGESTS:
        assert digest in mcp_deploy.REMOTE


def test_released_digests_cover_every_tag():
    git = shutil.which("git")
    repo = Path(__file__).resolve().parents[1]
    tags = subprocess.run(  # noqa: S603 - fixed git argv
        [git or "git", "-C", str(repo), "tag", "--list", "v*"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    if not git or not tags:
        pytest.skip("git tags unavailable")
    for tag in tags:
        shown = subprocess.run(  # noqa: S603 - fixed git argv
            [git, "-C", str(repo), "show", f"{tag}:src/plane_toolchain/ce_tools.py"],
            capture_output=True,
            check=False,
        )
        assert shown.returncode == 0
        assert sha(shown.stdout) in mcp_deploy.RELEASED_ENTRY_POINT_DIGESTS, (
            f"append the ce_tools.py digest of {tag} to RELEASED_ENTRY_POINT_DIGESTS"
        )


# PTC-103: write_atomic never follows or reuses a temp file; corrupt receipt is readable.


def test_write_atomic_replaces_a_symlinked_temp_file_without_following_it(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("keep")
    (tmp_path / "out.tmp").symlink_to(victim)
    mcp_deploy.write_atomic(tmp_path / "out", "new", 0o640)
    assert victim.read_text() == "keep"
    assert (tmp_path / "out").read_text() == "new"
    assert not (tmp_path / "out.tmp").exists()


def test_write_atomic_does_not_reuse_a_stale_temp_file(tmp_path):
    stale = tmp_path / "out.tmp"
    stale.write_text("stale content that is longer")
    stale.chmod(0o666)
    mcp_deploy.write_atomic(tmp_path / "out", "new", 0o600)
    assert (tmp_path / "out").read_bytes() == b"new"
    assert (tmp_path / "out").stat().st_mode & 0o777 == 0o600


def test_write_atomic_open_refuses_to_follow_or_truncate(tmp_path, monkeypatch):
    flags = []
    real = mcp_deploy.os.open
    monkeypatch.setattr(mcp_deploy.os, "open", lambda p, f, *a: flags.append(f) or real(p, f, *a))
    mcp_deploy.write_atomic(tmp_path / "out", "x")
    needed = os.O_EXCL | os.O_NOFOLLOW
    assert flags[0] & needed == needed and not flags[0] & os.O_TRUNC


def test_write_atomic_without_mode_keeps_the_existing_mode(tmp_path):
    target = tmp_path / "out"
    target.write_text("old")
    target.chmod(0o640)
    mcp_deploy.write_atomic(target, "new")
    assert target.read_text() == "new"
    assert target.stat().st_mode & 0o777 == 0o640


def test_write_atomic_without_mode_creates_private_file(tmp_path):
    mcp_deploy.write_atomic(tmp_path / "out", "new")
    assert (tmp_path / "out").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("operation", ["apply", "revert"])
def test_corrupt_receipt_raises_readable_error(root, operation):
    (root / "plane-toolchain-ce-receipt.json").write_text("not json")
    with pytest.raises(ValueError, match="Receipt unreadable; reconcile"):
        deploy(operation, root, mcp_host())


def test_write_atomic_without_mode_never_inherits_a_symlink_targets_mode(tmp_path):
    target = tmp_path / "target"
    target.write_text("old")
    target.chmod(0o666)
    link = tmp_path / "out"
    link.symlink_to(target)
    mcp_deploy.write_atomic(link, "new")
    assert not link.is_symlink() and link.read_text() == "new"
    assert link.stat().st_mode & 0o777 == 0o600
    assert target.read_text() == "old" and target.stat().st_mode & 0o777 == 0o666


@pytest.mark.parametrize("text", ["1", "null", "[]"])
def test_non_object_receipt_raises_readable_error(root, text):
    (root / "plane-toolchain-ce-receipt.json").write_text(text)
    with pytest.raises(ValueError, match="Receipt unreadable; reconcile"):
        deploy("apply", root, mcp_host())


def timing_out_restarts(fake, which):
    """fake.docker, but the `which` restart (1-based) raises the shipped timeout error."""
    real, seen = fake.docker, []

    def docker(*args, **kw):
        if args[:1] == ("restart",):
            seen.append(args)
            if len(seen) in which:
                raise RuntimeError("docker restart timed out after 300 s")
        return real(*args, **kw)

    fake.docker = docker


def test_rollback_restart_timeout_keeps_both_causes_when_still_unhealthy(root, capsys):
    # PTC-115: the timeout replaced "still unhealthy"; the operator needs both facts.
    fake = mcp_host()
    fake.containers["plane-mcp"]["health"] = "starting"
    timing_out_restarts(fake, {2})  # the rollback restart
    with pytest.raises(RuntimeError, match="did not become healthy") as error:
        deploy("apply", root, fake)
    assert "still unhealthy" in str(error.value)
    assert "docker restart timed out after 300 s" in capsys.readouterr().err
    assert (root / "server.py").read_bytes() == ORIGINAL


def test_rollback_restart_timeout_after_an_interrupt_keeps_both_causes(root, capsys):
    fake = mcp_host()
    fake.containers["plane-mcp"]["health"] = "starting"
    timing_out_restarts(fake, {2})
    calls = []

    def sleep(seconds):
        calls.append(seconds)
        if len(calls) == 1:
            raise KeyboardInterrupt

    data = dict(operation="apply", directory=str(root), container="plane-mcp", source=V1)
    with pytest.raises(KeyboardInterrupt):
        mcp_deploy.deploy(data, fake.docker, lambda path: False, sleep=sleep)
    err = capsys.readouterr().err
    assert "MCP still unhealthy after rollback" in err
    assert "docker restart timed out after 300 s" in err
