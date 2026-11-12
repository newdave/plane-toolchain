"""Reversible derived-image deployment for an owner-managed Plane API.

Docker configuration stays inside the remote process. Never print it or save it to
an intermediate file. Retain the stopped original container as the rollback unit.
"""

import copy
import hashlib
import http.client
import io
import json
import os
import shlex
import socket
import subprocess
import sys
import tarfile
from pathlib import Path

from plane_toolchain.remote import docker, docker_ok, program


def replacement(original, image, live_url, digest):
    """Keep the existing opaque runtime configuration without exposing its values."""
    if original["HostConfig"].get("AutoRemove"):
        raise ValueError("AutoRemove prevents retaining a rollback container")
    body = copy.deepcopy(original["Config"])
    body["Image"] = image
    body["Env"] = [
        value for value in body.get("Env", []) if not value.startswith("CE_PLANE_LIVE_URL=")
    ]
    body["Env"].append("CE_PLANE_LIVE_URL=" + live_url)
    body["Labels"] = dict(body.get("Labels") or {}, **{"terrainlink.pages.source": digest})
    body["HostConfig"] = copy.deepcopy(original["HostConfig"])
    endpoints = {}
    for name, value in original["NetworkSettings"]["Networks"].items():
        if value.get("IPAMConfig"):
            raise ValueError("Static network addresses need a deployment-specific migration")
        endpoints[name] = {"Aliases": value.get("Aliases") or []}
    body["NetworkingConfig"] = {"EndpointsConfig": endpoints}
    return body


def backup_name(name):
    """Legacy rollback container name; deployed receipts depend on it."""
    return name + "-before-terrainlink-pages"


def source_label(container):
    return container["Config"].get("Labels", {}).get("terrainlink.pages.source")


def healthy(name, probe, sleep, attempts=45):
    """Wait for the API inside the container to answer; raise when it never does."""
    for _ in range(attempts):
        if probe(
            "exec",
            name,
            "python",
            "-c",
            "import urllib.request; "
            "urllib.request.urlopen('http://127.0.0.1:8000/api/instances/',timeout=3)",
        ):
            return
        sleep(1)
    raise ValueError("API health check failed")


def build_context(base_image, source):
    """Return a tar build context deriving the exact running image."""
    # No credentials or runtime environment in context.
    dockerfile = (
        "FROM " + base_image + "\nCOPY plane_pages.py /code/plane/api/terrainlink_pages.py\n"
        "RUN python -c \"from pathlib import Path; p=Path('/code/plane/api/urls/__init__.py'); "
        "p.write_text(p.read_text()+"
        "'\\nfrom plane.api.terrainlink_pages import routes\\nurlpatterns += routes()\\n')\"\n"
        "RUN python -m py_compile /code/plane/api/terrainlink_pages.py\n"
    ).encode()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for filename, content in [("Dockerfile", dockerfile), ("plane_pages.py", source)]:
            info = tarfile.TarInfo(filename)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return stream.getvalue()


