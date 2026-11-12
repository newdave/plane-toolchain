"""Workspace slug rename: owner-side tooling.

This slice holds only the host receipt and journal store. These functions later run on the
host inside the stdlib-only REMOTE built by program(), so they take every path as an argument
and use the standard library only. For program(), ship: write_atomic (from mcp_deploy),
receipt_dir, secure_receipt_dir, private_file_check, step_order, write_receipt, new_receipt,
load_receipt, advance, append_journal and load_journal.

Messages never carry receipt or journal content.
"""

import json
import os
import pathlib
import stat

from plane_toolchain.mcp_deploy import write_atomic  # shipped to the host by program()


def receipt_dir(environ, home=None):
    """Receipt directory: PLANE_RENAME_RECEIPT_DIR, else under the SSH user's home."""
    configured = environ.get("PLANE_RENAME_RECEIPT_DIR")
    if configured:
        return pathlib.Path(configured)
    base = pathlib.Path(home) if home is not None else pathlib.Path.home()
    return base / ".local" / "state" / "plane-toolchain" / "workspace-rename"


def private_file_check(path, uid, directory):
    """Refuse a symlink, wrong type, group/other access or foreign owner."""
    info = os.lstat(path)
    wanted = stat.S_ISDIR if directory else stat.S_ISREG
    if stat.S_ISLNK(info.st_mode) or not wanted(info.st_mode):
        raise RuntimeError(
            "receipt store path is not a plain " + ("directory" if directory else "file")
        )
    if info.st_mode & 0o077:
        raise RuntimeError("receipt store path has loose permissions")
    if info.st_uid != uid:
        raise RuntimeError("receipt store path has a foreign owner")


def secure_receipt_dir(directory, uid=None):
    """Create the directory 0700 if absent; refuse a symlink, loose mode or foreign owner."""
    uid = os.geteuid() if uid is None else uid
    directory = pathlib.Path(directory)
    if not os.path.lexists(directory):
        directory.parent.mkdir(parents=True, exist_ok=True)
        os.mkdir(directory, 0o700)
        os.chmod(directory, 0o700)
    private_file_check(directory, uid, True)
    return directory


def step_order(phase):
    if phase == "revert":
        return ("reverting", "restored", "started", "reverted")
    return (
        "pending",
        "stopped",
        "backed_up",
        "slug_done",
        "rows_done",
        "cache_done",
        "started",
        "complete",
    )


def write_receipt(directory, receipt):
    """Atomic 0600 write: write_atomic fsyncs the file and the directory."""
    path = pathlib.Path(directory) / "receipt.json"
    temporary = str(path) + ".tmp"
    if os.path.lexists(temporary):
        os.unlink(temporary)
    previous = os.umask(0o077)
    try:
        write_atomic(path, json.dumps(receipt, sort_keys=True))
    finally:
        os.umask(previous)


def load_receipt(directory, old, new, uid=None):
    """Return the receipt, or None if absent. Refuse unsafe files and other renames."""
    uid = os.geteuid() if uid is None else uid
    secure_receipt_dir(directory, uid)
    path = pathlib.Path(directory) / "receipt.json"
    if not os.path.lexists(path):
        return None
    private_file_check(path, uid, False)
    try:
        receipt = json.loads(path.read_text())
    except ValueError:
        raise RuntimeError("receipt is unreadable") from None
    if not isinstance(receipt, dict) or receipt.get("from") != old or receipt.get("to") != new:
        raise RuntimeError("receipt belongs to a different rename")
    return receipt


def new_receipt(directory, old, new, uid=None):
    """Write a pending receipt, or return the existing one for the same rename."""
    existing = load_receipt(directory, old, new, uid)
    if existing is not None:
        return existing
    receipt = {"from": old, "to": new, "phase": "apply", "step": "pending"}
    write_receipt(directory, receipt)
    return receipt


def advance(directory, step, uid=None):
    """Move the receipt forward only. Re-entering the current step is a safe re-run."""
    uid = os.geteuid() if uid is None else uid
    path = pathlib.Path(directory) / "receipt.json"
    secure_receipt_dir(directory, uid)
    private_file_check(path, uid, False)
    try:
        receipt = json.loads(path.read_text())
    except ValueError:
        raise RuntimeError("receipt is unreadable") from None
    phase, current = receipt.get("phase"), receipt.get("step")
    if phase in ("apply", "revert") and step in step_order(phase):
        order = step_order(phase)
        if current not in order or order.index(step) < order.index(current):
            raise RuntimeError("receipt state cannot move backwards")
        target_phase = phase
    elif step == "reverting" and phase == "apply":
        target_phase = "revert"  # revert may begin from any apply state
    elif step in step_order("apply") + step_order("revert"):
        raise RuntimeError("receipt state cannot move backwards")
    else:
        raise RuntimeError("unknown receipt step")
    receipt["phase"], receipt["step"] = target_phase, step
    write_receipt(directory, receipt)
    return receipt


def append_journal(directory, record, uid=None):
    """Append one sorted-key JSON line to records.jsonl and fsync before returning."""
    uid = os.geteuid() if uid is None else uid
    secure_receipt_dir(directory, uid)
    path = pathlib.Path(directory) / "records.jsonl"
    created = not os.path.lexists(path)
    if not created:
        private_file_check(path, uid, False)
    line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.write(descriptor, line.encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if created:
        parent = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)


def load_journal(directory, uid=None):
    """Return journal records. A torn trailing line is ignored; any other bad line is refused."""
    uid = os.geteuid() if uid is None else uid
    secure_receipt_dir(directory, uid)
    path = pathlib.Path(directory) / "records.jsonl"
    if not os.path.lexists(path):
        return []
    private_file_check(path, uid, False)
    lines = path.read_bytes().split(b"\n")
    records = []
    for index, raw in enumerate(lines):
        if not raw:
            continue
        try:
            records.append(json.loads(raw))
        except ValueError:
            if index == len(lines) - 1:
                break  # torn final line: the crash happened mid-append
            raise RuntimeError("journal has a corrupt line") from None
    return records
