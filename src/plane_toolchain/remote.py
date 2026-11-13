"""Docker seams and program assembly for deployment code run on owner hosts.

Functions shipped with `program` run as `python3 -c` over SSH: keep them stdlib-only and
never print runtime configuration.
"""

import inspect
import json
import os
import shlex
import signal
import subprocess
import sys

# A dropped SSH session sends SIGHUP and closes the output channel. Ignore both so the remote
# operation and its rollback run to the end; `say` drops writes to a closed channel. Not
# daemonised: a surviving session still sees the final message. SIGKILL or a reboot cannot be
# caught; a later apply reconciles Pages containers, and MCP keeps a pending receipt. `guarded`
# turns SIGTERM into SystemExit so the BaseException rollbacks run (PTC-66).
PREAMBLE = (
    "signal.signal(signal.SIGHUP, signal.SIG_IGN)\nsignal.signal(signal.SIGPIPE, signal.SIG_IGN)\n"
)


def docker(*args, **kw):
    """Run the Docker CLI; raise on failure unless check=False; return stdout bytes.

    Output is captured unless the caller routes stdout or stderr itself.
    """
    kw.setdefault("check", True)
    if "stdout" not in kw and "stderr" not in kw:
        kw["capture_output"] = True
    return subprocess.run(["docker", *args], **kw).stdout or b""  # noqa: S603,S607


def docker_ok(*args, timeout=None):
    """Run the Docker CLI without raising or output; return whether it succeeded.

    With a timeout (seconds) a hung command counts as failure.
    """
    extra = {} if timeout is None else {"timeout": timeout}
    try:
        result = subprocess.run(  # noqa: S603 - fixed docker argv
            ["docker", *args],  # noqa: S607 - docker on PATH
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **extra,
        )
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def say(text, stream=None):
    """Write one line; a closed SSH channel must not abort the operation."""
    try:
        stream = stream or sys.stdout
        stream.write(text + "\n")
        stream.flush()
    except OSError:
        pass


def program(imports, functions, entry, constants=None):
    """Assemble a self-contained remote program from function source and an entry stub.

    `constants` (name -> repr-able literal) become `NAME = <repr>` lines before the functions.
    """
    parts = ["import " + ",".join(sorted({*imports, "signal", "sys"})), PREAMBLE]
    parts += [f"{name} = {value!r}\n" for name, value in (constants or {}).items()]
    parts += [inspect.getsource(function) for function in [say, *functions]]
    return "\n".join(parts + [entry])


def ssh_argv(host, source):
    """SSH argv that never prompts, fails fast on a dead link and keeps the host an operand."""
    options = [
        "BatchMode=yes",
        "ConnectTimeout=15",
        "ServerAliveInterval=15",
        "ServerAliveCountMax=4",
        "LogLevel=QUIET",  # ssh's own diagnostics name the host; remote stderr still passes
    ]
    argv = ["ssh"]
    for option in options:
        argv += ["-o", option]
    return argv + ["--", host, "python3 -c " + shlex.quote(source)]


def quiet_failure(kind, failure, trace):
    """Excepthook for the remote program: `Type: message (in function, line n)` on stderr.

    Only the innermost frame of the program itself is named, never a path or a traceback.
    """
    import traceback

    frames = [f for f in traceback.extract_tb(trace) if f.filename == "<string>"]
    where = " (in " + frames[-1].name + ", line " + str(frames[-1].lineno) + ")" if frames else ""
    say(kind.__name__ + ": " + str(failure) + where, sys.stderr)


def terminate(signum, frame):
    """SIGTERM handler for the remote program: unwind as SystemExit(143) so rollbacks run.

    The first signal ignores further ones, so a second SIGTERM cannot cut the rollback short.
    """
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    say("Terminated (SIGTERM); rolling back", sys.stderr)
    sys.exit(143)


def hold_sigterm():
    """Block SIGTERM for a rollback; return the old mask for `release_sigterm`.

    A SIGTERM that arrives meanwhile stays pending and is delivered (exit 143) once the
    rollback is done, so it can never cut a rollback short.
    """
    return signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})


def release_sigterm(mask):
    signal.pthread_sigmask(signal.SIG_SETMASK, mask)


def guarded(entry):
    """Prefix a remote entry stub with the quiet excepthook and the SIGTERM handler.

    An uncaught exception on the host then prints one line instead of a traceback with host
    paths, and the interpreter still exits 1. SIGTERM raises SystemExit(143) through the
    program's `except BaseException` rollbacks. Tests that exec a program see exceptions as before.
    """
    return (
        inspect.getsource(quiet_failure)
        + inspect.getsource(terminate)
        + "sys.excepthook = quiet_failure\nsignal.signal(signal.SIGTERM, terminate)\n\n"
        + entry
    )


SSH_FAILED = (
    "SSH connection failed or dropped (exit 255). The host program may have run; "
    "check the host, PLANE_MCP_SSH and SSH keys before retrying."
)
REMOTE_FAILED = "Remote operation failed (exit {}); see the message above."
INTERRUPTED = "Interrupted locally. The host program may still be running; check the host."


def run_remote(host, source, payload, check=True, interrupted=INTERRUPTED):
    """Run the remote program over SSH with the payload on stdin; return its exit code.

    The remote's own stderr streams straight to ours. A failure exits with the remote's code
    (1 when unknown) after one fixed line: never the argv (host, program source) or a traceback.
    With check=False the caller gets any exit code back and only Ctrl-C and a dead link exit here.
    """
    try:
        result = subprocess.run(  # noqa: S603 - validated owner host; fixed remote program
            ssh_argv(host, source), input=json.dumps(payload), text=True, check=check
        )
    except subprocess.CalledProcessError as failure:
        code = failure.returncode if failure.returncode > 0 else 1
        say(SSH_FAILED if code == 255 else REMOTE_FAILED.format(code), sys.stderr)
        sys.exit(code)
    except KeyboardInterrupt:
        say(interrupted, sys.stderr)
        sys.exit(130)
    code = getattr(result, "returncode", 0)
    if code == 255:
        say(SSH_FAILED, sys.stderr)
    return code


def require_env(*names):
    """Return the named variables; exit naming the first missing one, never printing values."""
    for name in names:
        if not os.environ.get(name):
            sys.exit("Missing environment variable: " + name)
    return [os.environ[name] for name in names]
