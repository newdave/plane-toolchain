"""Read-only: list the Plane MCP container's bind mounts so PLANE_MCP_DIRECTORY can be set.

Configured by PLANE_MCP_SSH and PLANE_MCP_CONTAINER from the environment only. Runs one
`docker inspect --format` over SSH that reads `.Mounts` and nothing else (no environment,
no labels), and changes nothing on the host.
"""

import os
import re
import shlex
import subprocess
import sys

TIMEOUT = 60  # seconds for the whole SSH call; ConnectTimeout bounds the connection itself
CONTAINER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
# Bind mounts only: source, destination and mode, tab separated.
FORMAT = (
    '{{range .Mounts}}{{if eq .Type "bind"}}'
    '{{.Source}}{{"\\t"}}{{.Destination}}{{"\\t"}}{{if .RW}}rw{{else}}ro{{end}}{{"\\n"}}'
    "{{end}}{{end}}"
)


def fail(message):
    sys.exit("plane-mcp-mounts: " + message)


def settings(env):
    """Return (host, container) from env; exit with a clear message when missing or invalid."""
    for name in ("PLANE_MCP_SSH", "PLANE_MCP_CONTAINER"):
        if not env.get(name):
            fail("Missing environment variable: " + name)
    host, container = env["PLANE_MCP_SSH"], env["PLANE_MCP_CONTAINER"]
    if host.startswith("-") or any(c.isspace() for c in host):
        fail("Invalid PLANE_MCP_SSH")
    if not CONTAINER.fullmatch(container):
        fail("Invalid PLANE_MCP_CONTAINER")
    return host, container


def ssh_argv(host, container):
    remote = " ".join(["docker", "inspect", "--format", shlex.quote(FORMAT), container])
    options = ["BatchMode=yes", "ConnectTimeout=15", "ServerAliveInterval=15"]
    options += ["ServerAliveCountMax=4", "LogLevel=QUIET"]
    argv = ["ssh"]
    for option in options:
        argv += ["-o", option]
    return [*argv, "--", host, remote]


def mounts(output):
    """Return clean `source<TAB>destination<TAB>mode` lines; drop anything else."""
    rows = []
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) == 3 and fields[2] in ("rw", "ro"):
            rows.append("\t".join(re.sub(r"[\x00-\x1f\x7f]", "?", f) for f in fields))
    return rows


def main(env=None):
    host, container = settings(os.environ if env is None else env)
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, host kept an operand after `--`
            ssh_argv(host, container),
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        fail(f"ssh timed out after {TIMEOUT} s")
    if result.returncode != 0:
        fail(f"ssh or docker inspect failed (exit {result.returncode}); check host and container")
    rows = mounts(result.stdout)
    if not rows:
        fail("No bind mounts found on the container")
    print("source\tdestination\tmode")
    print("\n".join(rows))


if __name__ == "__main__":
    main()
