"""Run a shipped remote program in a real process against the fake Docker host.

Used by the SIGTERM tests: the program blocks in its health wait (the first `time.sleep`
writes a marker file, then really sleeps), the test sends SIGTERM, and this script reports the
host state the program's own rollback left behind.

usage: sigterm_harness.py <config.json>   (kind, directory, marker; plus the payload)
"""

import http.client
import io
import json
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from fake_docker import FakeDocker  # noqa: E402

from plane_toolchain import mcp_deploy, pages_deploy  # noqa: E402

config = json.loads(pathlib.Path(sys.argv[1]).read_text())
fake = FakeDocker()
if config["kind"] == "pages":
    fake.add("api", labels={"owner": "kept"})
    fake.created_healthy = False  # the derived container never turns healthy: apply waits
    program = pages_deploy.REMOTE
else:
    fake.add("plane-mcp")
    fake.containers["plane-mcp"]["health"] = "starting"
    if config.get("fail_restart"):
        fake.fail.append("restart")  # the first restart fails: a failure-triggered rollback
    program = mcp_deploy.REMOTE

real_sleep = time.sleep
blocked = []


def sleep(seconds):
    if not blocked:
        blocked.append(True)
        pathlib.Path(config["marker"]).write_text("waiting")
        real_sleep(config.get("block", 60))


subprocess.run = fake.subprocess_run
time.sleep = sleep
http.client.HTTPConnection = fake.http_connection()
sys.stdin = io.StringIO(json.dumps(config["payload"]))
code = 0
try:
    exec(compile(program, "<string>", "exec"), {"__name__": "__main__"})  # noqa: S102
except SystemExit as exit:
    code = exit.code
state = {
    "code": code,
    "containers": {
        name: {
            "running": c["running"],
            "labelled": bool(c["labels"].get("plane-toolchain.pages.source")),
        }
        for name, c in fake.containers.items()
    },
    "calls": [
        list(call) for call in fake.calls if call and call[0] in ("restart", "rename", "start")
    ],
}
print(json.dumps(state))
sys.exit(code)
