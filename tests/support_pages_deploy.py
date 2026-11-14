"""Shared helpers and fixtures for the split test_pages_deploy files."""

import hashlib
import inspect
import json
import subprocess
import sys
import pytest
from fake_docker import NOW, FakeDocker, run_program
from plane_toolchain import pages_deploy


SOURCE = "ROUTES = []\n"


DIGEST = hashlib.sha256(SOURCE.encode()).hexdigest()


IMAGE = "plane-toolchain-pages:" + DIGEST[:16]


BACKUP = "api-before-plane-toolchain-pages"


LABEL = "plane-toolchain.pages.source"


LEGACY_BACKUP = "api-before-terrainlink-pages"


# Tripwire lookups: one exact-name listing, then an inspect of each listed container.
PS = ("ps", "-a", "--filter", "name=api", "--format", "{{.Names}}")


LOOKUP = [PS, ("inspect", "api")]


RETAINED = (  # the post-run listing behind "retained containers: ..."
    "ps",
    "-a",
    "--filter",
    "name=^api(-before-plane-toolchain-pages|-pages-(reverted|failed)-)",
    "--format",
    "{{.Names}}",
)


HELD = ("inspect", BACKUP)  # the neutral backup label read, only when it is listed


HEALTH = (
    "exec",
    "api",
    "python",
    "-c",
    "import urllib.request; "
    "urllib.request.urlopen('http://127.0.0.1:8000/api/instances/',timeout=3)",
)


PAGES = (
    "exec",
    "api",
    "python",
    "-c",
    "import sys, urllib.error, urllib.request\n"
    "try:\n"
    "    urllib.request.urlopen('http://127.0.0.1:8000/api/v1/workspaces/health-probe/projects/"
    "00000000-0000-0000-0000-000000000000/ce-pages/', timeout=3)\n"
    "except urllib.error.HTTPError as error:\n"
    "    sys.exit(0 if error.code in (401, 403) else 1)\n"
    "sys.exit(1)",
)


def payload(operation):
    return dict(
        operation=operation,
        container="api",
        live_url="http://live:3000",
        source=SOURCE,
        digest=DIGEST,
        replacement="import copy\n" + inspect.getsource(pages_deploy.replacement),
    )


def remote(operation, fake, monkeypatch):
    run_program(pages_deploy.REMOTE, payload(operation), fake, monkeypatch)


UPSTREAM = "makeplane/plane-backend:v1.2.3"


UPSTREAM_DIGEST = "makeplane/plane-backend@sha256:upstream"


MUTATIONS = {"create", "stop", "start", "restart", "rename", "update", "rm", "kill"}


def build_payload(backend_image=None):
    return dict(
        operation="build",
        worker="plane-worker",
        backend_image=backend_image,
        source=SOURCE,
        digest=DIGEST,
    )


@pytest.fixture
def saltbox():
    """Local derived API image, a worker on the upstream tag and no rollback container."""
    fake = FakeDocker()
    fake.images["sha256:base"] = []
    fake.images[UPSTREAM] = [UPSTREAM_DIGEST]
    fake.add("plane-api", labels={"owner": "kept"})
    fake.add("plane-worker")
    fake.containers["plane-worker"]["image"] = UPSTREAM
    return fake


def build(fake, monkeypatch, backend_image=None):
    run_program(pages_deploy.REMOTE, build_payload(backend_image), fake, monkeypatch)


@pytest.fixture
def host():
    fake = FakeDocker()
    fake.add("api", labels={"owner": "kept"})
    return fake


@pytest.fixture
def applied():
    fake = FakeDocker()
    fake.add(BACKUP, labels={"owner": "kept"}, running=False, RestartPolicy={"Name": "no"})
    fake.add("api", labels={"owner": "kept", LABEL: DIGEST})
    return fake


def restart(fake, name):
    return fake.containers[name]["host_config"]["RestartPolicy"]["Name"]


def retained(fake):
    """Every container that is not the live `api` must not auto-start."""
    return {n: restart(fake, n) for n in fake.containers if n != "api"}


def assert_original_restored(fake):
    assert fake.containers["api"]["labels"] == {"owner": "kept"}
    assert fake.containers["api"]["running"]
    assert BACKUP not in fake.containers


