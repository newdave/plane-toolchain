"""Reversible owner-approved MCP entry-point update; leave Docker settings intact."""

import ast
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time
from pathlib import Path

from plane_toolchain.legacy import (
    MCP,
    MCP_BACKUP_NAME,
    MCP_RECEIPT_NAME,
    NEUTRAL_MCP_BACKUP_NAME,
    NEUTRAL_MCP_RECEIPT_NAME,
    refusal,
)
from plane_toolchain.remote import docker, guarded, program, require_env, run_remote, say


def git_tracked(root):
    """Return whether git tracks the host entry point; never raise."""
    result = subprocess.run(  # noqa: S603 - fixed git argv
        ["git", "-C", str(root), "ls-files", "--error-unmatch", "server.py"],  # noqa: S607 - git on PATH
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def rewrite(target, content):
    """Overwrite target in place so the bind-mounted inode survives; flush and fsync.

    Not atomic: a crash between truncate and fsync leaves a partial file. The backup
    (`NEUTRAL_MCP_BACKUP_NAME`, see legacy.py) holds the upstream original, not the previous
    install: restore it by hand, or re-apply.
    """
    with open(target, "r+b") as handle:
        handle.truncate(0)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def write_atomic(path, text):
    """Write text next to path, fsync, then os.replace so readers see old or new, never partial."""
    temporary = str(path) + ".tmp"
    with open(temporary, "w") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    # Sync the directory so a power loss cannot bring back the older file.
    fsync_directory(pathlib.Path(path).parent)


def fsync_directory(directory):
    """Sync a directory so a rename or unlink survives power loss."""
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def legacy_state(root):
    """Sorted legacy-named files present in root: backup, receipt, receipt temp. Names only."""
    names = (MCP_BACKUP_NAME, MCP_RECEIPT_NAME, MCP_RECEIPT_NAME + ".tmp")
    return sorted(name for name in names if os.path.lexists(root / name))


def owned_digests(recorded):
    """Digests of entry points this tool may overwrite: installed, plus previous if pending."""
    digests = {recorded.get("installed")}
    if recorded.get("pending"):
        digests.add(recorded.get("previous"))
    digests.discard(None)
    return digests


def plain_entry_point(content):
    """Return whether content is a bare upstream-style entry point (no defs or classes)."""
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError):
        return False
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    defs = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    return "get_header_mcp" in names and not any(isinstance(n, defs) for n in ast.walk(tree))


def inspect_state(docker, container):
    """Return (running, restarting, health status or None, restart count); no config leaves."""
    document = json.loads(docker("inspect", container))[0]
    state = document["State"]
    health = (state.get("Health") or {}).get("Status")
    return (
        bool(state.get("Running")),
        bool(state.get("Restarting")),
        health,
        document.get("RestartCount", 0),
    )


def wait_healthy(probe, sleep, attempts=45, stable=5):
    """Poll until running, not restarting, healthy and stable; True on success.

    With a healthcheck, two consecutive healthy probes pass. Without one, good probes
    must span `stable` seconds. A slow healthcheck (long interval or start_period) may
    need more attempts than the default 45.
    """
    baseline = None
    good = 0
    for attempt in range(attempts):
        if attempt:
            sleep(1)
        try:
            running, restarting, health, count = probe()
        except Exception:  # noqa: BLE001 - a failed probe counts as not yet healthy
            good = 0
            continue
        if baseline is None:
            baseline = count
        if count != baseline:
            return False
        ok = running and not restarting and health in (None, "healthy")
        good = good + 1 if ok else 0
        if good >= (2 if health else stable + 1):
            return True
    return False


