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


# --- T7: the apply state machine -------------------------------------------------------------

SHELL = ("python", "manage.py", "shell")
APPLY_OPS = ("step_slug", "step_rows")  # the only operations that emit journal events


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def verify_backup(directory, receipt, uid):
    """Check backup.dump against the receipt before anything runs; True if it must be adopted.

    A recorded SHA-256 must match the file. A dump without one is adopted only at `stopped`:
    backup_database links the dump only after a complete, fsynced pg_dump, so a crash between
    the link and the receipt write leaves exactly that state. Anything else refuses.
    """
    dump = pathlib.Path(directory) / "backup.dump"
    recorded = (receipt or {}).get("backup_sha256")
    present = os.path.lexists(dump)
    if present:
        private_file_check(dump, uid, False)
    step = (receipt or {}).get("step")
    order = step_order("apply")
    taken = step in order and order.index(step) >= order.index("backed_up")
    if recorded:
        if not present:
            raise RuntimeError("backup.dump is missing; refusing")
        if sha256_file(dump) != recorded:
            raise RuntimeError("backup.dump does not match the recorded SHA-256; refusing")
        return False
    if present:
        if step != "stopped":
            raise RuntimeError("backup.dump has no recorded SHA-256; refusing")
        return True
    if taken:
        raise RuntimeError("receipt is past the backup but has no backup SHA-256; refusing")
    return False


def deploy(data, seams):
    """Run `check` (read-only) or `apply` (resumable) for one workspace rename on the host.

    data: operation, from, to, api (container), snippet (source), optional receipt_dir,
    services (PLANE_*_CONTAINER overrides), live_url and api_url. seams: docker, popen, clock,
    sleep, optional crash(label) and uid. Returns the operator message; raises RuntimeError.

    Apply writes the pending receipt first, then runs each step as effect, then advance:
    pending, stopped, backed_up, slug_done, rows_done, cache_done, started, complete. Every
    refusal comes before the first stop. A failure before slug_done restarts the services; a
    failure after it stops with services down for the operator to resume (apply) or revert.
    """
    docker, popen = seams["docker"], seams["popen"]
    crash = seams.get("crash") or (lambda label: None)
    uid = os.geteuid() if seams.get("uid") is None else seams["uid"]
    operation = data.get("operation")
    if operation not in ("check", "apply"):
        raise RuntimeError("expected check or apply")
    old, new, api, snippet = data["from"], data["to"], data["api"], data["snippet"]
    directory = pathlib.Path(data.get("receipt_dir") or receipt_dir(os.environ))
    names = service_names(data.get("services") or {})
    options = {key: data[key] for key in ("live_url", "api_url") if data.get(key)}

    def call(op, args):
        return snippet_ok(api, snippet, op, args, directory, popen, uid)

    # Preflight: read-only, and it writes nothing (not even the receipt directory) for check.
    try:
        inspect_services(names, api, docker)
        receipt = None
        if operation == "apply" or os.path.lexists(directory):
            receipt = load_receipt(directory, old, new, uid)
        if receipt is not None and receipt.get("phase") != "apply":
            raise RuntimeError("receipt is in the revert phase; run revert")
        step = receipt["step"] if receipt else None
        if step == "complete":
            return "Rename already complete"
        records = load_journal(directory, uid) if receipt else []
        workspace_id = journal_workspace(records)
        adopt = verify_backup(directory, receipt, uid)
        order = step_order("apply")
        if workspace_id is None and step in order and order.index(step) >= order.index("slug_done"):
            raise RuntimeError("journal has no workspace record; refusing")
        if workspace_id is None:  # the slug step has not run, so the full check applies
            call("check", {"old": old, "new": new})
        if operation == "check":
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

    def effect(target):
        nonlocal workspace_id
        if target == "stopped":
            remaining = stop_services(names, docker, seams["clock"], seams["sleep"], crash)
            if remaining is None:  # wrong or empty vhost: the drain was never verified
                raise RuntimeError("celery queue not found; drain not verified")
        elif target == "backed_up":
            dump = directory / "backup.dump"
            if adopt:
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

    order = step_order("apply")
    while step != "complete":
        target = order[order.index(step) + 1]
        crash("before_" + target)
        try:
            effect(target)
        except Exception as failure:  # Killed and other BaseExceptions propagate untouched
            reason = str(failure) if isinstance(failure, RuntimeError) else type(failure).__name__
            if order.index(target) <= order.index("slug_done"):
                try:
                    start_services(names, docker, crash)
                except Exception:  # noqa: BLE001 - report, then raise the original failure
                    say("Warning: could not restart every service; start them by hand")
                raise RuntimeError(
                    f"Rename failed before the slug change ({target}): {reason}. Services "
                    "restarted; no data changed. Fix the cause and rerun apply, or run revert."
                ) from None
            if target == "complete":
                raise RuntimeError(
                    f"Rename health check failed: {reason}. Receipt left at started; services "
                    "are running. Investigate, then rerun apply to recheck, or run revert."
                ) from None
            raise RuntimeError(
                f"Rename stopped at {step} ({target}): {reason}. Services stay stopped. Rerun "
                "apply to resume, or run revert; nothing is rolled back automatically."
            ) from None
        crash("after_" + target)
        advance(directory, target, uid)
        step = target
    return f"Rename complete: {old} is now {new}"


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
    journal_workspace,
    run_snippet,
    report,
    snippet_ok,
    inspect_services,
    verify_backup,
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
}
IMPORTS = {"hashlib", "json", "os", "pathlib", "stat", "subprocess", "time"}
FUNCTIONS = (docker, *SHIPPED)


def entry(seams="host_seams()"):
    """Entry stub: the payload is JSON on stdin; a RuntimeError exits 1 with its message only."""
    return (
        "try:\n"
        f"    say(deploy(json.load(sys.stdin), {seams}))\n"
        "except RuntimeError as failure:\n"
        "    say(str(failure), sys.stderr)\n"
        "    sys.exit(1)\n"
    )


# The host program. T9's main sends it over SSH with the payload (snippet included) on stdin.
REMOTE = program(IMPORTS, FUNCTIONS, entry(), SHIPPED_CONSTANTS)
