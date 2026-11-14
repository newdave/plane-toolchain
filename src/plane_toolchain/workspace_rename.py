"""Workspace slug rename: owner-side tooling.

These functions run on the host inside the stdlib-only REMOTE built by program(), so they take
every path as an argument and use the standard library only. SHIPPED lists what program()
ships: fsync_directory and write_atomic (from mcp_deploy), and the receipt, Docker, backup and
apply state machine functions below. REMOTE is the assembled host program; `deploy` is its
entry point. `snippet_source` (owner side, not shipped) builds the Django snippet that the
payload carries to `manage.py shell`.
`say` and the `docker` helper come from remote.py; program() adds `say` itself.

Messages never carry receipt or journal content.
"""

import fcntl
import hashlib
import inspect
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time

from plane_toolchain import workspace_rename_backend, workspace_rename_match
from plane_toolchain.mcp_deploy import (
    fsync_directory,
    write_atomic,
)  # shipped to the host by program()
from plane_toolchain.remote import docker, program, say  # docker and say are shipped

# Outage-window Docker orchestration. Only `docker stop`, `start`, `restart` and `exec` (plus
# read-only `inspect`) are used; nothing here changes a container definition. REMOTE ships
# FUNCTIONS (remote.docker plus SHIPPED) with SHIPPED_CONSTANTS and IMPORTS (hashlib, json, os,
# pathlib, stat, subprocess and time). `crash(label)` is a test seam, a no-op on the host.
PG_DUMP = 'pg_dump -Fc -U "$POSTGRES_USER" "$POSTGRES_DB"'
DB_SIZE = (
    'psql -tA -U "$POSTGRES_USER" "$POSTGRES_DB" -c "select pg_database_size(current_database())"'
)
MIN_FREE_BYTES = 64 << 20
SALTBOX_LABEL = "com.github.saltbox.saltbox_managed"
DRAIN_SECONDS = 60
DRAIN_INTERVAL = 2
SNIPPET_IDLE_SECONDS = 900  # silent this long: hung, kill it (restarts per line; see PROGRESS)
KILL_WAIT = 30  # seconds the kill script may take before its client is killed (not confirmed)
SNIPPET_MARKER = "PLANE_RENAME_RUN"  # env var: <receipt-dir id>:<token> inside the API container
STUCK = "snippet process could not be confirmed stopped"
# Runs inside the API container as `python -c KILLER <match> <seconds>` (stdlib only). Finds, in
# /proc, the processes whose environment has SNIPPET_MARKER=<value> with value starting with
# <match>. A <match> ending in ":" is a sweep of one receipt directory's id: processes marked
# by any other directory are "foreign" and, if any exist, nothing is killed and it prints
# "foreign" (exit 2). A sweep ignores read-only `check` snippets (value starting "check:"), and
# a legacy value (no ":", from before the directory id) cannot be attributed: nothing is killed
# and it prints "legacy <pid>,<pid>" (exit 3). Otherwise SIGTERM then SIGKILL; prints "gone"
# (exit 0) or "alive" (1).
KILLER = """\
import os, signal, sys, time
match, wait = sys.argv[1].encode(), float(sys.argv[2])
root = sys.argv[3] if len(sys.argv) > 3 else "/proc"
sweep = match.endswith(b":")
prefix = b"PLANE_RENAME_RUN="
def marked():
    mine, foreign, legacy = [], [], []
    for name in os.listdir(root):
        if not name.isdigit() or int(name) == os.getpid():
            continue
        try:
            with open(root + "/" + name + "/environ", "rb") as handle:
                items = handle.read().split(b"\\0")
        except OSError:
            continue
        for item in items:
            if item.startswith(prefix):
                value = item[len(prefix):]
                if value.startswith(match):
                    mine.append(int(name))
                elif sweep and value.startswith(b"check:"):
                    pass
                else:
                    (foreign if b":" in value else legacy).append(int(name))
                break
    return mine, foreign, legacy
sig = signal.SIGTERM
for _ in range(int(wait * 2) + 1):
    pids, others, old = marked()
    if sweep and old:
        print("legacy " + ",".join(map(str, old)))
        sys.exit(3)
    if sweep and others:
        print("foreign")
        sys.exit(2)
    if not pids:
        print("gone")
        sys.exit(0)
    for pid in pids:
        try:
            os.kill(pid, sig)
        except OSError:
            pass
    sig = signal.SIGKILL
    time.sleep(0.5)
left = marked()[0]
print("alive" if left else "gone")
sys.exit(1 if left else 0)
"""


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
    """Messages (ready and unacknowledged) across the queues of the configured RabbitMQ vhost.

    Returns None when no `celery` queue is listed, so the caller cannot mistake a wrong or empty
    vhost for a drained queue. The vhost is expanded inside the container; argv holds no secret.
    """
    probe = 'rabbitmqctl list_queues -q -p "${RABBITMQ_DEFAULT_VHOST:-/}" name messages'
    try:
        out = docker("exec", mq, "sh", "-c", probe)
    except subprocess.CalledProcessError:
        raise RuntimeError("queue probe failed") from None
    total, celery = 0, False
    for line in out.decode().splitlines():
        fields = line.split()
        if fields and fields[0] == "celery":
            celery = True
        if fields and fields[-1].isdigit():
            total += int(fields[-1])
    return total if celery else None


def drain_queue(mq, docker, clock, sleep, timeout=DRAIN_SECONDS, interval=DRAIN_INTERVAL):
    """Poll until the queue is empty or timeout passes; return jobs left. Never purges.

    Returns None at once when the celery queue is not listed: there is nothing to wait for.
    """
    deadline = clock() + timeout
    remaining = queue_depth(mq, docker)
    while remaining and clock() < deadline:
        sleep(interval)
        remaining = queue_depth(mq, docker)
    return remaining


def worker_running(names, docker):
    """Whether the worker container runs; an unreadable state counts as running."""
    try:
        state = json.loads(docker("inspect", names["worker"]))[0].get("State") or {}
        return bool(state.get("Running"))
    except Exception:  # noqa: BLE001 - when unsure, wait the full drain time
        return True


def stop_services(names, docker, clock, sleep, crash=lambda label: None, allow_undrained=False):
    """Beat, drain, worker, web, space, admin; then restart Live. API, db and Redis stay up.

    Jobs still queued after DRAIN_SECONDS refuse (nothing past beat is stopped yet) unless
    `allow_undrained` is set, which continues and says so. With the worker already stopped
    (a resume or revert after a failure) nothing can drain, so a non-empty queue refuses at once.
    Jobs are never purged."""
    waiting = worker_running(names, docker)
    crash("before_stop_" + names["beat"])
    docker("stop", names["beat"])
    crash("after_stop_" + names["beat"])
    remaining = drain_queue(names["mq"], docker, clock, sleep, DRAIN_SECONDS if waiting else 0)
    if remaining is None:
        say("Warning: celery queue not found; drain not verified")
    elif remaining and not allow_undrained and not waiting:
        raise RuntimeError(
            f"{remaining} jobs still queued and the workers are stopped, so the queue cannot "
            "drain; the jobs are kept (not purged). Set PLANE_RENAME_ALLOW_UNDRAINED=1 to "
            "continue anyway"
        )
    elif remaining and not allow_undrained:
        raise RuntimeError(
            f"{remaining} jobs still queued after {DRAIN_SECONDS} s; not purged. Let the workers "
            "finish them and rerun, or set PLANE_RENAME_ALLOW_UNDRAINED=1 to continue anyway"
        )
    else:
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
    """Start the stopped services in reverse stop order. Live was restarted, not stopped.

    Every service is tried; failures are collected and raised together, by service key."""
    failed = []
    for key in ("admin", "space", "web", "worker", "beat"):
        crash("before_start_" + names[key])
        try:
            docker("start", names[key])
        except Exception:  # noqa: BLE001 - try the rest, then report every failure
            failed.append(key)
        crash("after_start_" + names[key])
    if failed:
        raise RuntimeError("could not start: " + ", ".join(failed))


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


