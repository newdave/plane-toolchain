"""Receipt and journal state files of the workspace rename (shipped to the host)."""

import hashlib
import json
import os
import pathlib
import stat
import time

from plane_toolchain.mcp_deploy import fsync_directory, write_atomic


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
        raise RuntimeError(
            "receipt belongs to a different rename; if that rename is finished, archive the "
            "receipt directory (move it aside whole, never delete it) before a new one"
        )
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


def repair_journal_tail(descriptor):
    """Make an open (O_APPEND) journal end in a newline before the next record is appended.

    A kill mid-append can leave a final line with no newline. Gluing a new record onto it would
    corrupt both. A tail that is a whole JSON object only lacks its newline: add it. Any other
    tail is a partial record: truncate back to the last newline. Both are fsynced."""
    size = os.fstat(descriptor).st_size
    if not size or os.pread(descriptor, 1, size - 1) == b"\n":
        return
    tail, start = b"", size
    while start > 0:
        start = max(0, start - 65536)
        tail = os.pread(descriptor, size - start - len(tail), start) + tail
        if b"\n" in tail:
            break
    cut = tail.rfind(b"\n") + 1
    fragment = tail[cut:]
    try:
        whole = isinstance(json.loads(fragment), dict)
    except ValueError:
        whole = False
    if whole:
        os.write(descriptor, b"\n")
    else:
        os.ftruncate(descriptor, start + cut)
    os.fsync(descriptor)


def append_journal(directory, record, uid=None):
    """Append one sorted-key JSON line to records.jsonl and fsync before returning."""
    uid = os.geteuid() if uid is None else uid
    secure_receipt_dir(directory, uid)
    path = pathlib.Path(directory) / "records.jsonl"
    created = not os.path.lexists(path)
    if not created:
        private_file_check(path, uid, False)
    line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    descriptor = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not created:
            repair_journal_tail(descriptor)
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


def set_receipt_field(directory, name, value, uid=None):
    """Set one top-level receipt field, keeping step state. Atomic 0600 rewrite."""
    uid = os.geteuid() if uid is None else uid
    secure_receipt_dir(directory, uid)
    path = pathlib.Path(directory) / "receipt.json"
    private_file_check(path, uid, False)
    try:
        receipt = json.loads(path.read_text())
    except ValueError:
        raise RuntimeError("receipt is unreadable") from None
    receipt[name] = value
    write_receipt(directory, receipt)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def leftover_files(directory):
    """Names of journals and backups in `directory`, current or rotated, sorted."""
    return sorted(
        name
        for name in os.listdir(directory)
        if name in ("records.jsonl", "backup.dump")
        or (name.startswith("records-") and name.endswith(".jsonl"))
        or (name.startswith("backup-") and name.endswith(".dump"))
    )


def journal_workspace(records):
    """The renamed workspace id from the journal's workspace record, or None. Two ids refuse."""
    ids = {r.get("id") for r in records if isinstance(r, dict) and r.get("kind") == "workspace"}
    if len(ids) > 1:
        raise RuntimeError("journal names more than one workspace; refusing")
    return ids.pop() if ids else None


def read_receipt(directory, uid):
    """The raw receipt of `directory`, after the private-file checks."""
    path = pathlib.Path(directory) / "receipt.json"
    secure_receipt_dir(directory, uid)
    private_file_check(path, uid, False)
    try:
        return json.loads(path.read_text())
    except ValueError:
        raise RuntimeError("receipt is unreadable") from None


def finish_rotation(directory, uid):
    """Complete the rotations a reset recorded in `rotating` ({file: new name}); never delete.

    Link, then unlink, so a kill at any point leaves each file under at least one name."""
    directory = pathlib.Path(directory)
    receipt = read_receipt(directory, uid)
    rotating = receipt.get("rotating") or {}
    for source, target in sorted(rotating.items()):
        current, rotated = directory / source, directory / target
        if os.path.lexists(current):
            private_file_check(current, uid, False)
            if not os.path.lexists(rotated):
                os.link(current, rotated)
            elif not os.path.samefile(current, rotated):
                raise RuntimeError("rotated file name is taken; refusing")
            os.unlink(current)
            fsync_directory(directory)
        elif not os.path.lexists(rotated):
            raise RuntimeError("file set aside by the reset is missing; refusing")
    if "rotating" in receipt:
        receipt.pop("rotating")
        write_receipt(directory, receipt)


