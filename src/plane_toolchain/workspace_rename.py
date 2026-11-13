"""Workspace slug rename: owner-side tooling.

This slice holds only the host receipt and journal store. These functions later run on the
host inside the stdlib-only REMOTE built by program(), so they take every path as an argument
and use the standard library only. For program(), ship: write_atomic (from mcp_deploy),
receipt_dir, secure_receipt_dir, private_file_check, step_order, write_receipt, new_receipt,
load_receipt, advance, append_journal and load_journal.

Messages never carry receipt or journal content.
"""

import hashlib
import json
import os
import pathlib
import stat
import subprocess

from plane_toolchain.mcp_deploy import write_atomic  # shipped to the host by program()
from plane_toolchain.remote import say  # shipped to the host by program()

# Outage-window Docker orchestration. Only `docker stop`, `start`, `restart` and `exec` (plus
# read-only `inspect`) are used; nothing here changes a container definition. The program()
# shipping list is SHIPPED (functions) and SHIPPED_CONSTANTS, with imports hashlib, json, os,
# pathlib, stat and subprocess. `docker` is remote.docker; `crash` is a test seam.
PG_DUMP = 'pg_dump -Fc -U "$POSTGRES_USER" "$POSTGRES_DB"'
SALTBOX_LABEL = "com.github.saltbox.saltbox_managed"
DRAIN_SECONDS = 60
DRAIN_INTERVAL = 2


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


def service_names(environ):
    """Container names from the environment; defaults are the live Saltbox names."""
    defaults = (
        ("db", "PLANE_DB_CONTAINER", "plane-db"),
        ("redis", "PLANE_REDIS_CONTAINER", "plane-redis"),
        ("web", "PLANE_WEB_CONTAINER", "plane"),
        ("space", "PLANE_SPACE_CONTAINER", "plane-space"),
        ("admin", "PLANE_ADMIN_CONTAINER", "plane-admin"),
        ("worker", "PLANE_WORKER_CONTAINER", "plane-worker"),
        ("beat", "PLANE_BEAT_CONTAINER", "plane-beat-worker"),
        ("live", "PLANE_LIVE_CONTAINER", "plane-live"),
        ("mq", "PLANE_MQ_CONTAINER", "plane-mq"),
    )
    return {key: environ.get(variable) or default for key, variable, default in defaults}


def queue_depth(mq, docker):
    """Total messages (ready and unacknowledged) across all RabbitMQ queues."""
    try:
        out = docker("exec", mq, "rabbitmqctl", "list_queues", "-q", "name", "messages")
    except subprocess.CalledProcessError:
        raise RuntimeError("queue probe failed") from None
    total = 0
    for line in out.decode().splitlines():
        fields = line.split()
        if fields and fields[-1].isdigit():
            total += int(fields[-1])
    return total


def drain_queue(mq, docker, clock, sleep, timeout=DRAIN_SECONDS, interval=DRAIN_INTERVAL):
    """Poll until the queue is empty or timeout passes; return jobs left. Never purges."""
    deadline = clock() + timeout
    remaining = queue_depth(mq, docker)
    while remaining and clock() < deadline:
        sleep(interval)
        remaining = queue_depth(mq, docker)
    return remaining


def stop_services(names, docker, clock, sleep, crash=lambda label: None):
    """Beat, drain, worker, web, space, admin; then restart Live. API, db and Redis stay up."""
    crash("before_stop_" + names["beat"])
    docker("stop", names["beat"])
    crash("after_stop_" + names["beat"])
    remaining = drain_queue(names["mq"], docker, clock, sleep)
    say("Queue drained" if not remaining else f"{remaining} jobs still queued; not purged")
    crash("after_drain")
    for key in ("worker", "web", "space", "admin"):
        crash("before_stop_" + names[key])
        docker("stop", names[key])
        crash("after_stop_" + names[key])
    crash("before_restart_" + names["live"])
    docker("restart", names["live"])
    crash("after_restart_" + names["live"])
    return remaining


def start_services(names, docker, crash=lambda label: None):
    """Start the stopped services in reverse stop order. Live was restarted, not stopped."""
    for key in ("admin", "space", "web", "worker", "beat"):
        crash("before_start_" + names[key])
        docker("start", names[key])
        crash("after_start_" + names[key])


def detect_saltbox(names, docker):
    """Report whether the database container looks Saltbox-managed. Information only."""
    try:
        document = json.loads(docker("inspect", names["db"]))[0]
        labels = document.get("Config", {}).get("Labels") or {}
    except (subprocess.CalledProcessError, ValueError, IndexError):
        labels = {}
    found = labels.get(SALTBOX_LABEL) == "true"
    say("Saltbox-managed host: " + ("yes" if found else "no") + " (information only)")
    return found


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


def backup_database(db, directory, popen, crash=lambda label: None, uid=None):
    """Stream pg_dump to backup.dump (0600), hashing and fsyncing; record the SHA-256.

    A failed or empty dump raises before any caller changes data, leaving no dump file.
    """
    uid = os.geteuid() if uid is None else uid
    secure_receipt_dir(directory, uid)
    final = pathlib.Path(directory) / "backup.dump"
    temporary = str(final) + ".tmp"
    if os.path.lexists(final):
        # Never replace the only pre-change backup. A dump on disk without a recorded hash
        # (crash window) is for T7's resume logic to resolve.
        raise RuntimeError("backup.dump already exists in the receipt directory; refusing")
    if os.path.lexists(temporary):
        os.unlink(temporary)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    digest, size, failure, process = hashlib.sha256(), 0, None, None
    try:
        process = popen(
            ["docker", "exec", db, "sh", "-c", PG_DUMP],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        while True:
            chunk = process.stdout.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            os.write(descriptor, chunk)
        if process.wait() != 0:
            failure = "backup failed: pg_dump exited with an error"
        elif not size:
            failure = "backup refused: pg_dump produced empty output"
        else:
            os.fsync(descriptor)
    except Exception:
        if process is not None:
            process.kill()
            process.wait()
        os.close(descriptor)
        os.unlink(temporary)
        raise
    os.close(descriptor)
    if failure:
        os.unlink(temporary)
        raise RuntimeError(failure)
    try:
        os.link(temporary, final)  # fails atomically if a dump appeared meanwhile
    finally:
        os.unlink(temporary)
    parent = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
    crash("after_backup_file")
    set_receipt_field(directory, "backup_sha256", digest.hexdigest(), uid)
    return digest.hexdigest()


SHIPPED = (
    secure_receipt_dir,
    private_file_check,
    step_order,
    write_receipt,
    new_receipt,
    load_receipt,
    advance,
    service_names,
    queue_depth,
    drain_queue,
    stop_services,
    start_services,
    detect_saltbox,
    set_receipt_field,
    backup_database,
)
SHIPPED_CONSTANTS = {
    "PG_DUMP": PG_DUMP,
    "SALTBOX_LABEL": SALTBOX_LABEL,
    "DRAIN_SECONDS": DRAIN_SECONDS,
    "DRAIN_INTERVAL": DRAIN_INTERVAL,
}
