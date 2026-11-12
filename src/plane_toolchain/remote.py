"""Docker seams and program assembly for deployment code run on owner hosts.

Functions shipped with `program` run as `python3 -c` over SSH: keep them stdlib-only and
never print runtime configuration.
"""

import inspect
import os
import shlex
import subprocess
import sys

# A dropped SSH session sends SIGHUP and closes the output channel. Ignore both so the remote
# operation and its rollback run to the end; `say` drops writes to a closed channel. Not
# daemonised: a surviving session still sees the final message. SIGKILL or a reboot cannot be
# caught; a later apply reconciles Pages containers, and MCP keeps a pending receipt.
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


def docker_ok(*args):
    """Run the Docker CLI without raising or output; return whether it succeeded."""
    result = subprocess.run(  # noqa: S603 - fixed docker argv
        ["docker", *args],  # noqa: S607 - docker on PATH
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def say(text, stream=None):
    """Write one line; a closed SSH channel must not abort the operation."""
    try:
        stream = stream or sys.stdout
        stream.write(text + "\n")
        stream.flush()
    except OSError:
        pass


def program(imports, functions, entry):
    """Assemble a self-contained remote program from function source and an entry stub."""
    parts = ["import " + ",".join(sorted({*imports, "signal", "sys"})), PREAMBLE]
    parts += [inspect.getsource(function) for function in [say, *functions]]
    return "\n".join(parts + [entry])


def ssh_argv(host, source):
    """SSH argv that never prompts, fails fast on a dead link and keeps the host an operand."""
    options = [
        "BatchMode=yes",
        "ConnectTimeout=15",
        "ServerAliveInterval=15",
        "ServerAliveCountMax=4",
    ]
    argv = ["ssh"]
    for option in options:
        argv += ["-o", option]
    return argv + ["--", host, "python3 -c " + shlex.quote(source)]


def require_env(*names):
    """Return the named variables; exit naming the first missing one, never printing values."""
    for name in names:
        if not os.environ.get(name):
            sys.exit("Missing environment variable: " + name)
    return [os.environ[name] for name in names]
