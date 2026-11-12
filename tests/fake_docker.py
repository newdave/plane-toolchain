"""In-memory Docker host for offline deployment tests; records every call."""

import hashlib
import io
import json
import subprocess
import sys
import tarfile
import time
from urllib.parse import parse_qs, urlparse


class FakeDocker:
    """Model containers (name, labels, running state, restart policy) and record calls."""

    def __init__(self):
        self.containers = {}
        self.images = {"sha256:base": ["registry.example/plane-backend@sha256:abc"]}
        self.calls = []
        # One-shot failures: a bare command, or a tuple matching the leading arguments,
        # e.g. "start" or ("rename", "api", "api-before-terrainlink-pages").
        self.fail = []
        self.streams = {}  # docker args -> (stdout, stderr) routing of the latest call
        self.builds = []
        self.tracked = False
        self.created_healthy = True

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
            "pages_status": 401,  # HTTP status the Pages probe URL answers with
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
            # `ps -a --filter name=^<prefix> --format {{.Names}}`: names starting with prefix.
            prefix = rest[rest.index("--filter") + 1].removeprefix("name=^")
            return 0, "".join(
                n + "\n" for n in sorted(self.containers) if n.startswith(prefix)
            ).encode()
        if command == "image":
            assert rest[0] == "inspect"
            return 0, json.dumps([{"RepoDigests": self.images[rest[1]]}]).encode()
        if command in ("stop", "start", "restart"):
            if rest[0] not in self.containers:
                return 1, b""
            self.containers[rest[0]]["running"] = command != "stop"
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
            container = self.containers.get(rest[0])
            ok = bool(container and container["running"] and container["healthy"])
            if ok and "/ce-pages/" in rest[-1]:
                # The probe script exits 0 only for 401/403.
                ok = container.get("pages_status", 401) in (401, 403)
            return (0 if ok else 1), b""
        if command == "build":
            assert rest[0] == "-t" and rest[2] == "-"
            with tarfile.open(fileobj=io.BytesIO(input), mode="r") as archive:
                files = {item.name: archive.extractfile(item).read() for item in archive}
            self.builds.append((rest[1], files))
            return 0, b""
        raise AssertionError(f"Unexpected docker call {args}")

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
            raise subprocess.CalledProcessError(code, ["docker", *args])
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
            raise subprocess.CalledProcessError(code, argv)
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
    try:
        exec(compile(program, "<string>", "exec"), {"__name__": "__main__"})  # noqa: S102
    except SystemExit as exit:
        assert exit.code in (0, None)
