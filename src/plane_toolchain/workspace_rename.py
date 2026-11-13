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

import hashlib
import inspect
import json
import os
import pathlib
import stat
import subprocess
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


def stop_services(names, docker, clock, sleep, crash=lambda label: None):
    """Beat, drain, worker, web, space, admin; then restart Live. API, db and Redis stay up."""
    crash("before_stop_" + names["beat"])
    docker("stop", names["beat"])
    crash("after_stop_" + names["beat"])
    remaining = drain_queue(names["mq"], docker, clock, sleep)
    if remaining is None:
        say("Warning: celery queue not found; drain not verified")
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


def run_snippet(api, snippet, op, args, directory, popen, uid=None):
    """Run one snippet operation through `docker exec -i <api> python manage.py shell`.

    Stdin is one request line (`RENAME_REQUEST = ...`) and then the snippet source. Stdout is
    read line by line; non-JSON lines are ignored and never echoed. Each `journal` event is
    appended to the journal (fsynced) before the next line is read, and is never kept or
    printed. Returns (summary or None, other events). A non-zero exit gives no summary.
    """
    request = json.dumps({"op": op, "args": args}, sort_keys=True)
    source = "RENAME_REQUEST = __import__('json').loads(" + repr(request) + ")\n" + snippet
    process = popen(
        ["docker", "exec", "-i", api, *SHELL],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    summary, events = None, []
    try:
        process.stdin.write(source.encode())
        process.stdin.close()
        for raw in process.stdout:
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
            else:
                events.append(event)
    except BaseException:
        process.kill()
        process.wait()
        raise
    if process.wait() != 0:
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


def snippet_ok(api, snippet, op, args, directory, popen, uid=None):
    """run_snippet and report; return the summary. A missing summary or ok:false raises."""
    summary, events = run_snippet(api, snippet, op, args, directory, popen, uid)
    report(op, summary, events)
    if summary is None:
        raise RuntimeError(op + " gave no summary")
    if summary.get("ok") is not True:
        raise RuntimeError(op + " failed")
    return summary


def inspect_services(names, api, docker):
    """Refuse unless every service container exists. Messages name the service, not the host."""
    for key, name in sorted({**names, "api": api}.items()):
        try:
            docker("inspect", name)
        except subprocess.CalledProcessError:
            raise RuntimeError("container not found for service " + key) from None


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


def deploy(data, seams):
    """Run `check` (read-only), `apply` or `revert` (both resumable) for one workspace rename.

    `revert` is handed to revert(); the rest of this docstring describes check and apply.

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
    docker, popen, clock = seams["docker"], seams["popen"], seams["clock"]
    crash = seams.get("crash") or (lambda label: None)
    uid = os.geteuid() if seams.get("uid") is None else seams["uid"]
    operation = data.get("operation")
    if operation == "revert":
        return revert(data, seams)
    if operation not in ("check", "apply"):
        raise RuntimeError("expected check, apply or revert")
    old, new, api, snippet = data["from"], data["to"], data["api"], data["snippet"]
    directory = pathlib.Path(data.get("receipt_dir") or receipt_dir(os.environ))
    names = service_names(data.get("services") or {})
    options = {key: data[key] for key in ("live_url", "api_url") if data.get(key)}
    order = step_order("apply")

    def call(op, args):
        return snippet_ok(api, snippet, op, args, directory, popen, uid)

    def at_or_past(step, mark):
        return step in order and order.index(step) >= order.index(mark)

    # Preflight: read-only, and it writes nothing (not even the receipt directory) for check.
    try:
        inspect_services(names, api, docker)
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
        if step == "complete":
            return "Rename already complete"
        records = load_journal(directory, uid) if receipt else []
        workspace_id = journal_workspace(records)
        backup_action = verify_backup(directory, receipt, uid)
        if workspace_id is None and at_or_past(step, "slug_done"):
            raise RuntimeError("journal has no workspace record; refusing")
        # The full check runs unless the slug step is done: at or past slug_done, or at
        # backed_up when a probe shows NEW held by the journalled workspace (kill after commit).
        slug_committed = at_or_past(step, "slug_done") or (
            step == "backed_up"
            and workspace_id is not None
            and slug_state(api, snippet, old, new, workspace_id, directory, popen, uid) == "renamed"
        )
        if not slug_committed:
            call("check", {"old": old, "new": new})
        call("flush_cache", {"dry_run": True})
        detect_saltbox(names, docker)
    except RuntimeError as failure:
        text = str(failure)
        raise RuntimeError(text if text.startswith("Refused") else "Refused: " + text) from None
    if operation == "check":
        if step:
            say("A receipt for this rename is at step " + step + "; apply resumes from there")
        return "Check passed: apply may run"

    receipt = new_receipt(directory, old, new, uid)  # pending receipt first
    step = receipt["step"]
    if step != "pending":
        say("Resuming the rename from step " + step)

    def stop():
        remaining = stop_services(names, docker, clock, seams["sleep"], crash)
        if remaining is None:  # wrong or empty vhost: the drain was never verified
            raise RuntimeError("celery queue not found; drain not verified")

    def effect(target):
        nonlocal workspace_id
        if target == "stopped":
            stop()
        elif target == "backed_up":
            dump = directory / "backup.dump"
            if backup_action == "adopt" and os.path.lexists(dump):
                set_receipt_field(directory, "backup_sha256", sha256_file(dump), uid)
                say("Adopted backup.dump with no recorded SHA-256 (crash after the dump)")
            elif os.path.lexists(dump):
                say("Backup already taken; SHA-256 matches")
            else:
                backup_database(names["db"], directory, popen, crash, uid)
                say("Database backed up")
        elif target == "slug_done":
            args = {"old": old, "new": new}
            if workspace_id:
                args["workspace_id"] = workspace_id
            summary = call("step_slug", args)
            renamed = summary.get("workspace_id")
            journalled = journal_workspace(load_journal(directory, uid))
            if not renamed or journalled != renamed or workspace_id not in (None, renamed):
                raise RuntimeError("slug step workspace does not match the journal")
            workspace_id = renamed
        elif target == "rows_done":
            call("step_rows", {"old": old, "new": new, "workspace_id": workspace_id, **options})
        elif target == "cache_done":
            call("flush_cache", {})
        elif target == "started":
            start_services(names, docker, crash)
        elif target == "complete":
            call("health", {"old": old, "new": new, "workspace_id": workspace_id, **options})

    def reason_of(failure):
        return str(failure) if isinstance(failure, RuntimeError) else type(failure).__name__

    def pre_slug_failure(target, reason):
        """Restart services and reset the receipt; never returns."""
        notes = []
        try:
            start_services(names, docker, crash)
        except Exception as problem:  # noqa: BLE001 - report it with the original failure
            notes.append(reason_of(problem) + "; start the remaining services by hand")
        try:
            reset_receipt(directory, clock, uid, crash)
        except Exception as problem:  # noqa: BLE001
            notes.append("receipt reset failed: " + reason_of(problem))
        restarted = "Services restarted" if not notes else "Services not all restarted"
        raise RuntimeError(
            f"Rename failed before the slug change ({target}): {reason}. {restarted}; no data "
            "changed. The receipt is back at pending and the backup was set aside, so the next "
            "apply stops services and takes a fresh backup. Fix the cause and rerun apply."
            + "".join(" Warning: " + note + "." for note in notes)
        )

    def post_slug_failure(target, reason):
        if target == "complete":
            raise RuntimeError(
                f"Rename health check failed: {reason}. Receipt left at started; services "
                "are running. Investigate, then rerun apply to recheck, or run revert."
            )
        if target == "started":
            state = "Some services did not start"
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
                stop()
    except Exception as failure:  # noqa: BLE001
        reason = reason_of(failure)
        if at_or_past(step, "slug_done"):
            post_slug_failure(step, reason)
        if workspace_id is not None:  # a slug step may have committed: never guess
            state = slug_state(api, snippet, old, new, workspace_id, directory, popen, uid)
            if state == "renamed":
                post_slug_failure(step, reason)
            if state != "original":
                raise RuntimeError(
                    f"Rename resume failed at {step}: {reason}; slug state unknown. Services "
                    "were left as they are and nothing was reset. Check which workspace holds "
                    "each slug and which services run, then rerun apply or run revert."
                ) from None
        pre_slug_failure(step, reason)

    while step != "complete":
        target = order[order.index(step) + 1]
        crash("before_" + target)
        try:
            effect(target)
            crash("after_" + target)
            advance(directory, target, uid)
        except Exception as failure:  # Killed and other BaseExceptions propagate untouched
            reason = reason_of(failure)
            if target in ("stopped", "backed_up"):
                pre_slug_failure(target, reason)
            if target != "slug_done":
                post_slug_failure(target, reason)
            # step_slug's outcome is unclear (no summary, non-zero exit, mismatch, or a
            # failed advance): probe it read-only before deciding anything.
            journalled = journal_workspace(load_journal(directory, uid))
            state = slug_state(api, snippet, old, new, journalled, directory, popen, uid)
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
        step = target
    return f"Rename complete: {old} is now {new}"


# --- T8: revert from the receipt -------------------------------------------------------------


def revert(data, seams):
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
    docker, popen, clock = seams["docker"], seams["popen"], seams["clock"]
    crash = seams.get("crash") or (lambda label: None)
    uid = os.geteuid() if seams.get("uid") is None else seams["uid"]
    old, new, api, snippet = data["from"], data["to"], data["api"], data["snippet"]
    directory = pathlib.Path(data.get("receipt_dir") or receipt_dir(os.environ))
    names = service_names(data.get("services") or {})
    options = {key: data[key] for key in ("live_url", "api_url") if data.get(key)}
    applied, order = step_order("apply"), step_order("revert")
    partial, finished, notes = None, False, []

    def call(op, args):
        return snippet_ok(api, snippet, op, args, directory, popen, uid)

    def reason_of(failure):
        return str(failure) if isinstance(failure, RuntimeError) else type(failure).__name__

    # Preflight: read-only. It never creates the receipt directory.
    try:
        inspect_services(names, api, docker)
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
                state = slug_state(api, snippet, old, new, workspace_id, directory, popen, uid)
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
                if slug_state(api, snippet, old, new, None, directory, popen, uid) != "original":
                    raise RuntimeError(
                        "slug state unknown: nothing was journalled, but OLD is not live or NEW "
                        "is taken; check which workspace holds each slug, and restore "
                        "backup.dump by hand as the last resort"
                    )
                say("Nothing was journalled; no restore needed")
    except RuntimeError as failure:
        text = str(failure)
        raise RuntimeError(text if text.startswith("Refused") else "Refused: " + text) from None

    if phase == "revert":
        say("Resuming the revert from step " + step)

    def stop():
        remaining = stop_services(names, docker, clock, seams["sleep"], crash)
        if remaining is None:
            raise RuntimeError("celery queue not found; drain not verified")

    def restore():
        nonlocal partial
        set_receipt_field(directory, "restore_started", True, uid)
        crash("before_restore")
        args = {"old": old, "new": new, "journal": records, "workspace_id": workspace_id}
        summary, events = run_snippet(api, snippet, "restore", args, directory, popen, uid)
        report("restore", summary, events)
        crash("after_restore")
        if summary is None:
            raise RuntimeError("restore gave no summary")
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
            call("health", {"old": new, "new": old, "workspace_id": workspace_id, **options})
        else:  # nothing journalled: OLD must be live and NEW free
            call("check", {"old": old, "new": new})

    def effect(target):
        if target == "reverting":
            set_receipt_field(directory, "reverted_from", origin, uid)
        elif target == "restored":
            say("Stopping services for the restore")
            stop()
            if restore_needed:
                restore()
            call("flush_cache", {})
        elif target == "started":
            start_services(names, docker, crash)
        elif target == "reverted":
            verify()

    def finish_partial():
        """Restart services and recheck after a partial restore; never returns."""
        nonlocal finished
        try:
            start_services(names, docker, crash)
        except Exception as problem:  # noqa: BLE001 - report it with the partial restore
            notes.append(reason_of(problem) + "; start the remaining services by hand")
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
            + "".join(" Warning: " + note + "." for note in notes)
        )

    def failed(target, reason):
        if target == "reverting":
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

    if phase == "apply":
        step = None
    while step != "reverted":
        target = order[0] if step is None else order[order.index(step) + 1]
        crash("before_" + target)
        try:
            effect(target)
            crash("after_" + target)
            if partial:
                finish_partial()
            advance(directory, target, uid)
        except Exception as failure:  # Killed and other BaseExceptions propagate untouched
            if finished:  # the report from finish_partial
                raise
            failed(target, reason_of(failure))
        step = target
    return f"Revert complete: {new} is back to {old}"


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
    backup_database,
    receipt_dir,
    append_journal,
    load_journal,
    sha256_file,
    leftover_files,
    journal_workspace,
    run_snippet,
    report,
    snippet_ok,
    inspect_services,
    read_receipt,
    finish_rotation,
    reset_receipt,
    verify_backup,
    slug_state,
    revert,
    deploy,
    host_seams,
)
SHIPPED_CONSTANTS = {
    "PG_DUMP": PG_DUMP,
    "SALTBOX_LABEL": SALTBOX_LABEL,
    "DRAIN_SECONDS": DRAIN_SECONDS,
    "DRAIN_INTERVAL": DRAIN_INTERVAL,
    "SHELL": SHELL,
    "APPLY_OPS": APPLY_OPS,
    "INTERRUPTED": INTERRUPTED,
}
IMPORTS = {"hashlib", "json", "os", "pathlib", "stat", "subprocess", "time"}
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


# The host program. T9's main sends it over SSH with the payload (snippet included) on stdin.
REMOTE = program(IMPORTS, FUNCTIONS, entry(), SHIPPED_CONSTANTS)