@pytest.fixture
def interrupted():
    """A revert died after `rename api -> reverted`: no `api`; the original waits as backup."""
    fake = FakeDocker()
    fake.add(BACKUP, labels={"owner": "kept"}, running=False, RestartPolicy={"Name": "no"})
    fake.add(
        f"api-pages-reverted-{NOW - 5}",
        labels={"owner": "kept", LABEL: DIGEST},
        running=False,
    )
    return fake


def assert_derived_back(fake):
    assert restart(fake, "api") == "unless-stopped"
    assert set(retained(fake).values()) == {"no"}
    assert fake.containers["api"]["labels"][LABEL] == DIGEST
    assert fake.containers["api"]["running"]
    assert not fake.containers[BACKUP]["running"]
    assert not [name for name in fake.containers if "pages-reverted" in name]


def policy_applied(fake):
    fake.add("api", labels={LABEL: DIGEST}, RestartPolicy={"Name": "always"})
    fake.containers[BACKUP]["host_config"]["RestartPolicy"] = {"Name": "no"}


def running(fake):
    return [name for name, container in fake.containers.items() if container["running"]]


NOTE = "Restart policy set to unless-stopped; adjust if your deployment differs\n"


REMIND = "; run revert before any Compose or orchestrator update of this service"


APPLIED = (
    "Pages derived image applied; API healthy; original container retained for rollback"
    "; retained containers: " + BACKUP + REMIND + "\n"
)


APPLIED_WITH_FAILED = (
    "Pages derived image applied; API healthy; original container retained for rollback"
    "; retained containers: "
    + BACKUP
    + ", api-pages-failed-"
    + str(NOW)
    + "; run prune to remove retired ones"
    + REMIND
    + "\n"
)


def assert_freshly_applied(fake, policy):
    """Final layout after a reconciled apply: one running derived `api`, original retained."""
    assert running(fake) == ["api"]
    assert fake.containers["api"]["image"] == IMAGE
    assert fake.containers["api"]["labels"] == {"owner": "kept", LABEL: DIGEST}
    assert restart(fake, "api") == policy
    assert fake.containers[BACKUP]["labels"] == {"owner": "kept"}
    assert set(retained(fake).values()) == {"no"}


@pytest.fixture
def retired():
    """A previous apply died after `rename api -> backup`: no container is named `api`."""
    fake = FakeDocker()
    fake.add(BACKUP, labels={"owner": "kept"}, running=False, RestartPolicy={"Name": "no"})
    return fake


RECOVERY_STEPS = [("rename", "api", BACKUP), ("rename", f"api-pages-reverted-{NOW}", "api")]


ENV = {
    "PLANE_MCP_SSH": "secret-owner@secret-host",
    "PLANE_API_CONTAINER": "secretapi",
    "PLANE_LIVE_URL": "https://secret-live.example:3000",
}


def main_exit(monkeypatch, capsys, env):
    monkeypatch.setattr(sys, "argv", ["pages_deploy", "check"])
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kw: pytest.fail("Must not connect"))
    with pytest.raises(SystemExit) as caught:
        pages_deploy.main()
    output = capsys.readouterr()
    assert output.out == "" and "secret" not in str(caught.value.code) + output.err
    return caught.value.code


def seams(fake):
    def health(name, pages=True):
        pages_deploy.healthy(name, fake.probe, lambda seconds: None, attempts=3, pages=pages)

    return dict(docker=fake.docker, probe=fake.probe, create=fake.create, health=health)


def hang_after(fake, trigger, target, nth=1):
    """Once a call starting with `trigger` was made, the `nth` call starting with `target`
    hangs: it raises TimeoutExpired as subprocess.run does, which the shipped docker() and
    docker_ok() turn into RuntimeError. Only the remote() path goes through them."""
    real, state = fake.execute, {"armed": False, "seen": 0}

    def execute(*args, **kw):
        if state["armed"] and args[: len(target)] == target:
            state["seen"] += 1
            if state["seen"] == nth:
                fake.calls.append(("hung", *args))
                raise subprocess.TimeoutExpired(["docker", *args], 300)
        state["armed"] = state["armed"] or args[: len(trigger)] == trigger
        return real(*args, **kw)

    fake.execute = execute


def labelled_running(fake):
    return [n for n, c in fake.containers.items() if c["labels"].get(LABEL) and c["running"]]


def deploy(operation, fake, later=0):
    return pages_deploy.deploy(payload(operation), now=lambda: NOW + later + 0.5, **seams(fake))


