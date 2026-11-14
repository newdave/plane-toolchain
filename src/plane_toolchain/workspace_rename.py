"""Workspace slug rename: owner-side tooling.

These functions run on the host inside the stdlib-only REMOTE built by program(), so they take
every path as an argument and use the standard library only. SHIPPED lists what program()
ships: the SHIPPED tuples of the parts (workspace_rename_receipt, _docker, _snippet, _run,
_apply, _revert), each in source order. REMOTE is the assembled host program; `deploy` is its
entry point. `snippet_source` (owner side, not shipped) builds the Django snippet that the
payload carries to `manage.py shell`.
`say` and the `docker` helper come from remote.py; program() adds `say` itself.

The parts are adopted into this namespace (see workspace_rename_share), so patching a name
here, as the tests do, reaches every caller. Every part name is readable as an attribute.

Messages never carry receipt or journal content.
"""

import fcntl
import inspect
import os
import pathlib
import subprocess
import sys
import time

from plane_toolchain import (
    workspace_rename_apply,
    workspace_rename_backend,
    workspace_rename_docker,
    workspace_rename_match,
    workspace_rename_receipt,
    workspace_rename_revert,
    workspace_rename_run,
    workspace_rename_share,
    workspace_rename_snippet,
)
from plane_toolchain.remote import docker, program  # docker is shipped
from plane_toolchain.workspace_rename_apply import run_apply
from plane_toolchain.workspace_rename_docker import (
    BACKUP_SECONDS,
    DB_SIZE,
    DB_SIZE_SECONDS,
    DRAIN_INTERVAL,
    DRAIN_SECONDS,
    MIN_FREE_BYTES,
    PG_DUMP,
    SALTBOX_LABEL,
    service_names,
)
from plane_toolchain.workspace_rename_receipt import private_file_check, secure_receipt_dir
from plane_toolchain.workspace_rename_revert import run_revert
from plane_toolchain.workspace_rename_run import run_directory, seam_uid
from plane_toolchain.workspace_rename_snippet import (
    APPLY_OPS,
    BOOTSTRAP,
    KILL_WAIT,
    KILLER,
    SHELL,
    SNIPPET_IDLE_SECONDS,
    SNIPPET_MARKER,
    STUCK,
)

PARTS = (
    workspace_rename_receipt,
    workspace_rename_docker,
    workspace_rename_snippet,
    workspace_rename_run,
    workspace_rename_apply,
    workspace_rename_revert,
)
# One namespace for every part function, like the host program (see workspace_rename_share).
_ADOPTED = workspace_rename_share.adopt(globals(), PARTS)

INTERRUPTED = (
    "Rename interrupted ({}). Services may be stopped. The receipt keeps the last completed "
    "step: rerun apply to resume, or run revert (rerun revert if a revert was interrupted). "
    "Both stop services first."
)


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

    The snippet deadline override (`snippet_idle_seconds`) is read per run by run_context."""
    descriptor = lock_run(run_directory(data), seam_uid(seams), create) if lock else None
    try:
        return run(data, seams)
    finally:
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


def _shipped(part):
    """The part's SHIPPED functions as adopted here, so a patched name is what runs."""
    return tuple(_ADOPTED.get(function.__name__, function) for function in part.SHIPPED)


SHIPPED = (
    *_shipped(workspace_rename_receipt),
    *_shipped(workspace_rename_docker),
    *_shipped(workspace_rename_snippet),
    *_shipped(workspace_rename_run),
    *_shipped(workspace_rename_apply),
    *_shipped(workspace_rename_revert),
    lock_run,
    run_locked,
    revert,
    deploy,
    host_seams,
)
SHIPPED_CONSTANTS = {
    "PG_DUMP": PG_DUMP,
    "DB_SIZE": DB_SIZE,
    "MIN_FREE_BYTES": MIN_FREE_BYTES,
    "DB_SIZE_SECONDS": DB_SIZE_SECONDS,
    "BACKUP_SECONDS": BACKUP_SECONDS,
    "SNIPPET_IDLE_SECONDS": SNIPPET_IDLE_SECONDS,
    "KILL_WAIT": KILL_WAIT,
    "SNIPPET_MARKER": SNIPPET_MARKER,
    "STUCK": STUCK,
    "KILLER": KILLER,
    "SALTBOX_LABEL": SALTBOX_LABEL,
    "DRAIN_SECONDS": DRAIN_SECONDS,
    "DRAIN_INTERVAL": DRAIN_INTERVAL,
    "SHELL": SHELL,
    "BOOTSTRAP": BOOTSTRAP,
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
    stale = os.environ.get("PLANE_RENAME_ALLOW_STALE_BINARIES") or None
    if stale not in (None, "1"):
        sys.exit("Invalid PLANE_RENAME_ALLOW_STALE_BINARIES (use 1)")
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
    if stale:
        payload["allow_stale_binaries"] = True
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
