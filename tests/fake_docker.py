"""In-memory Docker host for offline deployment tests; records every call."""

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
        self.fail = []  # commands that fail once, in order of appearance
        self.builds = []
        self.tracked = False
        self.created_healthy = True

    def add(self, name, labels=None, running=True, healthy=True, **host_config):
        self.containers[name] = {
            "image": "sha256:base",
            "env": ["OPAQUE_SECRET=do-not-print", "CE_PLANE_LIVE_URL=http://old:3000"],
            "labels": dict(labels or {}),
            "running": running,
            "healthy": healthy,
            "host_config": {"RestartPolicy": {"Name": "unless-stopped"}, **host_config},
        }

    def document(self, name):
        container = self.containers[name]
        return {
            "Image": container["image"],
            "Config": {
                "Image": container["image"],
                "Env": list(container["env"]),
                "Labels": dict(container["labels"]),
            },
            "HostConfig": json.loads(json.dumps(container["host_config"])),
            "NetworkSettings": {"Networks": {"shared": {"Aliases": ["api"], "IPAMConfig": None}}},
            "State": {"Running": container["running"]},
        }

    def execute(self, *args, input=None):
        """Return (returncode, stdout) for one docker CLI invocation."""
        self.calls.append(args)
        command, rest = args[0], args[1:]
        if command in self.fail:
            self.fail.remove(command)
            return 1, b""
        if command == "inspect":
            if rest[0] not in self.containers:
                return 1, b""
            return 0, json.dumps([self.document(rest[0])]).encode()
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
        if command == "exec":
            container = self.containers.get(rest[0])
            ok = bool(container and container["running"] and container["healthy"])
            return (0 if ok else 1), b""
        if command == "build":
            assert rest[0] == "-t" and rest[2] == "-"
            with tarfile.open(fileobj=io.BytesIO(input), mode="r") as archive:
                files = {item.name: archive.extractfile(item).read() for item in archive}
            self.builds.append((rest[1], files))
            return 0, b""
        raise AssertionError(f"Unexpected docker call {args}")

    # Injected seams.
    def docker(self, *args, **kw):
        code, stdout = self.execute(*args, input=kw.get("input"))
        if code and kw.get("check", True):
            raise subprocess.CalledProcessError(code, ["docker", *args])
        return stdout

    def probe(self, *args):
        return self.execute(*args)[0] == 0

    def create(self, name, body):
        self.calls.append(("create", name))
        if "create" in self.fail:
            self.fail.remove("create")
            raise ValueError("Derived API container creation failed")
        if name in self.containers:
            raise ValueError("Derived API container creation failed")
        self.containers[name] = {
            "image": body["Image"],
            "env": list(body["Env"]),
            "labels": dict(body["Labels"]),
            "running": False,
            "healthy": self.created_healthy,
            "host_config": body["HostConfig"],
        }

    # Process-level emulation for running a whole remote program.
    def subprocess_run(self, argv, **kw):
        if argv[0] == "git":
            self.calls.append(tuple(argv))
            code, stdout = (0 if self.tracked else 1), b""
        else:
            assert argv[0] == "docker"
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
