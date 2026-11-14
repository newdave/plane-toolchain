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


def docker(*args, timeout=None, **kw):
    """Run the Docker CLI; raise on failure unless check=False; return stdout bytes.

    Output is captured unless the caller routes stdout or stderr itself. A hung CLI or daemon
    is killed after `timeout` seconds (default 1800 for `build`, 300 otherwise; the literals
    live here because this source is shipped alone) and raises RuntimeError naming only the
    Docker command, so a caller's rollback runs and no container name is printed.
    """
    kw.setdefault("check", True)
    if "stdout" not in kw and "stderr" not in kw:
        kw["capture_output"] = True
    if timeout is None:
        timeout = 1800 if args[:1] == ("build",) else 300
    try:
        return subprocess.run(["docker", *args], timeout=timeout, **kw).stdout or b""  # noqa: S603,S607
    except subprocess.TimeoutExpired:
        command = args[0] if args else ""
        raise RuntimeError(f"docker {command} timed out after {timeout:g} s") from None


def docker_ok(*args, timeout=300):
    """Run the Docker CLI without output; return whether the command ran and succeeded.

    False means only that it ran and failed (callers read `inspect` as an existence test). A
    command still running after `timeout` seconds is killed and raises RuntimeError, as in
    `docker`, so a hung daemon never reads as a missing container.
    """
    try:
        result = subprocess.run(  # noqa: S603 - fixed docker argv
            ["docker", *args],  # noqa: S607 - docker on PATH
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        command = args[0] if args else ""
        raise RuntimeError(f"docker {command} timed out after {timeout:g} s") from None
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


def release_sigterm(mask, failure=None):
    """Restore the mask from `hold_sigterm`; a deferred SIGTERM is delivered here (exit 143).

    `failure` is the exception the caller re-raises next. That SystemExit would replace it, so
    when SIGTERM is pending its report goes out first, ahead of the termination line. If the
    signal lands only after the check, the report still goes out before the exit propagates.
    A `failure` that is not an `Exception` (the SIGTERM itself) needs no report.
    """
    report = None
    if isinstance(failure, Exception):
        report = type(failure).__name__ + ": " + str(failure)
        if signal.SIGTERM in signal.sigpending():
            say(report, sys.stderr)
            report = None
    try:
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)
    except SystemExit:
        if report:
            say(report, sys.stderr)
        raise


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
SSH_SECONDS = 6 * 3600  # a whole remote operation, pg_dump included; dead links fail sooner
SSH_TIMED_OUT = "SSH session timed out locally after {:g} s."


def ssh_timeout():
    """Seconds the local SSH session may run: `PLANE_SSH_TIMEOUT`, else SSH_SECONDS.

    A long rename can outlast the default and lose its local output (the host program keeps
    running). The value must be from 1 second to 7 days; anything else exits naming only
    the variable."""
    value = os.environ.get("PLANE_SSH_TIMEOUT", "").strip()
    if not value:
        return SSH_SECONDS
    try:
        seconds = float(value)
    except ValueError:
        seconds = 0.0
    if not 1 <= seconds <= 7 * 86400:  # also rejects nan and inf
        sys.exit("PLANE_SSH_TIMEOUT must be a number of seconds from 1 to 604800")
    return seconds


def run_remote(host, source, payload, check=True, interrupted=INTERRUPTED, timeout=None):
    """Run the remote program over SSH with the payload on stdin; return its exit code.

    `timeout` defaults to ssh_timeout(): 6 hours, or `PLANE_SSH_TIMEOUT` seconds.

    The remote's own stderr streams straight to ours. A failure exits with the remote's code
    (1 when unknown) after one fixed line: never the argv (host, program source) or a traceback.
    With check=False the caller gets any exit code back and only Ctrl-C, a dead link and the
    `timeout` (seconds, for the whole session) exit here. On expiry the local ssh is killed and
    this exits 124 with the `interrupted` guidance: the host program ignores the hang-up and may
    still be running, so its own timeouts and rollbacks decide what it leaves behind.
    """
    timeout = ssh_timeout() if timeout is None else timeout
    try:
        result = subprocess.run(  # noqa: S603 - validated owner host; fixed remote program
            ssh_argv(host, source),
            input=json.dumps(payload),
            text=True,
            check=check,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        say(SSH_TIMED_OUT.format(timeout), sys.stderr)
        say(interrupted, sys.stderr)
        sys.exit(124)
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