def deploy(data, docker, tracked, probe=None, sleep=time.sleep):
    """Run one operation on the host entry point; return the operator message.

    docker(*args, **kw) -> bytes raises on failure unless check=False;
    tracked(root) -> bool never raises; probe() -> inspect_state tuple; sleep(seconds).
    """
    root = pathlib.Path(data["directory"]).resolve(strict=True)
    # Tripwire: refuse legacy-named state before reading or changing anything.
    found = legacy_state(root)
    if found:
        raise ValueError(refusal(found))
    target = root / "server.py"
    if target.is_symlink() or not target.is_file():
        raise ValueError("Expected regular MCP entry point")
    if tracked(root):
        raise ValueError("Refusing to edit tracked host file")
    current = target.read_bytes()
    operation = data["operation"]
    desired = data["source"].encode()
    compile(desired, str(target), "exec")
    if operation == "check":
        if current != desired:
            raise ValueError("MCP compatibility entry point differs")
        return "MCP entry point verified"
    backup = root / NEUTRAL_MCP_BACKUP_NAME
    receipt = root / NEUTRAL_MCP_RECEIPT_NAME
    old_receipt = receipt.read_text() if receipt.exists() else None
    recorded = json.loads(old_receipt) if old_receipt is not None else {}
    if backup.exists() and "original" in recorded:
        if sha256(backup.read_bytes()) != recorded["original"]:
            raise ValueError("Backup does not match the recorded original; reconcile")
    if operation == "apply":
        if current == desired:
            # Repair a missing, stale or pending receipt (e.g. after a killed deploy).
            final = {"installed": sha256(desired)}
            if backup.exists():
                final["original"] = sha256(backup.read_bytes())
                if recorded != final or not receipt.exists():
                    write_atomic(receipt, json.dumps(final))
            return "MCP compatibility entry point already applied"
        if backup.exists():
            if current != backup.read_bytes() and sha256(current) not in owned_digests(recorded):
                raise ValueError("Host entry point changed; preserve it and reconcile")
        else:
            # Digest first: a receipt that recorded the original is authoritative.
            if "original" in recorded:
                known = sha256(current) == recorded["original"]
            else:
                known = plain_entry_point(current)
            if not known:
                raise ValueError("Unexpected original entry point; review before replacing")
            backup.write_bytes(current)
            backup.chmod(target.stat().st_mode & 0o777)
        content = desired
    elif operation == "revert":
        if not backup.exists() or not receipt.exists():
            raise ValueError("No compatibility backup available")
        previous = backup.read_bytes()
        if current == previous:
            return "Original MCP entry point already restored"
        if sha256(current) not in owned_digests(recorded):
            raise ValueError("Host entry point changed; refusing overwrite")
        content = previous
    else:
        raise ValueError("Expected apply, revert or check")
    if probe is None:

        def probe():
            return inspect_state(docker, data["container"])

    def restore_receipt():
        if old_receipt is not None:
            write_atomic(receipt, old_receipt)
        elif receipt.exists():
            receipt.unlink()

    def restore():
        """Put the previous bytes and receipt back, restart; return whether MCP came back."""
        rewrite(target, current)
        if operation == "apply":
            restore_receipt()
        docker("restart", data["container"], check=False, stdout=subprocess.DEVNULL)
        return wait_healthy(probe, sleep)

    if operation == "apply":
        # Pending receipt first: a kill during restart or health wait stays recoverable, since
        # revert and apply accept either the new or the previous digest while it is pending.
        pending = {
            "installed": sha256(content),
            "previous": recorded.get("installed"),
            "original": sha256(backup.read_bytes()),
            "pending": True,
        }
        write_atomic(receipt, json.dumps(pending))
    # This file contains code only; Docker credentials and configuration stay untouched.
    try:
        rewrite(target, content)
    except BaseException:
        rewrite(target, current)
        if operation == "apply":
            restore_receipt()
        raise
    try:
        docker("restart", data["container"], stdout=subprocess.DEVNULL)
        healthy = wait_healthy(probe, sleep)
    except BaseException:
        if not restore():
            say("MCP still unhealthy after rollback", sys.stderr)
        raise
    if not healthy:
        if restore():
            raise RuntimeError("MCP did not become healthy; previous entry point restored")
        raise RuntimeError(
            "MCP did not become healthy; previous entry point restored; MCP still unhealthy"
        )
    if operation == "apply":
        final = {"installed": sha256(content), "original": sha256(backup.read_bytes())}
        write_atomic(receipt, json.dumps(final))
    if target.read_bytes() != content:
        raise ValueError("Entry point write not verified")
    return "MCP entry point " + operation + " verified; verify authenticated MCP access separately"


REMOTE = program(
    ["ast", "hashlib", "json", "os", "pathlib", "subprocess", "sys", "time"],
    [
        docker,
        git_tracked,
        sha256,
        rewrite,
        write_atomic,
        fsync_directory,
        legacy_state,
        refusal,
        owned_digests,
        plain_entry_point,
        inspect_state,
        wait_healthy,
        deploy,
    ],
    guarded("say(deploy(json.load(sys.stdin), docker, git_tracked))\n"),
    MCP,
)


def main():
    operation = sys.argv[1]
    host, directory = require_env("PLANE_MCP_SSH", "PLANE_MCP_DIRECTORY")
    container = os.environ.get("PLANE_MCP_CONTAINER", "plane-mcp")
    if host.startswith("-") or not directory.startswith("/") or container.startswith("-"):
        raise ValueError("Invalid MCP deployment destination")
    payload = {
        "operation": operation,
        "directory": directory,
        "container": container,
        "source": Path(__file__).with_name("ce_tools.py").read_text(),
    }
    run_remote(host, REMOTE, payload)


if __name__ == "__main__":
    main()
