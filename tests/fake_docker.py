"""In-memory Docker host for offline deployment tests; records every call."""

import hashlib
import io
import json
import re
import signal
import subprocess
import sys
import tarfile
import time
from urllib.parse import parse_qs, urlparse


class Killed(BaseException):
    """Simulated process death: not an Exception, so no handler in the code under test eats it."""


class FakeDocker:
    """Model containers (name, labels, running state, restart policy) and record calls."""

    def __init__(self):
        self.containers = {}
        self.images = {"sha256:base": ["registry.example/plane-backend@sha256:abc"]}
        self.calls = []
        # One-shot failures: a bare command, or a tuple matching the leading arguments,
        # e.g. "start" or ("rename", "api", "api-before-plane-toolchain-pages").
        self.fail = []
        self.streams = {}  # docker args -> (stdout, stderr) routing of the latest call
        self.builds = []
        self.tracked = False
        self.created_healthy = True
        # Rename host seams. on_manage_shell(stdin_text) -> (stdout, returncode) serves
        # `exec ... manage.py shell`; pg_dump* serve the backup stream; queue_depths is one
        # RabbitMQ depth per probe (the last repeats); crash_at arms a one-shot Killed.
        self.on_manage_shell = None
        self.pg_dump = b"PGDMP-fake-dump"
        self.pg_dump_code = 0
        self.queue_depths = [0]
        self.celery_listed = True
        self.queue_probes = []
        self.crash_label = None
        self.error_output = b""  # stderr attached to a failed call's CalledProcessError

    def add(self, name, labels=None, running=True, healthy=True, hostname=None, **host_config):
        ident = self.new_id()
        self.containers[name] = {
            "id": ident,
            "hostname": hostname or ident[:12],
            "image": "sha256:base",
            "env": ["OPAQUE_SECRET=do-not-print", "CE_PLANE_LIVE_URL=http://old:3000"],
            "labels": dict(labels or {}),
            "running": running,
            "healthy": healthy,
            "pages_status": None,  # Pages probe HTTP status; None: 401 if derived, else 404
            "health": None,  # None: no healthcheck; else a status such as "starting"
            "restart_counts": [0],  # one value consumed per inspect; last one repeats
            "restarting": False,
            "host_config": {"RestartPolicy": {"Name": "unless-stopped"}, **host_config},
        }

    def new_id(self):
        self.serial = getattr(self, "serial", 0) + 1
        return hashlib.sha256(str(self.serial).encode()).hexdigest()

    def document(self, name):
        container = self.containers[name]
        return {
            "Id": container["id"],
            "Image": container["image"],
            "Config": {
                "Hostname": container["hostname"],
                "Image": container["image"],
                "Env": list(container["env"]),
                "Labels": dict(container["labels"]),
            },
            "HostConfig": json.loads(json.dumps(container["host_config"])),
            "NetworkSettings": {"Networks": {"shared": {"Aliases": ["api"], "IPAMConfig": None}}},
            "State": self.state(container),
            "RestartCount": self.restart_count(container),
        }

    @staticmethod
    def restart_count(container):
        counts = container["restart_counts"]
        return counts.pop(0) if len(counts) > 1 else counts[0]

    @staticmethod
    def state(container):
        state = {"Running": container["running"], "Restarting": container["restarting"]}
        if container["health"]:
            state["Health"] = {"Status": container["health"]}
        return state

    def execute(self, *args, input=None):
        """Return (returncode, stdout) for one docker CLI invocation."""
        self.calls.append(args)
        command, rest = args[0], args[1:]
        if self.failing(args):
            return 1, b""
        if command == "inspect":
            if rest[0] not in self.containers:
                return 1, b""
            return 0, json.dumps([self.document(rest[0])]).encode()
        if command == "ps":
            # `ps -a --filter name=<regex> --format {{.Names}}`: Docker matches the regex anywhere.
            pattern = rest[rest.index("--filter") + 1].removeprefix("name=")
            return 0, "".join(
                n + "\n" for n in sorted(self.containers) if re.search(pattern, n)
            ).encode()
        if command == "image":
            assert rest[0] == "inspect"
            if rest[1] not in self.images:
                return 1, b""
            return 0, json.dumps([{"RepoDigests": self.images[rest[1]]}]).encode()
        if command in ("stop", "start", "restart"):
            if rest[0] not in self.containers:
                return 1, b""
            self.containers[rest[0]]["running"] = command != "stop"
            return 0, b""
        if command == "rm":
            if rest[0] not in self.containers or self.containers[rest[0]]["running"]:
                return 1, b""
            del self.containers[rest[0]]
            return 0, b""
        if command == "rename":
            old, new = rest
            if old not in self.containers or new in self.containers:
                return 1, b""
            self.containers[new] = self.containers.pop(old)
            return 0, b""
        if command == "update":
            assert rest[0].startswith("--restart=") and len(rest) == 2
            if rest[1] not in self.containers:
                return 1, b""
            name, _, count = rest[0][len("--restart=") :].partition(":")
            policy = {"Name": name, **({"MaximumRetryCount": int(count)} if count else {})}
            self.containers[rest[1]]["host_config"]["RestartPolicy"] = policy
            return 0, b""
        if command == "exec":
            routed = self.route_rename_exec(rest, input)
            if routed is not None:
                return routed
        if command == "exec":
            container = self.containers.get(rest[0])
            ok = bool(container and container["running"] and container["healthy"])
            if ok and "/ce-pages/" in rest[-1]:
                # The probe script exits 0 only for 401/403. Only a derived image (source
                # label) serves the Pages route; the upstream original answers 404.
                status = container.get("pages_status")
                if status is None:
                    status = 401 if "plane-toolchain.pages.source" in container["labels"] else 404
                ok = status in (401, 403)
            return (0 if ok else 1), b""
        if command == "build":
            assert rest[0] == "-t" and rest[2] == "-"
            with tarfile.open(fileobj=io.BytesIO(input), mode="r") as archive:
                files = {item.name: archive.extractfile(item).read() for item in archive}
            self.builds.append((rest[1], files))
            return 0, b""
        raise AssertionError(f"Unexpected docker call {args}")

    def route_rename_exec(self, rest, input):
        """Serve manage.py shell, pg_dump and the RabbitMQ queue probe; None for other execs."""
        rest = list(rest)
        while rest and rest[0].startswith("-"):  # exec flags such as -i
            rest.pop(0)
        container = self.containers.get(rest[0]) if rest else None
        if container is None:
            return None
        if not container["running"]:
            return 1, b""
        if "manage.py" in rest and "shell" in rest:
            assert self.on_manage_shell is not None, "no manage.py shell handler set"
            stdout, code = self.on_manage_shell((input or b"").decode())
            return code, stdout if isinstance(stdout, bytes) else stdout.encode()
        if rest[-1].startswith("pg_dump"):
            return self.pg_dump_code, self.pg_dump
        if "rabbitmqctl" in " ".join(rest) and "list_queues" in " ".join(rest):
            self.queue_probes.append(" ".join(rest[1:]))
            if not self.celery_listed:  # e.g. the probe hit a different vhost
                return 0, b"other\t0\n"
            depths = self.queue_depths
            depth = depths.pop(0) if len(depths) > 1 else depths[0]
            return 0, f"celery\t{depth}\nother\t0\n".encode()
        return None

    def crash_at(self, label):
        self.crash_label = label

    def checkpoint(self, label):
        """Orchestration seam: raise Killed once at the armed label."""
        if self.crash_label == label:
            self.crash_label = None
            raise Killed(label)

    def popen(self, argv, **kw):
        """Streaming stand-in for subprocess.Popen on a docker argv.

        With stdin=PIPE the command runs when stdout is first read (after the caller has written
        and closed stdin), as `docker exec -i ... manage.py shell` reads its program first.
        """
        assert argv[0] == "docker"
        self.streams[tuple(argv[1:])] = (kw.get("stdout"), kw.get("stderr"))
        fake = self

        class Stdin:
            def __init__(self):
                self.data, self.closed = bytearray(), False

            def write(self, data):
                assert not self.closed
                self.data += data
                return len(data)

            def close(self):
                self.closed = True

        class Process:
            def __init__(self):
                self.stdin = Stdin() if kw.get("stdin") == subprocess.PIPE else None
                self.returncode, self.result, self.killed = None, None, False
                if self.stdin is None:
                    self.run()

            def run(self):
                if self.result is None:
                    piped = bytes(self.stdin.data) if self.stdin is not None else None
                    code, stdout = fake.execute(*argv[1:], input=piped)
                    self.result = (code, io.BytesIO(stdout))
                return self.result

            @property
            def stdout(self):
                return self.run()[1]

            def wait(self):
                self.returncode = self.run()[0]
                return self.returncode

            def kill(self):
                self.killed = True

        return Process()

    def failing(self, args):
        for entry in self.fail:
            if entry == args[0] or (isinstance(entry, tuple) and args[: len(entry)] == entry):
                self.fail.remove(entry)
                return True
        return False

    # Injected seams.
    def docker(self, *args, **kw):
        self.streams[args] = (kw.get("stdout"), kw.get("stderr"))
        code, stdout = self.execute(*args, input=kw.get("input"))
        if code and kw.get("check", True):
            raise subprocess.CalledProcessError(code, ["docker", *args], stderr=self.error_output)
        return stdout

    def probe(self, *args):
        return self.execute(*args)[0] == 0

    def create(self, name, body):
        self.calls.append(("create", name))
        if self.failing(("create", name)):
            raise ValueError("Derived API container creation failed")
        if name in self.containers:
            raise ValueError("Derived API container creation failed")
        ident = self.new_id()
        self.containers[name] = {
            "id": ident,
            "hostname": body.get("Hostname") or ident[:12],
            "image": body["Image"],
            "env": list(body["Env"]),
            "labels": dict(body["Labels"]),
            "running": False,
            "healthy": self.created_healthy,
            "health": None,
            "restart_counts": [0],
            "restarting": False,
            "host_config": body["HostConfig"],
        }

    # Process-level emulation for running a whole remote program.
    def subprocess_run(self, argv, **kw):
        if argv[0] == "git":
            self.calls.append(tuple(argv))
            code, stdout = (0 if self.tracked else 1), b""
        else:
            assert argv[0] == "docker"
            self.streams[tuple(argv[1:])] = (kw.get("stdout"), kw.get("stderr"))
            code, stdout = self.execute(*argv[1:], input=kw.get("input"))
        if code and kw.get("check"):
            raise subprocess.CalledProcessError(code, argv, stderr=self.error_output)
        return subprocess.CompletedProcess(argv, code, stdout if kw.get("capture_output") else None)

    def http_connection(self):
        fake = self

        class Connection:
            def __init__(self, host):
                assert host == "localhost"

            def request(self, method, url, body, headers):
                assert method == "POST" and headers == {"Content-Type": "application/json"}
                parsed = urlparse(url)
                assert parsed.path == "/containers/create"
                try:
                    fake.create(parse_qs(parsed.query)["name"][0], json.loads(body))
                    self.status = 201
                except ValueError:
                    self.status = 409

            def getresponse(self):
                return self

            def read(self):
                return b""

            def close(self):
                pass

        return Connection


NOW = 1700000000


def run_program(program, payload, fake, monkeypatch):
    """Execute a remote program as `python3 -c` would, against the fake host."""
    import http.client

    monkeypatch.setattr(subprocess, "run", fake.subprocess_run)
    monkeypatch.setattr(time, "sleep", lambda seconds: fake.calls.append(("sleep", seconds)))
    monkeypatch.setattr(time, "time", lambda: NOW + 0.5)
    monkeypatch.setattr(http.client, "HTTPConnection", fake.http_connection())
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    terminate = signal.getsignal(signal.SIGTERM)  # a guarded program installs its own handler
    try:
        exec(compile(program, "<string>", "exec"), {"__name__": "__main__"})  # noqa: S102
    except SystemExit as exit:
        assert exit.code in (0, None)
    finally:
        signal.signal(signal.SIGTERM, terminate)
