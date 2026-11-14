"""The Django snippet process (run, kill, report) of the workspace rename (shipped to the
host)."""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading

from plane_toolchain.remote import say
from plane_toolchain.workspace_rename_receipt import append_journal

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


def stderr_tail(handle, limit=300, hide=()):
    """The last few lines of a child's stderr, safe to show: quoted text (names, roles, hosts
    and paths in quotes) and every name in `hide` (container names) is replaced, control
    characters are dropped and the result is capped.

    Best effort: a child could still name a value unquoted, which is why only a short tail is
    kept and nothing the tool read from the environment is ever added. An unquoted value cannot
    be told from ordinary words, so none is guessed at; the tail goes to the operator's own
    terminal only, never to a log, the journal, a receipt or Plane (PTC-115)."""
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
    # A deep traceback puts its `Traceback` line far above the class line, so read a wide
    # window (64 KiB covers a ~1000-frame recursion error); stderr_tail keeps its short one.
    handle.seek(max(0, handle.tell() - 65536))
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


SHELL = ("python", "manage.py", "shell")


# `manage.py shell -c BOOTSTRAP`: read one length line, then exactly that many bytes of program
# (the request line and the snippet) from stdin and run them. Stdin then stays open for acks:
# the host writes `ack` after each journal line is fsynced, and the snippet waits for it before
# the write that line covers. Plain `manage.py shell` would read stdin to its end first.
BOOTSTRAP = (
    "import sys; exec(sys.stdin.buffer.read(int(sys.stdin.buffer.readline())).decode('utf-8'))"
)


APPLY_OPS = ("step_slug", "step_rows")  # the only operations that emit journal events


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


def run_snippet(api, snippet, op, args, directory, popen, uid=None, hide=(), idle=None):
    """Run one snippet operation through `docker exec -i <api> python manage.py shell`.

    The program runs as `manage.py shell -c BOOTSTRAP`. Stdin is a length line, then the
    program: one request line (`RENAME_REQUEST = ...`) and the snippet source. Stdout is read
    line by line; non-JSON lines are ignored and never echoed. Each `journal` event is appended
    to the journal (fsynced) and only then acknowledged with an `ack` line on stdin; the
    snippet writes the row it covers only after that ack. Journal lines are never kept or
    printed. Returns (summary or None, other events). A non-zero exit gives no summary.
    Stderr goes to a temporary file. Without a summary, an `exit` event carries the exit code
    and snippet_cause (exception class and redacted stderr tail; `hide` names are masked).

    The process runs with a `PLANE_RENAME_RUN=<receipt-dir id>:<token>` marker in its environment. Killing the
    local `docker exec` client does not stop the Python process in the container, so every
    abort (a deadline of `idle` seconds, default SNIPPET_IDLE_SECONDS, without output, an error, an interrupt, a
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
            [
                "docker",
                "exec",
                "-i",
                "-e",
                SNIPPET_MARKER + "=" + token,
                api,
                *SHELL,
                "-c",
                BOOTSTRAP,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
        )
        summary, events = read_snippet(
            process,
            source,
            api,
            op,
            token,
            directory,
            popen,
            uid,
            SNIPPET_IDLE_SECONDS if idle is None else idle,
        )
        if summary is None:
            code = process.wait()
            cause = snippet_cause(errors, hide=(*hide, api))
            if code or cause:
                events.append({"event": "exit", "code": code, "cause": cause})
    return summary, events


def read_snippet(process, source, api, op, token, directory, popen, uid, idle):
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
        timer[:] = [threading.Timer(idle, expire)]
        timer[0].daemon = True
        timer[0].start()

    def disarm():
        for old in timer:
            old.cancel()

    try:
        arm()
        program = source.encode()
        process.stdin.write(str(len(program)).encode() + b"\n" + program)
        process.stdin.flush()  # stdin stays open: the acks follow
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
                process.stdin.write(b"ack\n")  # only once the line is durable
                process.stdin.flush()
            elif event.get("event") == "summary":
                summary = event
            elif event.get("event") != "progress":  # progress only re-arms the deadline
                events.append(event)
        process.stdin.close()
    except BaseException as failure:
        disarm()
        process.kill()
        process.wait()
        try:
            process.stdin.close()
        except OSError:  # the pipe is already broken; nothing is left to send
            pass
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
                f"{op} timed out after {idle:g} s without output; the snippet "
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
        elif kind == "not_rebuilt":
            say(
                f"Not rebuilt: {event.get('count')} in {event.get('table')}."
                f"{event.get('column')} (the stored editor binary keeps the old slug)"
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


def snippet_ok(api, snippet, op, args, directory, popen, uid=None, hide=(), idle=None):
    """run_snippet and report; return the summary. A missing summary or ok:false raises."""
    summary, events = run_snippet(api, snippet, op, args, directory, popen, uid, hide, idle)
    report(op, summary, events)
    if summary is None:
        raise RuntimeError(no_summary(op, events))
    if summary.get("ok") is not True:
        raise RuntimeError(op + " failed")
    return summary


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


def slug_state(api, snippet, old, new, workspace_id, directory, popen, uid=None, idle=None):
    """Read-only probe of the slug after an unclear step_slug: "renamed", "original" or
    "unknown".

    With the journalled `workspace_id`, health reports whether NEW is held by exactly that
    workspace and OLD by none ("renamed"), and, with the slugs swapped, whether OLD is held by
    it and NEW by none ("original"). Without an id, a passing `check` (OLD live, NEW free) is
    "original". Health's API check is ignored here; only the slug checks count."""

    def held(first, second):
        args = {"old": first, "new": second, "workspace_id": workspace_id}
        summary, events = run_snippet(
            api, snippet, "health", args, directory, popen, uid, idle=idle
        )
        checks = {e.get("check"): e.get("ok") for e in events if e.get("event") == "health"}
        return summary is not None and checks.get("new_present") and checks.get("old_absent")

    try:
        if workspace_id:
            if held(old, new):
                return "renamed"
            return "original" if held(new, old) else "unknown"
        summary, _ = run_snippet(
            api, snippet, "check", {"old": old, "new": new}, directory, popen, idle=idle
        )
        return "original" if summary is not None and summary.get("ok") is True else "unknown"
    except Exception:  # noqa: BLE001 - a failed probe is an unknown state, never a guess
        return "unknown"


SHIPPED = (
    stderr_tail,
    exception_class,
    snippet_cause,
    no_summary,
    run_id,
    run_killer,
    is_stuck,
    stop_snippet,
    run_snippet,
    read_snippet,
    report,
    snippet_ok,
    sweep_orphans,
    slug_state,
)