def run_health(fake, attempts=2):
    pages_deploy.healthy("api", fake.probe, lambda seconds: None, attempts=attempts)


LEGACY_LABEL = "terrainlink.pages.source"


REFUSAL = (
    "Legacy-named state found: {}. This release no longer reads it. "
    "Migrate first with plane-toolchain at commit 1cb6153: "
    "Pages revert (apply only if check passes there), MCP apply. Or reconcile manually."
)


def snapshot(fake):
    return json.loads(json.dumps(fake.containers, sort_keys=True))


def assert_untouched(fake, before):
    """Refusal changed nothing: no mutating or build call, containers identical."""
    assert not [c for c in fake.calls if c[0] in MUTATIONS | {"build"}]
    assert fake.builds == []
    assert snapshot(fake) == before


def legacy_backup_host(with_api=True):
    fake = FakeDocker()
    fake.add(LEGACY_BACKUP, labels={"owner": "kept"}, running=False, RestartPolicy={"Name": "no"})
    if with_api:
        fake.add("api", labels={"owner": "kept", LEGACY_LABEL: DIGEST})
    return fake


def only_reads(fake):
    assert not [c for c in fake.calls if c[0] in MUTATIONS | {"build"}]
    assert fake.builds == []


# PTC-57: a failing remote prints its refusal line only; no host, program text or traceback.
FAKE_HOST = "owner@203.0.113.7"


REFUSAL_LINE = "ValueError: Legacy-named state found: example"


def failing_ssh(monkeypatch, code=1, error=None):
    def run(argv, **kw):
        if error:
            raise error
        print(REFUSAL_LINE, file=sys.stderr)
        if kw.get("check"):
            raise subprocess.CalledProcessError(code, argv)
        return subprocess.CompletedProcess(argv, code)

    monkeypatch.setattr(subprocess, "run", run)


def assert_no_leaks(capsys):
    seen = capsys.readouterr()
    text = seen.out + seen.err
    for leak in ("203.0.113.7", "owner@", "python3 -c", "Traceback", "CalledProcessError"):
        assert leak not in text
    return seen


def pages_env(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["pages_deploy", "check"])
    monkeypatch.setenv("PLANE_MCP_SSH", FAKE_HOST)
    monkeypatch.setenv("PLANE_API_CONTAINER", "plane-api_1")
    monkeypatch.setenv("PLANE_LIVE_URL", "http://live:3000")


class FakeSocket:
    """AF_UNIX socket stand-in serving one canned HTTP reply to the real http.client."""

    instances = []

    def __init__(self, *args):
        self.timeout = "unset"
        self.closed = False
        self.sent = b""
        self.reply = b"HTTP/1.1 201 Created\r\nContent-Length: 0\r\n\r\n"
        self.connect_error = None
        FakeSocket.instances.append(self)

    def settimeout(self, seconds):
        self.timeout = seconds

    def connect(self, address):
        if self.connect_error:
            raise self.connect_error

    def sendall(self, data):
        self.sent += data

    def makefile(self, *args, **kw):
        import io

        return io.BytesIO(self.reply)

    def close(self):
        self.closed = True


@pytest.fixture
def sockets(monkeypatch):
    import socket

    FakeSocket.instances = []
    monkeypatch.setattr(socket, "socket", FakeSocket)
    return FakeSocket


BUILD_NOISE = "\n".join("step %d" % n for n in range(40)) + "\nERROR: boom\n"


def park_retired(fake):
    for name in ("api-pages-reverted-1", "api-pages-failed-2", "api-pages-failed-2-1"):
        fake.add(name, labels={LABEL: DIGEST}, running=False, RestartPolicy={"Name": "no"})


def prune(fake, confirm=False):
    data = dict(payload("prune"), confirm=confirm)
    return pages_deploy.deploy(data, now=lambda: NOW + 0.5, **seams(fake))


COMPOSE = {
    "com.docker.compose.project": "plane",
    "com.docker.compose.service": "api",
    "com.docker.compose.container-number": "1",
    "com.docker.compose.config-hash": "abc123",
    "com.docker.compose.version": "2.0",
    "owner": "kept",
}


@pytest.fixture
def composed():
    fake = FakeDocker()
    fake.add("api", labels=dict(COMPOSE))
    return fake