def stderr_tail(handle, limit=300, hide=()):
    """The last few lines of a child's stderr, safe to show: quoted text (names, roles, hosts
    and paths in quotes) and every name in `hide` (container names) is replaced, control
    characters are dropped and the result is capped.

    Best effort: a child could still name a value unquoted, which is why only a short tail is
    kept and nothing the tool read from the environment is ever added."""
    handle.flush()
    handle.seek(0, 2)
    size = handle.tell()
    handle.seek(max(0, size - 4096))
    text = handle.read().decode("utf-8", "replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()][-3:]
    text = " | ".join(lines)
    for name in sorted({h for h in hide if h}, key=len, reverse=True):
        text = re.sub(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", "<container>", text)
    text = re.sub(r"\"[^\"]*\"|'[^']*'", "<redacted>", text)
    return "".join(c for c in text if c.isprintable())[:limit]


def exception_class(handle):
    """The exception class named by the last Python traceback on a child's stderr, or "".

    It is the first unindented `Name:` (or bare `Name`) line after the last `Traceback` line,
    for example `ModuleNotFoundError` or `django.db.utils.OperationalError`. A class name
    carries no record content; the message after it is left to stderr_tail."""
    handle.flush()
    handle.seek(0, 2)
    handle.seek(max(0, handle.tell() - 4096))
    lines = handle.read().decode("utf-8", "replace").splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith("Traceback ")]
    for line in lines[starts[-1] + 1 :] if starts else ():
        found = re.match(r"([A-Za-z_][\w.]*)(?::|$)", line)
        if found:
            return found.group(1)
    return ""


def snippet_cause(handle, hide=()):
    """Why a snippet gave no summary, safe to show: the exception class, then a redacted
    stderr tail (see stderr_tail). Empty when stderr is empty."""
    name = exception_class(handle)
    tail = stderr_tail(handle, hide=hide)
    return "; ".join(part for part in (name, tail and "stderr: " + tail) if part)


def no_summary(op, events):
    """The failure message for an operation that gave no summary, with the exit cause that
    run_snippet recorded in an `exit` event, if any."""
    for event in events:
        if event.get("event") == "exit":
            cause = event.get("cause")
            return (
                f"{op} gave no summary (exited with code {event.get('code')}"
                + (": " + cause if cause else "")
                + ")"
            )
    return op + " gave no summary"


def database_size(db, popen):
    """Size of the Plane database in bytes from the db container, or None if it is unknown."""
    process = None
    try:
        process = popen(
            ["docker", "exec", db, "sh", "-c", DB_SIZE],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        out = process.stdout.read(64)
        if process.wait() != 0:
            return None
        return int(out.decode().strip())
    except Exception:  # noqa: BLE001 - an unknown size falls back to the minimum
        if process is not None:
            process.kill()
            process.wait()
        return None


def check_free_space(directory, db, popen):
    """Refuse when the receipt directory cannot hold a dump of the database.

    The dump is compressed, so the database size is a safe upper bound; with no size, the
    minimum applies. Free space is what an unprivileged writer can use."""
    needed = max(database_size(db, popen) or 0, MIN_FREE_BYTES)
    info = os.statvfs(directory)
    if info.f_bavail * info.f_frsize < needed:
        raise RuntimeError(
            "backup refused: not enough free space in the receipt directory for a database dump"
        )


def validate_dump(db, dump, popen, hide=()):
    """Check the finished dump with `pg_restore -f /dev/null` inside the db container.

    That reads the whole archive, every data block included, and writes it nowhere: it proves
    the dump is complete and readable by the tool that restores it, without touching the
    database. The dump goes in on stdin."""
    with tempfile.TemporaryFile() as errors, open(dump, "rb") as source:
        process = popen(
            ["docker", "exec", "-i", db, "pg_restore", "-f", "/dev/null"],
            stdin=source,
            stdout=subprocess.DEVNULL,
            stderr=errors,
        )
        try:
            code = process.wait()
        except BaseException:
            process.kill()
            process.wait()
            raise
        if code != 0:
            reason = stderr_tail(errors, hide=hide)
            raise RuntimeError(
                "backup refused: pg_restore cannot read the dump"
                + (" (" + reason + ")" if reason else "")
            )


def backup_database(db, directory, popen, crash=lambda label: None, uid=None, hide=()):
    """Stream pg_dump to backup.dump (0600), hashing and fsyncing; record the SHA-256.

    Before the dump the receipt directory must have room for it. After it, `pg_restore -f
    /dev/null` must read the whole file. A failed, empty or unreadable dump raises before any caller changes data,
    leaving no dump file; failures carry a short, redacted stderr tail.
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
    check_free_space(directory, db, popen)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    digest, size, failure, process = hashlib.sha256(), 0, None, None
    try:
        with tempfile.TemporaryFile() as errors:
            process = popen(
                ["docker", "exec", db, "sh", "-c", PG_DUMP],
                stdout=subprocess.PIPE,
                stderr=errors,
            )
            while True:
                chunk = process.stdout.read(1 << 20)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
                os.write(descriptor, chunk)
            if process.wait() != 0:
                reason = stderr_tail(errors, hide=hide)
                failure = "backup failed: pg_dump exited with an error" + (
                    " (" + reason + ")" if reason else ""
                )
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
    try:
        if failure:
            raise RuntimeError(failure)
        validate_dump(db, temporary, popen, hide)
    except BaseException:
        os.unlink(temporary)
        raise
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


# --- T7: the apply state machine -------------------------------------------------------------

SHELL = ("python", "manage.py", "shell")
APPLY_OPS = ("step_slug", "step_rows")  # the only operations that emit journal events
INTERRUPTED = (
    "Rename interrupted ({}). Services may be stopped. The receipt keeps the last completed "
    "step: rerun apply to resume, or run revert (rerun revert if a revert was interrupted). "
    "Both stop services first."
)


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


def is_stuck(reason):
    """Whether a failure reason says a snippet process could not be confirmed stopped."""
    return str(reason).startswith(STUCK)


def run_id(directory):
    """A short id of a receipt directory (hash of its real path), carried in every snippet's
    marker so one run can only ever stop its own directory's snippets."""
    return hashlib.sha256(os.path.realpath(str(directory)).encode()).hexdigest()[:12]


def run_killer(api, match, popen):
    """Run the kill script in the API container for `match` (a full marker value, or a
    directory id ending in ":" to sweep); return "gone", "alive", "foreign", "legacy <pids>"
    or "" (unknown).

    The script's wait is bounded by KILL_WAIT: past it the client is killed and the result is
    unknown."""
    process, timer = None, None
    try:
        process = popen(
            ["docker", "exec", api, "python", "-c", KILLER, match, "15"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        timer = threading.Timer(KILL_WAIT, process.kill)
        timer.daemon = True
        timer.start()
        text = process.stdout.read(256).strip().decode()
        code = process.wait()
        word = text.split(" ")[0]
        return text if code == {"gone": 0, "alive": 1, "foreign": 2, "legacy": 3}.get(word) else ""
    except Exception:  # noqa: BLE001 - an unconfirmed stop is reported, never guessed
        if process is not None:
            process.kill()
        return ""
    finally:
        if timer is not None:
            timer.cancel()


def stop_snippet(api, token, popen):
    """Stop the snippet process with marker value `token` in the API container; return
    whether it is confirmed gone. Any error counts as not confirmed."""
    return run_killer(api, token, popen) == "gone"


def run_snippet(api, snippet, op, args, directory, popen, uid=None, hide=()):
    """Run one snippet operation through `docker exec -i <api> python manage.py shell`.

    Stdin is one request line (`RENAME_REQUEST = ...`) and then the snippet source. Stdout is
    read line by line; non-JSON lines are ignored and never echoed. Each `journal` event is
    appended to the journal (fsynced) before the next line is read, and is never kept or
    printed. Returns (summary or None, other events). A non-zero exit gives no summary.
    Stderr goes to a temporary file. Without a summary, an `exit` event carries the exit code
    and snippet_cause (exception class and redacted stderr tail; `hide` names are masked).

    The process runs with a `PLANE_RENAME_RUN=<receipt-dir id>:<token>` marker in its environment. Killing the
    local `docker exec` client does not stop the Python process in the container, so every
    abort (a deadline of SNIPPET_IDLE_SECONDS without output, an error, an interrupt, a
    non-zero exit) is followed by stop_snippet, and the caller hears of the failure only once
    the process is confirmed gone. If that cannot be confirmed the failure is a STUCK one:
    callers must not restart services or reset the receipt then.
    """
    request = json.dumps({"op": op, "args": args}, sort_keys=True)
    source = "RENAME_REQUEST = __import__('json').loads(" + repr(request) + ")\n" + snippet
    # A check only reads and takes no lock, so its marker is not a directory's: a sweep ignores it.
    token = ("check" if op == "check" else run_id(directory)) + ":" + os.urandom(8).hex()
    with tempfile.TemporaryFile() as errors:
        process = popen(
            ["docker", "exec", "-i", "-e", SNIPPET_MARKER + "=" + token, api, *SHELL],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
        )
        summary, events = read_snippet(process, source, api, op, token, directory, popen, uid)
        if summary is None:
            code = process.wait()
            cause = snippet_cause(errors, hide=(*hide, api))
            if code or cause:
                events.append({"event": "exit", "code": code, "cause": cause})
    return summary, events


def read_snippet(process, source, api, op, token, directory, popen, uid):
    """run_snippet's loop: pipe `source` to the started snippet `process`, then read its
    output; returns (summary, events)."""
    summary, events = None, []
    expired, timer = [], []

    def expire():
        expired.append(True)
        process.kill()

    def arm():
        for old in timer:
            old.cancel()
        timer[:] = [threading.Timer(SNIPPET_IDLE_SECONDS, expire)]
        timer[0].daemon = True
        timer[0].start()

    def disarm():
        for old in timer:
            old.cancel()

    try:
        arm()
        process.stdin.write(source.encode())
        process.stdin.close()
        for raw in process.stdout:
            arm()
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("event") == "journal":
                if op not in APPLY_OPS or not isinstance(event.get("record"), dict):
                    raise RuntimeError("unexpected journal event from " + op + "; refusing")
                append_journal(directory, event["record"], uid)
            elif event.get("event") == "summary":
                summary = event
            elif event.get("event") != "progress":  # progress only re-arms the deadline
                events.append(event)
    except BaseException as failure:
        disarm()
        process.kill()
        process.wait()
        if not stop_snippet(api, token, popen):
            if isinstance(failure, Exception):
                raise RuntimeError(STUCK + " (" + op + ")") from None
            say(
                "A snippet may still be running in the API container (stop could not be "
                "confirmed); stop it by hand before rerunning.",
                sys.stderr,
            )
        raise
    disarm()
    code = process.wait()
    if expired or code != 0:
        if not stop_snippet(api, token, popen):
            raise RuntimeError(STUCK + " (" + op + ")")
        if expired:
            raise RuntimeError(
                f"{op} timed out after {SNIPPET_IDLE_SECONDS:g} s without output; the snippet "
                "process was stopped in the API container"
            )
        summary = None
    return summary, events


def report(op, summary, events):
    """Tell the operator every refusal, error, skip, unfixed hit and health result, by id only."""
    planned = 0
    for event in events:
        kind = event.get("event")
        where = " ".join(str(event[k]) for k in ("table", "id") if k in event)
        if kind == "plan":
            planned += 1
        elif kind == "refuse":
            say("Refused: " + str(event.get("reason")))
        elif kind == "error":
            detail = " (" + str(event["detail"]) + ")" if "detail" in event else ""
            say("Error: " + str(event.get("reason")) + (" at " + where if where else "") + detail)
        elif kind == "mismatch":
            name = str(event.get("model")) + (
                "." + str(event["field"]) if event.get("field") else ""
            )
            found = (
                f", expected {event.get('expected')}, found {event.get('found')}"
                if "expected" in event
                else ""
            )
            say(f"Model mismatch: {name} ({event.get('reason')}{found})")
        elif kind == "skip":
            say("Skipped " + where + " (" + str(event.get("reason")) + ")")
        elif kind == "unfixed":
            say(
                f"Not rewritten: {event.get('count')} in {event.get('table')}."
                f"{event.get('column')} ({event.get('reason')})"
            )
        elif kind == "health":
            say(
                "Health " + str(event.get("check")) + ": " + ("ok" if event.get("ok") else "FAILED")
            )
    if summary is None:
        say(op + ": no summary from the snippet")
        return
    if op == "check" and summary.get("ok"):
        say(
            f"check: {summary.get('planned', planned)} values planned, "
            f"{summary.get('rebuild', 0)} editor rebuilds, {summary.get('unfixed', 0)} not rewritten"
        )
    for key in ("rewritten", "not_rebuilt", "restored", "already_restored", "skipped"):
        if key in summary:
            say(f"{op}: {key} {summary[key]}")
    if summary.get("already"):
        say(op + ": already done")
    if "sessions_in_cache" in summary:
        cleared = "cleared" if summary.get("cleared") else "not cleared (dry run)"
        say(f"Cache {cleared}; sessions kept in the cache: {summary['sessions_in_cache']}")
    if "session_cache_alias" in summary:
        say("Sessions use cache alias " + summary["session_cache_alias"])
    if summary.get("partial"):
        say(f"Partial restore: {summary.get('skipped', 0)} rows skipped; this is not a full revert")


def snippet_ok(api, snippet, op, args, directory, popen, uid=None, hide=()):
    """run_snippet and report; return the summary. A missing summary or ok:false raises."""
    summary, events = run_snippet(api, snippet, op, args, directory, popen, uid, hide)
    report(op, summary, events)
    if summary is None:
        raise RuntimeError(no_summary(op, events))
    if summary.get("ok") is not True:
        raise RuntimeError(op + " failed")
    return summary


def inspect_services(names, api, docker):
    """Refuse unless every service container exists; return {service: container id}.

    Messages name the service, not the host."""
    ids = {}
    for key, name in sorted({**names, "api": api}.items()):
        try:
            ids[key] = json.loads(docker("inspect", name))[0].get("Id")
        except subprocess.CalledProcessError:
            raise RuntimeError("container not found for service " + key) from None
    return ids


def refuse_api_service(names, api, ids=None):
    """Refuse when the API container is also one of the service containers.

    By name, and by container id when `ids` (from inspect_services) is given: one container can
    answer to two names (a name and an id, say).

    Stopping (or restarting) a service would then take the API container down with it, and the
    snippet runs in the API container. The names come from PLANE_*_CONTAINER, so a wrong
    override can cause it."""
    same_id = ids and ids.get("api") and any(ids.get("api") == ids[key] for key in names)
    if api in names.values() or same_id:
        raise RuntimeError(
            "the API container is also a service container; give the API container and each "
            "service its own name (PLANE_API_CONTAINER, PLANE_*_CONTAINER)"
        )


def sweep_orphans(api, popen, operation, directory):
    """Before apply or revert, stop snippets of THIS receipt directory left by a killed run.

    The run lock (per receipt directory) means no other apply or revert on this directory is
    live, so its marked processes are orphans. A marked process of a different directory is a
    live run elsewhere on this instance: refuse, never kill it. `check` takes no lock and never
    sweeps (it could be a live run's snippet). A sweep ignores `check` snippets (read-only) and
    refuses, naming the pids, on an old-format marker with no directory id."""
    if operation not in ("apply", "revert"):
        return
    word = run_killer(api, run_id(directory) + ":", popen)
    if word == "foreign":
        raise RuntimeError(
            "another rename run is active on this instance (a snippet marked by a different "
            "receipt directory is running in the API container)"
        )
    if word.startswith("legacy "):
        pids = word.split(" ", 1)[1]
        raise RuntimeError(
            "an older-format snippet process is running in the API container (pid " + pids + "); "
            "it cannot be tied to a receipt directory. If no rename is running, stop it with "
            f"`docker exec {api} kill {pids.replace(',', ' ')}` and rerun"
        )
    if word != "gone":
        raise RuntimeError(
            "could not confirm that no earlier snippet is running in the API container"
        )


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


def slug_state(api, snippet, old, new, workspace_id, directory, popen, uid=None):
    """Read-only probe of the slug after an unclear step_slug: "renamed", "original" or
    "unknown".

    With the journalled `workspace_id`, health reports whether NEW is held by exactly that
    workspace and OLD by none ("renamed"), and, with the slugs swapped, whether OLD is held by
    it and NEW by none ("original"). Without an id, a passing `check` (OLD live, NEW free) is
    "original". Health's API check is ignored here; only the slug checks count."""

    def held(first, second):
        args = {"old": first, "new": second, "workspace_id": workspace_id}
        summary, events = run_snippet(api, snippet, "health", args, directory, popen, uid)
        checks = {e.get("check"): e.get("ok") for e in events if e.get("event") == "health"}
        return summary is not None and checks.get("new_present") and checks.get("old_absent")

    try:
        if workspace_id:
            if held(old, new):
                return "renamed"
            return "original" if held(new, old) else "unknown"
        summary, _ = run_snippet(api, snippet, "check", {"old": old, "new": new}, directory, popen)
        return "original" if summary is not None and summary.get("ok") is True else "unknown"
    except Exception:  # noqa: BLE001 - a failed probe is an unknown state, never a guess
        return "unknown"


def seam_uid(seams):
    """The uid that must own the receipt files: the `uid` seam, else this process's euid."""
    return os.geteuid() if seams.get("uid") is None else seams["uid"]


def run_directory(data):
    """The receipt directory of a payload: `receipt_dir`, else the environment default."""
    return pathlib.Path(data.get("receipt_dir") or receipt_dir(os.environ))


def reason_of(failure):
    """The operator text of a failure: a RuntimeError's message, else only its type name."""
    return str(failure) if isinstance(failure, RuntimeError) else type(failure).__name__


def refused(failure):
    """A preflight RuntimeError as a refusal: its message, prefixed `Refused: ` once."""
    text = str(failure)
    return RuntimeError(text if text.startswith("Refused") else "Refused: " + text)


def warnings_text(notes):
    """The ` Warning: <note>.` tail of a failure message, one per note."""
    return "".join(" Warning: " + note + "." for note in notes)


def drain_state(reason):
    """The service state after a failed drain gate, or None when `reason` is not one."""
    if "and the workers are stopped" in reason:  # fast-fail drain gate
        return "Services were left as they are (workers stopped, jobs kept)"
    if "jobs still queued after" in reason:  # 60 s drain timeout, worker was running
        return "Beat is stopped and the other services were left as they are (jobs kept)"
    return None


def run_context(data, seams, operations=None):
    """What check, apply and revert share for one run, as a dict: the seams (docker, popen,
    clock, crash, uid, and seams itself for the lazily read sleep) and the payload (operation,
    old, new, api, snippet, directory, names, options, undrained).

    run_apply and run_revert each build one, use the run_* helpers below for the snippet call,
    the service stop and start and the step machine, and keep only their own steps and failure
    messages. Seams are read before the operation check, and the payload after it. (A dict and
    plain functions, not a class: program() ships function source only.)"""
    run = {
        "seams": seams,
        "docker": seams["docker"],
        "popen": seams["popen"],
        "clock": seams["clock"],
        "crash": seams.get("crash") or (lambda label: None),
        "uid": seam_uid(seams),
        "operation": data.get("operation"),
    }
    if operations is not None and run["operation"] not in operations:
        raise RuntimeError("expected check, apply or revert")
    run["old"], run["new"] = data["from"], data["to"]
    run["api"], run["snippet"] = data["api"], data["snippet"]
    run["directory"] = run_directory(data)
    run["names"] = service_names(data.get("services") or {})
    run["options"] = {key: data[key] for key in ("live_url", "api_url") if data.get(key)}
    run["undrained"] = data.get("allow_undrained") is True
    return run


def run_call(run, op, args):
    """Run one snippet operation; return its summary. No summary or ok:false raises."""
    return snippet_ok(
        run["api"],
        run["snippet"],
        op,
        args,
        run["directory"],
        run["popen"],
        run["uid"],
        run["names"].values(),
    )


def run_health(run, old, new, workspace_id):
    """Health with `old` gone and `new` held by `workspace_id` (and the payload URLs)."""
    args = {"old": old, "new": new, "workspace_id": workspace_id, **run["options"]}
    return run_call(run, "health", args)


def run_slug_state(run, workspace_id):
    """slug_state (a read-only probe) for this run's rename."""
    return slug_state(
        run["api"],
        run["snippet"],
        run["old"],
        run["new"],
        workspace_id,
        run["directory"],
        run["popen"],
        run["uid"],
    )


def run_preflight(run, operation):
    """The read-only checks every operation starts with; `operation` picks the sweep."""
    refuse_api_service(run["names"], run["api"])
    ids = inspect_services(run["names"], run["api"], run["docker"])
    refuse_api_service(run["names"], run["api"], ids)
    sweep_orphans(run["api"], run["popen"], operation, run["directory"])


def run_stop(run):
    """Drain and stop the services; an unverified drain raises."""
    sleep = run["seams"]["sleep"]
    remaining = stop_services(
        run["names"], run["docker"], run["clock"], sleep, run["crash"], run["undrained"]
    )
    if remaining is None:  # wrong or empty vhost: the drain was never verified
        raise RuntimeError("celery queue not found; drain not verified")


def run_start(run):
    start_services(run["names"], run["docker"], run["crash"])


def run_start_noting(run, notes):
    """Start the services; a failure is added to `notes` instead of raised."""
    try:
        run_start(run)
    except Exception as problem:  # noqa: BLE001 - report it with the original failure
        notes.append(reason_of(problem) + "; start the remaining services by hand")


def run_steps(run, order, step, effect, recover, settle=None):
    """Run every step after `step` (None: from the first) up to the last of `order`.

    Each step is crash("before_<step>"), effect, crash("after_<step>"), settle (if any), then
    advance. An Exception goes to recover(step, target, failure), with `step` the last step
    reached; it raises, or returns once it has advanced the receipt itself. Killed and other
    BaseExceptions propagate untouched."""
    crash = run["crash"]
    while step != order[-1]:
        target = order[0] if step is None else order[order.index(step) + 1]
        crash("before_" + target)
        try:
            effect(target)
            crash("after_" + target)
            if settle is not None:
                settle()
            advance(run["directory"], target, run["uid"])
        except Exception as failure:
            recover(step, target, failure)
        step = target


def run_apply(data, seams):
    """Run `check` (read-only), `apply` or `revert` (both resumable) for one workspace rename.

    data: operation, from, to, api (container), snippet (source), optional receipt_dir,
    services (PLANE_*_CONTAINER overrides), live_url and api_url. seams: docker, popen, clock,
    sleep, optional crash(label) and uid. Returns the operator message; raises RuntimeError.

    Apply writes the pending receipt first, then runs each step as effect, then advance:
    pending, stopped, backed_up, slug_done, rows_done, cache_done, started, complete. Every
    refusal comes before the first stop. A resume between stopped and cache_done stops the
    services again first (idempotent). A failure before the slug change restarts the services
    and resets the receipt to pending with the backup and journal rotated, so the next apply
    stops again and takes a fresh dump; a kill keeps the receipt, and its matching backup is
    reused unless a service ran again before the slug change (then it is rotated too). A
    failure to stop again on resume after a possible slug commit is probed, never reset. An
    unclear step_slug is probed read-only: renamed continues, original is a pre-slug failure,
    anything else stops with services down. After the slug change, a failure stops with
    services down for the operator to resume (apply) or revert. There is no automatic rollback.

    Known risk: a kill of this process between the snippet's print of a journal line and the
    host's fsync of it can leave a written row without a journal entry; only the pg_dump backup
    recovers that row. Closing it needs an acknowledgement channel, which this does not have.
    """
    run = run_context(data, seams, ("check", "apply"))
    operation, old, new = run["operation"], run["old"], run["new"]
    directory, uid, names = run["directory"], run["uid"], run["names"]
    docker, clock, crash = run["docker"], run["clock"], run["crash"]
    order = step_order("apply")

    def at_or_past(step, mark):
        return step in order and order.index(step) >= order.index(mark)

    # Preflight: read-only, and it writes nothing (not even the receipt directory) for check.
    try:
        run_preflight(run, operation)
        receipt = None
        if operation == "apply" or os.path.lexists(directory):
            receipt = load_receipt(directory, old, new, uid)
        if receipt is not None and receipt.get("phase") != "apply":
            if receipt.get("step") == "reverted":
                raise RuntimeError(
                    "this rename was reverted and the revert is complete; archive or move the "
                    "receipt directory before a new apply"
                )
            raise RuntimeError("receipt is in the revert phase; run revert")
        if receipt is None and os.path.lexists(directory):
            leftover = leftover_files(directory)
            if leftover:
                raise RuntimeError(
                    "found " + ", ".join(leftover) + " without a receipt: a journal or backup "
                    "from an earlier rename; move them or the receipt directory aside before a "
                    "new apply"
                )
        step = receipt["step"] if receipt else None
        records = load_journal(directory, uid) if receipt else []
        workspace_id = journal_workspace(records)
        if step == "complete":
            if workspace_id is None:
                raise RuntimeError("journal has no workspace record; refusing")
            run_health(run, old, new, workspace_id)
            return (
                "Rename already complete and healthy. To run another rename (including the "
                "opposite one), archive the receipt directory first: move it aside whole, "
                "keeping backup.dump, records.jsonl and any rotated files; never delete them"
            )
        backup_action = verify_backup(directory, receipt, uid)
        if workspace_id is None and at_or_past(step, "slug_done"):
            raise RuntimeError("journal has no workspace record; refusing")
        # The full check runs unless the slug step is done: at or past slug_done, or at
        # backed_up when a probe shows NEW held by the journalled workspace (kill after commit).
        slug_committed = at_or_past(step, "slug_done") or (
            step == "backed_up"
            and workspace_id is not None
            and run_slug_state(run, workspace_id) == "renamed"
        )
        if not slug_committed:
            run_call(run, "check", {"old": old, "new": new})
        run_call(run, "flush_cache", {"dry_run": True})
        detect_saltbox(names, docker)
    except RuntimeError as failure:
        raise refused(failure) from None
    if operation == "check":
        if step:
            say("A receipt for this rename is at step " + step + "; apply resumes from there")
        return "Check passed: apply may run"

    receipt = new_receipt(directory, old, new, uid)  # pending receipt first
    step = receipt["step"]
    if step != "pending":
        say("Resuming the rename from step " + step)

    def effect(target):
        nonlocal workspace_id
        if target == "stopped":
            run_stop(run)
        elif target == "backed_up":
            dump = directory / "backup.dump"
            if backup_action == "adopt" and os.path.lexists(dump):
                set_receipt_field(directory, "backup_sha256", sha256_file(dump), uid)
                say("Adopted backup.dump with no recorded SHA-256 (crash after the dump)")
            elif os.path.lexists(dump):
                say("Backup already taken; SHA-256 matches")
            else:
                backup_database(
                    names["db"],
                    directory,
                    run["popen"],
                    crash,
                    uid,
                    hide=(*names.values(), run["api"]),
                )
                say("Database backed up")
        elif target == "slug_done":
            args = {"old": old, "new": new}
            if workspace_id:
                args["workspace_id"] = workspace_id
            summary = run_call(run, "step_slug", args)
            renamed = summary.get("workspace_id")
            journalled = journal_workspace(load_journal(directory, uid))
            if not renamed or journalled != renamed or workspace_id not in (None, renamed):
                raise RuntimeError("slug step workspace does not match the journal")
            workspace_id = renamed
        elif target == "rows_done":
            args = {"old": old, "new": new, "workspace_id": workspace_id, **run["options"]}
            run_call(run, "step_rows", args)
        elif target == "cache_done":
            run_call(run, "flush_cache", {})
        elif target == "started":
            run_start(run)
        elif target == "complete":
            run_health(run, old, new, workspace_id)

    def pre_slug_failure(target, reason):
        """Restart services and reset the receipt; never returns."""
        notes = []
        run_start_noting(run, notes)
        try:
            reset_receipt(directory, clock, uid, crash)
        except Exception as problem:  # noqa: BLE001
            notes.append("receipt reset failed: " + reason_of(problem))
        restarted = "Services restarted" if not notes else "Services not all restarted"
        raise RuntimeError(
            f"Rename failed before the slug change ({target}): {reason}. {restarted}; no data "
            "changed. The receipt is back at pending and the backup was set aside, so the next "
            "apply stops services and takes a fresh backup. Fix the cause and rerun apply."
            + warnings_text(notes)
        )

    def snippet_stuck(at, reason):
        raise RuntimeError(
            f"Rename stopped at {at}: {reason}. Services were not restarted and the receipt was "
            "not reset. Stop the snippet process in the API container by hand (its environment "
            "has PLANE_RENAME_RUN), then rerun apply to resume or run revert."
        )

    def post_slug_failure(step, target, reason):
        if target == "complete":
            raise RuntimeError(
                f"Rename health check failed: {reason}. Receipt left at started; services "
                "are running. Investigate, then rerun apply to recheck, or run revert."
            )
        if target == "started":
            state = "Some services did not start"
        elif drain_state(reason) is not None:
            state = drain_state(reason)
        elif target == step:  # stopping services again on resume failed
            state = "Some services may still be running; stop them by hand"
        else:
            state = "Services stay stopped"
        moving = "" if target == step else f" (moving to {target})"
        raise RuntimeError(
            f"Rename stopped at {step}{moving}: {reason}. {state}. Rerun apply to resume, "
            "or run revert; nothing is rolled back automatically."
        )

    def any_running():
        for key in ("beat", "worker", "web", "space", "admin"):
            state = json.loads(docker("inspect", names[key]))[0].get("State") or {}
            if state.get("Running"):
                return True
        return False

    try:
        if backup_action == "rotate":
            finish_rotation(directory, uid)
        if at_or_past(step, "stopped") and not at_or_past(step, "started"):
            # A service running again since the kill may have changed data after the backup.
            if not at_or_past(step, "slug_done") and not slug_committed and any_running():
                say("Services ran again since the kill; setting the backup aside for a fresh one")
                reset_receipt(directory, clock, uid, crash)
                step, workspace_id, backup_action = "pending", None, None
            else:
                say("Making sure services are stopped before resuming")
                run_stop(run)
    except Exception as failure:  # noqa: BLE001
        reason = reason_of(failure)
        if is_stuck(reason):
            snippet_stuck(step, reason)
        if at_or_past(step, "slug_done"):
            post_slug_failure(step, step, reason)
        if workspace_id is not None:  # a slug step may have committed: never guess
            state = run_slug_state(run, workspace_id)
            if state == "renamed":
                post_slug_failure(step, step, reason)
            if state != "original":
                raise RuntimeError(
                    f"Rename resume failed at {step}: {reason}; slug state unknown. Services "
                    "were left as they are and nothing was reset. Check which workspace holds "
                    "each slug and which services run, then rerun apply or run revert."
                ) from None
        pre_slug_failure(step, reason)

    def recover(step, target, failure):
        nonlocal workspace_id
        reason = reason_of(failure)
        if is_stuck(reason):
            snippet_stuck(step, reason)
        if target in ("stopped", "backed_up"):
            pre_slug_failure(target, reason)
        if target != "slug_done":
            post_slug_failure(step, target, reason)
        # step_slug's outcome is unclear (no summary, non-zero exit, mismatch, or a
        # failed advance): probe it read-only before deciding anything.
        journalled = journal_workspace(load_journal(directory, uid))
        state = run_slug_state(run, journalled)
        if state == "original":
            pre_slug_failure(target, reason)
        if state != "renamed":
            raise RuntimeError(
                f"Rename slug step outcome unknown: {reason}. Services stay stopped; the "
                "receipt is at backed_up. Check which workspace holds each slug, then rerun "
                "apply or run revert."
            ) from None
        say("Slug step unclear, but NEW is held by the journalled workspace; continuing")
        workspace_id = journalled
        advance(directory, target, uid)

    run_steps(run, order, step, effect, recover)
    return f"Rename complete: {old} is now {new}"


# --- T8: revert from the receipt -------------------------------------------------------------


def run_revert(data, seams):
    """Revert one workspace rename from its receipt and journal; resumable like apply.

    Same payload and seams as deploy. Steps (effect, then advance): reverting (the receipt
    marker, with `reverted_from` naming the apply step), restored (stop services, restore from
    the current records.jsonl only, flush the cache), started, reverted (health with the slugs
    swapped and the journalled `workspace_id`). Every refusal comes before the first stop, and
    the restore runs only with services stopped. The restore is skipped when nothing was
    journalled, or when slug_state shows the slug change never committed and no restore was
    started. A partial restore restarts services but leaves the receipt at reverting, so a
    rerun stops services and restores again; it is never reported as complete. The backup and
    the journal are never deleted, and the pg_dump backup is never restored here: that is a
    manual last resort.
    """
    run = run_context(data, seams)
    old, new, directory = run["old"], run["new"], run["directory"]
    uid, crash = run["uid"], run["crash"]
    applied, order = step_order("apply"), step_order("revert")
    partial, finished, notes = None, False, []

    # Preflight: read-only. It never creates the receipt directory.
    try:
        run_preflight(run, "revert")
        if not os.path.lexists(directory / "receipt.json"):
            raise RuntimeError("no receipt for this rename; nothing to revert")
        receipt = load_receipt(directory, old, new, uid)
        phase, step = receipt.get("phase"), receipt.get("step")
        if phase not in ("apply", "revert") or step not in step_order(phase):
            raise RuntimeError("receipt state is unknown")
        if step == "reverted":
            return "Revert already complete"
        records = load_journal(directory, uid)  # the current journal only, never a rotated one
        workspace_id = journal_workspace(records)
        if records and workspace_id is None:
            raise RuntimeError("journal has no workspace record")
        origin = step if phase == "apply" else receipt.get("reverted_from")
        committed = origin in applied and applied.index(origin) >= applied.index("slug_done")
        restore_needed = False
        if phase == "apply" or step == "reverting":
            if not records and committed:
                raise RuntimeError(
                    "journal is empty but the receipt is past the slug change; restore "
                    "backup.dump by hand as the last resort"
                )
            if records:
                state = run_slug_state(run, workspace_id)
                if state == "unknown":
                    raise RuntimeError(
                        "slug state unknown: the journalled workspace holds neither slug as "
                        "expected; check which workspace holds each slug"
                    )
                restore_needed = (
                    state == "renamed" or committed or bool(receipt.get("restore_started"))
                )
                if not restore_needed:
                    say("The slug change never committed; no restore needed")
            else:
                if run_slug_state(run, None) != "original":
                    raise RuntimeError(
                        "slug state unknown: nothing was journalled, but OLD is not live or NEW "
                        "is taken; check which workspace holds each slug, and restore "
                        "backup.dump by hand as the last resort"
                    )
                say("Nothing was journalled; no restore needed")
    except RuntimeError as failure:
        raise refused(failure) from None

    if phase == "revert":
        say("Resuming the revert from step " + step)

    def restore():
        nonlocal partial
        set_receipt_field(directory, "restore_started", True, uid)
        crash("before_restore")
        args = {"old": old, "new": new, "journal": records, "workspace_id": workspace_id}
        summary, events = run_snippet(
            run["api"],
            run["snippet"],
            "restore",
            args,
            directory,
            run["popen"],
            uid,
            run["names"].values(),
        )
        report("restore", summary, events)
        crash("after_restore")
        if summary is None:
            raise RuntimeError(no_summary("restore", events))
        skipped = summary.get("skipped", 0) if summary.get("partial") else 0
        if not skipped and summary.get("ok") is not True:
            raise RuntimeError("restore failed")
        try:
            set_receipt_field(directory, "restore_partial", skipped, uid)
        except Exception as problem:  # noqa: BLE001 - a partial restore must still finish
            if not skipped:
                raise
            notes.append("could not record restore_partial in the receipt: " + reason_of(problem))
        partial = skipped or None

    def verify():
        if workspace_id:
            run_health(run, new, old, workspace_id)
        else:  # nothing journalled: OLD must be live and NEW free
            run_call(run, "check", {"old": old, "new": new})

    def effect(target):
        if target == "reverting":
            set_receipt_field(directory, "reverted_from", origin, uid)
        elif target == "restored":
            say("Stopping services for the restore")
            run_stop(run)
            if restore_needed:
                restore()
            run_call(run, "flush_cache", {})
        elif target == "started":
            run_start(run)
        elif target == "reverted":
            verify()

    def finish_partial():
        """After a partial restore: restart services and recheck; never returns then."""
        nonlocal finished
        if not partial:
            return
        run_start_noting(run, notes)
        try:
            verify()
        except Exception as problem:  # noqa: BLE001
            notes.append("health check failed: " + reason_of(problem))
        finished = True
        raise RuntimeError(
            f"Revert incomplete: the restore was partial ({partial} rows skipped, listed above "
            "by id); this is not a full revert. The slug and every other journalled row are "
            "restored and services were started. The skipped rows changed after the rename and "
            "were not overwritten. Review them, then rerun revert to recheck (it stops services "
            "first). Restoring backup.dump by hand is the last resort and loses later edits."
            + warnings_text(notes)
        )

    def failed(target, reason):
        if is_stuck(reason):
            raise RuntimeError(
                f"Revert stopped at {target}: {reason}. Services were not restarted and the "
                "receipt was not changed. Stop the snippet process in the API container by hand "
                "(its environment has PLANE_RENAME_RUN), then rerun revert."
            )
        if drain_state(reason) is not None:
            state = drain_state(reason)
        elif target == "reverting":
            state = "Nothing was changed by the revert; services were left as they are"
        elif target == "restored":
            state = (
                "Services stay stopped (some may still run if stopping failed). The restore is "
                "all-or-nothing and safe to repeat"
            )
        elif target == "started":
            state = "The data is restored, but some services did not start"
        else:
            state = "Services are running; the receipt is left at started"
        raise RuntimeError(
            f"Revert stopped at {target}: {reason}. {state}. Fix the cause and rerun revert. "
            "The journal and backup.dump are kept; restoring backup.dump by hand is the last "
            "resort."
        )

    def recover(step, target, failure):
        if finished:  # the report from finish_partial
            raise failure
        failed(target, reason_of(failure))

    run_steps(run, order, None if phase == "apply" else step, effect, recover, finish_partial)
    return f"Revert complete: {new} is back to {old}"


LOCK_FILE = "run.lock"


def lock_run(directory, uid, create):
    """Take the exclusive run lock of a receipt directory; return its descriptor.

    Apply and revert hold it for the whole run, so a second run (a double start, or a dropped
    SSH session whose remote program keeps going) refuses instead of driving a second state
    machine over one receipt. It is `flock` on `run.lock` (0600, same private-file checks as the
    receipt), released by closing the descriptor or when the process dies. The lock belongs to
    this receipt directory, which must be on a local filesystem (not NFS), and the directory
    must never be archived while it is held. A revert (`create` False) with no directory, or
    none holding a receipt once locked, refuses at once with no docker call: otherwise an apply
    could start between the lookup and the preflight and run beside it."""
    directory = pathlib.Path(directory)
    if create:
        secure_receipt_dir(directory, uid)
    elif not os.path.lexists(directory):
        raise RuntimeError("Refused: no receipt for this rename; nothing to revert")
    else:
        secure_receipt_dir(directory, uid)
    path = directory / LOCK_FILE
    if os.path.lexists(path):
        private_file_check(path, uid, False)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        private_file_check(path, uid, False)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(
                "Refused: another apply or revert holds the lock on the receipt directory; "
                "wait for it to finish, or check the host for a run still going"
            ) from None
        if not create and not os.path.lexists(directory / "receipt.json"):
            raise RuntimeError("Refused: no receipt for this rename; nothing to revert")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def run_locked(data, seams, run, create, lock=True):
    """Run `run(data, seams)` under the run lock of the receipt directory (none for check).

    A valid `snippet_idle_seconds` in the payload replaces SNIPPET_IDLE_SECONDS for the run."""
    global SNIPPET_IDLE_SECONDS
    descriptor = lock_run(run_directory(data), seam_uid(seams), create) if lock else None
    idle, previous = data.get("snippet_idle_seconds"), SNIPPET_IDLE_SECONDS
    if isinstance(idle, (int, float)) and not isinstance(idle, bool) and 0 < idle <= 86400:
        SNIPPET_IDLE_SECONDS = idle
    try:
        return run(data, seams)
    finally:
        SNIPPET_IDLE_SECONDS = previous
        if descriptor is not None:
            os.close(descriptor)


def deploy(data, seams):
    """Run `check` (read-only), `apply` or `revert` (both resumable) for one workspace rename.

    `check` takes no lock and writes nothing. `apply` and `revert` first take the run lock and
    refuse at once if another run holds it; see run_apply and run_revert for the steps."""
    operation = data.get("operation")
    if operation == "revert":
        return revert(data, seams)
    if operation == "apply":
        return run_locked(data, seams, run_apply, True)
    return run_locked(data, seams, run_apply, False, lock=False)


def revert(data, seams):
    """Revert one workspace rename under the run lock; see run_revert."""
    return run_locked(data, seams, run_revert, False)


def host_seams():
    return {"docker": docker, "popen": subprocess.Popen, "clock": time.time, "sleep": time.sleep}


def snippet_source():
    """The Django snippet shipped in the payload: the matcher, then the backend (owner side)."""
    return (
        inspect.getsource(workspace_rename_match)
        + "\n"
        + inspect.getsource(workspace_rename_backend)
    )


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
    service_names,
    queue_depth,
    drain_queue,
    stop_services,
    start_services,
    detect_saltbox,
    set_receipt_field,
    stderr_tail,
    exception_class,
    snippet_cause,
    no_summary,
    database_size,
    check_free_space,
    validate_dump,
    backup_database,
    receipt_dir,
    repair_journal_tail,
    append_journal,
    load_journal,
    sha256_file,
    leftover_files,
    journal_workspace,
    worker_running,
    run_id,
    run_killer,
    is_stuck,
    stop_snippet,
    run_snippet,
    read_snippet,
    report,
    snippet_ok,
    inspect_services,
    refuse_api_service,
    sweep_orphans,
    read_receipt,
    finish_rotation,
    reset_receipt,
    verify_backup,
    slug_state,
    seam_uid,
    run_directory,
    reason_of,
    refused,
    warnings_text,
    drain_state,
    run_context,
    run_call,
    run_health,
    run_slug_state,
    run_preflight,
    run_stop,
    run_start,
    run_start_noting,
    run_steps,
    run_revert,
    revert,
    run_apply,
    lock_run,
    run_locked,
    deploy,
    host_seams,
)
SHIPPED_CONSTANTS = {
    "PG_DUMP": PG_DUMP,
    "DB_SIZE": DB_SIZE,
    "MIN_FREE_BYTES": MIN_FREE_BYTES,
    "SNIPPET_IDLE_SECONDS": SNIPPET_IDLE_SECONDS,
    "KILL_WAIT": KILL_WAIT,
    "SNIPPET_MARKER": SNIPPET_MARKER,
    "STUCK": STUCK,
    "KILLER": KILLER,
    "SALTBOX_LABEL": SALTBOX_LABEL,
    "DRAIN_SECONDS": DRAIN_SECONDS,
    "DRAIN_INTERVAL": DRAIN_INTERVAL,
    "SHELL": SHELL,
    "APPLY_OPS": APPLY_OPS,
    "INTERRUPTED": INTERRUPTED,
    "LOCK_FILE": LOCK_FILE,
}
IMPORTS = {
    "fcntl",
    "hashlib",
    "json",
    "os",
    "pathlib",
    "re",
    "stat",
    "subprocess",
    "tempfile",
    "threading",
    "time",
}
FUNCTIONS = (docker, *SHIPPED)


def entry(seams="host_seams()"):
    """Entry stub: the payload is JSON on stdin. A RuntimeError exits 1 with its message only;
    an OSError, Ctrl-C or SIGTERM exits 1 with restart-or-resume guidance, never a traceback."""
    return (
        "def interrupted(signum, frame):\n"
        "    raise KeyboardInterrupt\n"
        "signal.signal(signal.SIGTERM, interrupted)\n"
        "try:\n"
        f"    say(deploy(json.load(sys.stdin), {seams}))\n"
        "except RuntimeError as failure:\n"
        "    say(str(failure), sys.stderr)\n"
        "    sys.exit(1)\n"
        "except (OSError, KeyboardInterrupt) as failure:\n"
        "    say(INTERRUPTED.format(type(failure).__name__), sys.stderr)\n"
        "    sys.exit(1)\n"
    )


# The host program. main() sends it over SSH with the payload (snippet included) on stdin.
REMOTE = program(IMPORTS, FUNCTIONS, entry(), SHIPPED_CONSTANTS)

# Owner side only (not shipped): the CLI that sends REMOTE and the payload over SSH.
SERVICE_VARIABLES = (
    "PLANE_DB_CONTAINER",
    "PLANE_REDIS_CONTAINER",
    "PLANE_WEB_CONTAINER",
    "PLANE_SPACE_CONTAINER",
    "PLANE_ADMIN_CONTAINER",
    "PLANE_WORKER_CONTAINER",
    "PLANE_BEAT_CONTAINER",
    "PLANE_LIVE_CONTAINER",
    "PLANE_MQ_CONTAINER",
)
LOCAL_INTERRUPTED = (
    "Interrupted locally. The host program may still be running or have stopped part way. "
    "Check the receipt on the host, then rerun the same task to resume or run the revert task."
)


def container_name_ok(name):
    return (
        bool(name) and not name.startswith("-") and name.replace("-", "").replace("_", "").isalnum()
    )


def main(argv=None):
    """Owner CLI: check, apply or revert. Values come from the environment, are validated
    before SSH and are never printed. The payload (snippet included) goes on stdin; the remote
    exit code becomes ours."""
    from plane_toolchain.pages_deploy import valid_live_url
    from plane_toolchain.remote import require_env, run_remote

    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or argv[0] not in ("check", "apply", "revert"):
        sys.exit("Usage: workspace_rename check|apply|revert")
    operation = argv[0]
    old, new, host, api, live_url = require_env(
        "PLANE_RENAME_FROM",
        "PLANE_RENAME_TO",
        "PLANE_MCP_SSH",
        "PLANE_API_CONTAINER",
        "PLANE_LIVE_URL",
    )
    for name, slug in (("PLANE_RENAME_FROM", old), ("PLANE_RENAME_TO", new)):
        if not workspace_rename_match.SLUG_RE.match(slug):
            sys.exit("Invalid " + name)
    if old == new:
        sys.exit("PLANE_RENAME_FROM and PLANE_RENAME_TO must differ")
    if host.startswith("-") or not container_name_ok(api):
        sys.exit("Invalid deployment destination")
    if not valid_live_url(live_url):
        sys.exit("Invalid PLANE_LIVE_URL")
    api_url = os.environ.get("PLANE_API_URL") or None
    if api_url and not valid_live_url(api_url):
        sys.exit("Invalid PLANE_API_URL")
    services = {}
    for variable in SERVICE_VARIABLES:
        value = os.environ.get(variable)
        if value and not container_name_ok(value):
            sys.exit("Invalid " + variable)
        if value:
            services[variable] = value
    if api in service_names(services).values():
        sys.exit("The API container must differ from every service container")
    undrained = os.environ.get("PLANE_RENAME_ALLOW_UNDRAINED") or None
    if undrained not in (None, "1"):
        sys.exit("Invalid PLANE_RENAME_ALLOW_UNDRAINED (use 1)")
    idle = os.environ.get("PLANE_RENAME_SNIPPET_IDLE_SECONDS") or None
    if idle is not None:
        try:
            idle = float(idle)
        except ValueError:
            idle = 0
        if not 0 < idle <= 86400:
            sys.exit("Invalid PLANE_RENAME_SNIPPET_IDLE_SECONDS (seconds, up to 86400)")
    directory = os.environ.get("PLANE_RENAME_RECEIPT_DIR") or None
    if directory and (not directory.startswith("/") or any(c.isspace() for c in directory)):
        sys.exit("Invalid PLANE_RENAME_RECEIPT_DIR")
    payload = {
        "operation": operation,
        "from": old,
        "to": new,
        "api": api,
        "snippet": snippet_source(),
        "live_url": live_url,
    }
    for key, value in (("receipt_dir", directory), ("api_url", api_url), ("services", services)):
        if value:
            payload[key] = value
    if undrained:
        payload["allow_undrained"] = True
    if idle:
        payload["snippet_idle_seconds"] = idle
    sys.exit(run_remote(host, REMOTE, payload, check=False, interrupted=LOCAL_INTERRUPTED))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(
            "Workspace rename failed before or during SSH. No runtime configuration logged.",
            file=sys.stderr,
        )
        sys.exit(1)
