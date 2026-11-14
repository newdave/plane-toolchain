"""Docker orchestration, free-space checks and the database backup of the workspace
rename (shipped to the host)."""

import hashlib
import json
import os
import pathlib
import subprocess
import tempfile
import threading

from plane_toolchain.remote import say
from plane_toolchain.workspace_rename_receipt import secure_receipt_dir, set_receipt_field
from plane_toolchain.workspace_rename_snippet import stderr_tail

# Outage-window Docker orchestration. Only `docker stop`, `start`, `restart` and `exec` (plus
# read-only `inspect`) are used; nothing here changes a container definition. REMOTE ships
# FUNCTIONS (remote.docker plus SHIPPED) with SHIPPED_CONSTANTS and IMPORTS (hashlib, json, os,
# pathlib, stat, subprocess and time). `crash(label)` is a test seam, a no-op on the host.
PG_DUMP = 'pg_dump -Fc -U "$POSTGRES_USER" "$POSTGRES_DB"'


DB_SIZE = (
    'psql -tA -U "$POSTGRES_USER" "$POSTGRES_DB" -c "select pg_database_size(current_database())"'
)


MIN_FREE_BYTES = 64 << 20


# Deadlines for the streamed `docker exec` children (the docker() helper has its own): past
# one the local client is killed. Two backup deadlines fit inside remote.SSH_SECONDS.
DB_SIZE_SECONDS = 120


BACKUP_SECONDS = 2 * 3600  # pg_dump, and again the pg_restore read-back


SALTBOX_LABEL = "com.github.saltbox.saltbox_managed"


DRAIN_SECONDS = 60


DRAIN_INTERVAL = 2


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


def kill_after(process, seconds):
    """Kill `process` (a local `docker exec` client) once `seconds` pass; return (timer,
    expired). `expired` holds True once the deadline fired; cancel the timer when done."""
    expired = []

    def fire():
        expired.append(True)
        process.kill()

    timer = threading.Timer(seconds, fire)
    timer.daemon = True
    timer.start()
    return timer, expired


def database_size(db, popen):
    """Size of the Plane database in bytes from the db container, or None if it is unknown
    (including a query still running after DB_SIZE_SECONDS)."""
    process, timer = None, None
    try:
        process = popen(
            ["docker", "exec", db, "sh", "-c", DB_SIZE],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        timer, expired = kill_after(process, DB_SIZE_SECONDS)
        out = process.stdout.read(64)
        if process.wait() != 0 or expired:
            return None
        return int(out.decode().strip())
    except Exception:  # noqa: BLE001 - an unknown size falls back to the minimum
        if process is not None:
            process.kill()
            process.wait()
        return None
    finally:
        if timer is not None:
            timer.cancel()


def check_free_space(directory, db, popen):
    """Refuse when the receipt directory cannot hold a dump of the database and the journal.

    The dump is compressed, so the database size is a safe upper bound for it; the journal
    records the old value of every row apply rewrites, which the database size also bounds, so
    the need is twice that size. Rotated backups and journals are hard links (finish_rotation),
    so they take no new space and the files already there are already out of the free count.
    With no size, the minimum applies. Free space is what an unprivileged writer can use."""
    needed = max(2 * (database_size(db, popen) or 0), MIN_FREE_BYTES)
    info = os.statvfs(directory)
    if info.f_bavail * info.f_frsize < needed:
        raise RuntimeError(
            "backup refused: not enough free space in the receipt directory for a database dump"
        )


def validate_dump(db, dump, popen, hide=()):
    """Check the finished dump with `pg_restore -f /dev/null` inside the db container.

    That reads the whole archive, every data block included, and writes it nowhere: it proves
    the dump is complete and readable by the tool that restores it, without touching the
    database. The dump goes in on stdin. A read-back still running after BACKUP_SECONDS is
    killed and refused."""
    with tempfile.TemporaryFile() as errors, open(dump, "rb") as source:
        process = popen(
            ["docker", "exec", "-i", db, "pg_restore", "-f", "/dev/null"],
            stdin=source,
            stdout=subprocess.DEVNULL,
            stderr=errors,
        )
        timer, expired = kill_after(process, BACKUP_SECONDS)
        try:
            code = process.wait()
        except BaseException:
            process.kill()
            process.wait()
            raise
        finally:
            timer.cancel()
        if expired:
            raise RuntimeError(
                f"backup refused: pg_restore did not finish reading the dump in "
                f"{BACKUP_SECONDS:g} s"
            )
        if code != 0:
            reason = stderr_tail(errors, hide=hide)
            raise RuntimeError(
                "backup refused: pg_restore cannot read the dump"
                + (" (" + reason + ")" if reason else "")
            )


def backup_database(db, directory, popen, crash=lambda label: None, uid=None, hide=()):
    """Stream pg_dump to backup.dump (0600), hashing and fsyncing; record the SHA-256.

    Before the dump the receipt directory must have room for it. After it, `pg_restore -f
    /dev/null` must read the whole file. A failed, empty or unreadable dump raises before any
    caller changes data, leaving no dump file; failures carry a short, redacted stderr tail.
    A pg_dump still streaming after BACKUP_SECONDS is killed and fails the same way.
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
    digest, size, failure, process, timer = hashlib.sha256(), 0, None, None, None
    try:
        with tempfile.TemporaryFile() as errors:
            process = popen(
                ["docker", "exec", db, "sh", "-c", PG_DUMP],
                stdout=subprocess.PIPE,
                stderr=errors,
            )
            timer, expired = kill_after(process, BACKUP_SECONDS)
            while True:
                chunk = process.stdout.read(1 << 20)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
                os.write(descriptor, chunk)
            if process.wait() != 0 and expired:
                failure = f"backup failed: pg_dump timed out after {BACKUP_SECONDS:g} s"
            elif process.returncode != 0:
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
    finally:
        if timer is not None:
            timer.cancel()
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


SHIPPED = (
    service_names,
    queue_depth,
    drain_queue,
    stop_services,
    start_services,
    detect_saltbox,
    kill_after,
    database_size,
    check_free_space,
    validate_dump,
    backup_database,
    worker_running,
    inspect_services,
    refuse_api_service,
)