def reset_receipt(directory, clock, uid, crash=lambda label: None):
    """Explicitly put an apply receipt back to `pending` after a pre-slug failure.

    `advance` never moves backward; this is the one deliberate exception. The backup and the
    journal are rotated to timestamped names (kept, never deleted; listed in `rotated_backups`
    and `rotated_journals`) and the backup SHA-256 cleared, so the next apply stops services
    again, takes a fresh dump and journals the originals it actually rewrites. A pre-slug
    journal is safe to set aside: the slug transaction rolled back, so nothing it names was
    changed. The receipt is written first, naming the rotations; finish_rotation completes them
    now or on the next run."""
    directory = pathlib.Path(directory)
    receipt = read_receipt(directory, uid)
    if receipt.get("phase") != "apply":
        raise RuntimeError("only an apply receipt can be reset")
    rotating = dict(receipt.get("rotating") or {})
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(clock()))
    rotations = {"backup.dump": "rotated_backups", "records.jsonl": "rotated_journals"}
    for source, field in sorted(rotations.items()):
        if source in rotating or not os.path.lexists(directory / source):
            continue
        stem, _, suffix = source.partition(".")
        name, serial = stem + "-" + stamp + "." + suffix, 0
        taken = set(receipt.get(field) or [])
        while name in taken or os.path.lexists(directory / name):
            serial += 1
            name = stem + "-" + stamp + "-" + str(serial) + "." + suffix
        rotating[source] = name
        receipt[field] = [*(receipt.get(field) or []), name]
    if rotating:
        receipt["rotating"] = rotating
    receipt["step"] = "pending"
    receipt.pop("backup_sha256", None)
    write_receipt(directory, receipt)
    crash("after_reset_receipt")
    finish_rotation(directory, uid)


def verify_backup(directory, receipt, uid):
    """Check backup.dump against the receipt before anything runs; return the action needed.

    "rotate": a reset recorded a rotation that a kill interrupted. "adopt": a dump without a
    recorded SHA-256 at `stopped`, because backup_database links the dump only after a
    complete, fsynced pg_dump, so a crash between the link and the receipt write leaves exactly
    that state. None: nothing to do. A recorded SHA-256 must match the file; anything else
    refuses.
    """
    dump = pathlib.Path(directory) / "backup.dump"
    receipt = receipt or {}
    if receipt.get("rotating") and receipt.get("step") == "pending":
        return "rotate"
    recorded = receipt.get("backup_sha256")
    present = os.path.lexists(dump)
    if present:
        private_file_check(dump, uid, False)
    step = receipt.get("step")
    order = step_order("apply")
    taken = step in order and order.index(step) >= order.index("backed_up")
    if recorded:
        if not present:
            raise RuntimeError("backup.dump is missing; refusing")
        if sha256_file(dump) != recorded:
            raise RuntimeError("backup.dump does not match the recorded SHA-256; refusing")
        return None
    if present:
        if step != "stopped":
            raise RuntimeError("backup.dump has no recorded SHA-256; refusing")
        return "adopt"
    if taken:
        raise RuntimeError("receipt is past the backup but has no backup SHA-256; refusing")
    return None


SHIPPED = (
    fsync_directory,
    write_atomic,
    secure_receipt_dir,
    private_file_check,
    step_order,
    write_receipt,
    new_receipt,
    load_receipt,
    advance,
    set_receipt_field,
    receipt_dir,
    repair_journal_tail,
    append_journal,
    load_journal,
    sha256_file,
    leftover_files,
    journal_workspace,
    read_receipt,
    finish_rotation,
    reset_receipt,
    verify_backup,
)