def docker_create(name, body):
    """Create a container through the Docker API socket; the body stays in this process."""

    class DockerHTTP(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.connect("/var/run/docker.sock")

    connection = DockerHTTP("localhost")
    connection.request(
        "POST",
        "/containers/create?name=" + name,
        body=json.dumps(body),
        headers={"Content-Type": "application/json"},
    )
    response = connection.getresponse()
    response.read()
    if response.status != 201:
        raise ValueError("Derived API container creation failed")
    connection.close()


def check(current, payload, health):
    if source_label(current) != payload["digest"]:
        raise ValueError("Pages image does not match source")
    health(payload["container"])
    return "Pages image and API health verified"


def restore_any(name, backup, reverted, probe, docker):
    """Best effort after failed recovery: leave exactly one started `name` container."""
    aside = reverted + "-aside"
    try:
        if probe("inspect", name):
            if probe("start", name):
                return
            if not probe("rename", name, aside):
                return
        for source in (reverted, backup):
            if probe("inspect", source) and probe("rename", source, name):
                if probe("start", name):
                    return
                probe("rename", name, source)
        if probe("inspect", aside):
            probe("rename", aside, name)
    finally:
        # Return a parked container to its legacy name: derived -> reverted, original -> backup.
        if probe("inspect", aside):
            try:
                home = reverted if source_label(json.loads(docker("inspect", aside))[0]) else backup
                if not probe("inspect", home):
                    probe("rename", aside, home)
            except Exception:  # noqa: S110 - best effort; the original error is re-raised
                pass


def revert(current, payload, docker, probe, health, now):
    name = payload["container"]
    backup = backup_name(name)
    if not probe("inspect", backup):
        if source_label(current) is None:
            return "Original API container already restored"
        docker("inspect", backup)  # Fail before any change when no rollback container exists.
    if source_label(current) != payload["digest"]:
        raise ValueError("API changed; refusing rollback")
    reverted = name + "-pages-reverted-" + str(int(now()))
    done = []  # Completed swap steps, so recovery undoes only what happened.
    try:
        done.append("stop")
        docker("stop", name)
        docker("rename", name, reverted)
        done.append("derived")
        docker("rename", backup, name)
        done.append("original")
        docker("start", name)
        health(name)
    except BaseException as error:
        try:
            if "original" in done:
                docker("stop", name)
                docker("rename", name, backup)
            if "derived" in done:
                docker("rename", reverted, name)
            docker("start", name)
        except Exception:
            restore_any(name, backup, reverted, probe, docker)
            raise error  # Recovery error stays as __context__.
        raise
    return "Original API container restored; derived container retained stopped"


def apply(current, payload, docker, probe, create, health, now):
    name, digest = payload["container"], payload["digest"]
    backup = backup_name(name)
    if source_label(current) == digest:
        health(name)
        return "Pages image already applied; API healthy"
    if probe("inspect", backup):
        raise ValueError("Existing rollback container; reconcile before a new deployment")
    source = payload["source"].encode()
    base = json.loads(docker("image", "inspect", current["Image"]))[0]
    if not base.get("RepoDigests"):
        raise ValueError("Running image has no immutable registry digest")
    image = "terrainlink-plane-pages:" + digest[:16]
    docker(
        "build",
        "-t",
        image,
        "-",
        input=build_context(base["RepoDigests"][0], source),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    namespace = {}
    exec(payload["replacement"], namespace)  # noqa: S102 - source shipped by main()
    body = namespace["replacement"](current, image, payload["live_url"], digest)
    docker("stop", name)
    docker("rename", name, backup)
    try:
        create(name, body)
        docker("start", name)
        health(name)
    except BaseException:
        if probe("inspect", name):
            probe("stop", name)
            docker("rename", name, name + "-pages-failed-" + str(int(now())))
        docker("rename", backup, name)
        docker("start", name)
        raise
    return "Pages derived image applied; API healthy; original container retained for rollback"


def deploy(payload, docker, probe, create, health, now):
    """Run one operation against the Docker host; return the operator message.

    docker(*args, **kw) -> bytes raises on failure; probe(*args) -> bool never raises;
    create(name, body) raises unless created; health(name) raises unless healthy.
    """
    current = json.loads(docker("inspect", payload["container"]))[0]
    operation = payload["operation"]
    if operation == "check":
        return check(current, payload, health)
    if operation == "revert":
        return revert(current, payload, docker, probe, health, now)
    if operation != "apply":
        raise ValueError("Unknown operation")
    return apply(current, payload, docker, probe, create, health, now)


REMOTE = program(
    ["http.client", "io", "json", "socket", "subprocess", "sys", "tarfile", "time"],
    [docker, docker_ok, backup_name, source_label, healthy, build_context, docker_create]
    + [check, restore_any, revert, apply, deploy],
    "print(deploy(json.load(sys.stdin), docker, docker_ok, docker_create,\n"
    "    lambda name: healthy(name, docker_ok, time.sleep), time.time))\n",
)


def main():
    import inspect

    operation = sys.argv[1]
    host = os.environ["PLANE_MCP_SSH"]
    container = os.environ["PLANE_API_CONTAINER"]
    live_url = os.environ["PLANE_LIVE_URL"]
    if host.startswith("-") or not container.replace("-", "").replace("_", "").isalnum():
        raise ValueError("Invalid deployment destination")
    source = Path(__file__).with_name("pages_backend.py").read_text()
    payload = dict(
        operation=operation,
        container=container,
        live_url=live_url,
        source=source,
        digest=hashlib.sha256(source.encode()).hexdigest(),
        replacement="import copy\n" + inspect.getsource(replacement),
    )
    subprocess.run(  # noqa: S603 - validated owner host; fixed remote program
        ["ssh", host, "python3 -c " + shlex.quote(REMOTE)],  # noqa: S607 - system SSH
        input=json.dumps(payload),
        text=True,
        check=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(
            "Pages deployment failed; inspect container health "
            "before retrying. No runtime configuration logged.",
            file=sys.stderr,
        )
        sys.exit(1)
