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
    assert fake.calls[:3] == [git_call(root), RESTART, RESTART]
    # The backup remains after a failed first apply; no receipt was written.
    assert (root / "server.py.before-terrainlink-ce").exists()
    assert not (root / "terrainlink-ce-receipt.json").exists()


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
    # Legacy receipt names survive a rolled-back apply.
    assert (root / "server.py.before-terrainlink-ce").read_bytes() == ORIGINAL
    assert not (root / "terrainlink-ce-receipt.json").exists()


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
    assert not (root / "terrainlink-ce-receipt.json").exists()


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


V1 = "print('v1')\n"
V2 = "print('v2')\n"


def upgraded_host(root):
    fake = mcp_host()
    deploy("apply", root, fake, source=V1)
    return fake, (root / "terrainlink-ce-receipt.json").read_bytes()


def test_failed_upgrade_keeps_receipt_so_revert_and_reapply_work(root):
    fake, receipt = upgraded_host(root)
    fake.containers["plane-mcp"]["health"] = "starting"
    with pytest.raises(RuntimeError, match="did not become healthy"):
        deploy("apply", root, fake, source=V2)
    assert (root / "server.py").read_text() == V1
    assert (root / "terrainlink-ce-receipt.json").read_bytes() == receipt
    fake.containers["plane-mcp"]["health"] = "healthy"
    # Revert works from the rolled-back state...
    assert deploy("revert", root, fake).startswith("MCP entry point revert verified")
    assert (root / "server.py").read_bytes() == ORIGINAL
    # ...and re-applying v2 succeeds, then records the new receipt.
    assert deploy("apply", root, fake, source=V2).startswith("MCP entry point apply verified")
    new = json.loads((root / "terrainlink-ce-receipt.json").read_text())
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
    assert (root / "terrainlink-ce-receipt.json").read_bytes() == receipt


def test_check_ignores_corrupt_receipt(root):
    (root / "server.py").write_text(DESIRED)
    (root / "terrainlink-ce-receipt.json").write_text("not json")
    assert deploy("check", root, mcp_host()) == "MCP entry point verified"


def test_old_format_receipt_without_original_still_works(root):
    fake, _ = upgraded_host(root)
    (root / "terrainlink-ce-receipt.json").write_text(json.dumps({"installed": sha(V1.encode())}))
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
    receipt = root / "terrainlink-ce-receipt.json"
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
    receipt = root / "terrainlink-ce-receipt.json"
    assert json.loads(receipt.read_text()) == {
        "installed": sha(V2.encode()),
        "original": sha(ORIGINAL),
    }
    assert not (root / "terrainlink-ce-receipt.json.tmp").exists()


def test_first_apply_pending_receipt_has_null_previous(root):
    seen = []
    watch_receipt(root, mcp_host(), DESIRED, seen)
    assert seen[0]["previous"] is None and seen[0]["pending"] is True


def stop_first_apply(root):
    (root / "server.py.before-terrainlink-ce").write_bytes(ORIGINAL)
    (root / "server.py").write_text(V1)
    (root / "terrainlink-ce-receipt.json").write_text(pending(V1.encode(), None))


def stop_upgrade(root, fake):
    deploy("apply", root, fake, source=V1)
    (root / "server.py").write_text(V2)
    (root / "terrainlink-ce-receipt.json").write_text(pending(V2.encode(), sha(V1.encode())))


def test_stopped_first_apply_can_revert(root):
    stop_first_apply(root)
    assert deploy("revert", root, mcp_host()).startswith("MCP entry point revert verified")
    assert (root / "server.py").read_bytes() == ORIGINAL


def test_stopped_first_apply_can_apply_again_and_repairs_receipt(root):
    stop_first_apply(root)
    fake = mcp_host()
    message = deploy("apply", root, fake, source=V1)
    assert message == "MCP compatibility entry point already applied"
    assert json.loads((root / "terrainlink-ce-receipt.json").read_text()) == {
        "installed": sha(V1.encode()),
        "original": sha(ORIGINAL),
    }
    assert deploy("apply", root, fake, source=V2).startswith("MCP entry point apply verified")


def test_stopped_first_apply_missing_receipt_is_repaired(root):
    stop_first_apply(root)
    (root / "terrainlink-ce-receipt.json").unlink()
    deploy("apply", root, mcp_host(), source=V1)
    assert (root / "terrainlink-ce-receipt.json").exists()


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
    assert (root / "terrainlink-ce-receipt.json").read_bytes() == receipt


def test_interrupt_on_first_apply_removes_pending_receipt(root):
    interrupted(root, mcp_host(), V1)
    assert (root / "server.py").read_bytes() == ORIGINAL
    assert not (root / "terrainlink-ce-receipt.json").exists()


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
    (root / "server.py.before-terrainlink-ce").write_bytes(b"tampered\n")
    with pytest.raises(ValueError, match="Backup does not match"):
        deploy("apply", root, fake, source=V1)
    assert (root / "terrainlink-ce-receipt.json").read_bytes() == receipt


def test_receipt_write_fsyncs_directory(root, monkeypatch):
    kinds = []
    real = mcp_deploy.os.fsync
    monkeypatch.setattr(mcp_deploy.os, "fsync", lambda fd: (kinds.append(fd), real(fd)))
    opened = []
    real_open = mcp_deploy.os.open
    monkeypatch.setattr(mcp_deploy.os, "open", lambda *a: opened.append(a) or real_open(*a))
    deploy("apply", root, mcp_host())
    assert any(call[0] == root.resolve() or call[0] == root for call in opened)
