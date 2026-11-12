"""Reversible derived-image deployment for an owner-managed Plane API.

Docker configuration stays inside the remote process. Never print it or save it to
an intermediate file. Retain the stopped original container as the rollback unit.
"""

import copy
import hashlib
import http.client
import io
import json
import socket
import subprocess
import sys
import tarfile
from pathlib import Path
from urllib.parse import urlsplit

from plane_toolchain.remote import docker, docker_ok, program, require_env, say, ssh_argv


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
    # The copied hostname is the old container id; keep only an explicit custom one.
    if body.get("Hostname") == original.get("Id", "")[:12]:
        body.pop("Hostname", None)
    body["HostConfig"] = copy.deepcopy(original["HostConfig"])
    endpoints = {}
    for name, value in original["NetworkSettings"]["Networks"].items():
        if value.get("IPAMConfig"):
            raise ValueError("Static network addresses need a deployment-specific migration")
        aliases = [a for a in value.get("Aliases") or [] if a != original.get("Id", "")[:12]]
        endpoints[name] = {"Aliases": aliases}
    body["NetworkingConfig"] = {"EndpointsConfig": endpoints}
    return body


def backup_name(name):
    """Legacy rollback container name; deployed receipts depend on it."""
    return name + "-before-terrainlink-pages"


def restart_arg(container):
    """`docker update --restart` value for a container's current policy."""
    policy = container["HostConfig"].get("RestartPolicy") or {}
    name = policy.get("Name") or "no"
    count = policy.get("MaximumRetryCount")
    return name + ":" + str(count) if name == "on-failure" and count else name


def source_label(container):
    return container["Config"].get("Labels", {}).get("terrainlink.pages.source")


def healthy(name, probe, sleep, attempts=45):
    """Wait for the API and the Pages route inside the container; raise when they never answer."""
    api = (
        "import urllib.request; "
        "urllib.request.urlopen('http://127.0.0.1:8000/api/instances/',timeout=3)"
    )
    # Unauthenticated dummy ids: 401/403 proves the route exists and auth is enforced.
    pages = (
        "import sys, urllib.error, urllib.request\n"
        "try:\n"
        "    urllib.request.urlopen('http://127.0.0.1:8000/api/v1/workspaces/health-probe/projects/"
        "00000000-0000-0000-0000-000000000000/ce-pages/', timeout=3)\n"
        "except urllib.error.HTTPError as error:\n"
        "    sys.exit(0 if error.code in (401, 403) else 1)\n"
        "sys.exit(1)"
    )
    for _ in range(attempts):
        if probe("exec", name, "python", "-c", api) and probe("exec", name, "python", "-c", pages):
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


def restore_any(name, backup, reverted, probe, docker, policy):
    """Best effort after failed recovery: leave exactly one started `name` container.

    A retained container gets `--restart=no` only after its rename succeeds; a promoted one
    gets `policy`, the live deployment's policy read before any retirement (best effort).
    """
    aside = reverted + "-aside"
    try:
        if probe("inspect", name):
            probe("update", "--restart=" + policy, name)
            if probe("start", name):
                return
            if not probe("rename", name, aside):
                return
            probe("update", "--restart=no", aside)
        for source in (reverted, backup):
            if probe("inspect", source) and probe("rename", source, name):
                probe("update", "--restart=" + policy, name)
                if probe("start", name):
                    return
                if probe("rename", name, source):
                    probe("update", "--restart=no", source)
        if probe("inspect", aside):
            if probe("rename", aside, name):
                probe("update", "--restart=" + policy, name)
                probe("start", name)
    finally:
        # Return a parked container to its legacy name: derived -> reverted, original -> backup.
        if probe("inspect", aside):
            try:
                home = reverted if source_label(json.loads(docker("inspect", aside))[0]) else backup
                if not probe("inspect", home):
                    probe("rename", aside, home)
            except Exception:  # noqa: S110 - best effort; the original error is re-raised
                pass


def finish_revert(name, backup, docker, probe, health):
    """Complete a revert killed after `rename name -> reverted`: promote the original."""
    held = json.loads(docker("inspect", backup))[0]  # Raises when the backup is missing.
    if source_label(held) is not None:
        docker("inspect", name)  # A labelled backup is a derived: not ours to promote; raise.
    prefix = name + "-pages-reverted-"
    try:  # Optional lookup: a failing `ps` falls through to the default policy.
        listing = docker("ps", "-a", "--filter", "name=" + prefix, "--format", "{{.Names}}")
    except subprocess.CalledProcessError:
        listing = b""
    parked = sorted(n for n in listing.decode().split() if n.startswith(prefix))
    # Only the newest plain `<prefix><digits>` is the live derived; older ones may be stale.
    plain = [n for n in parked if n[len(prefix) :].isdigit()]
    policy = None
    if plain:
        candidate = restart_arg(json.loads(docker("inspect", plain[-1]))[0])
        policy = None if candidate == "no" else candidate
    if policy is None:
        policy = held_policy(held)
    for parked_name in parked:
        probe("update", "--restart=no", parked_name)  # Never auto-start beside the API.
    docker("rename", backup, name)
    probe("update", "--restart=" + policy, name)
    docker("start", name)
    health(name)  # On failure the original stays running.
    return "Original API container restored"


def revert(current, payload, docker, probe, health, now):
    name = payload["container"]
    backup = backup_name(name)
    if current is None:
        return finish_revert(name, backup, docker, probe, health)
    if not probe("inspect", backup):
        if source_label(current) is None:
            return "Original API container already restored"
        docker("inspect", backup)  # Fail before any change when no rollback container exists.
    if source_label(current) != payload["digest"]:
        raise ValueError("API changed; refusing rollback")
    reverted = name + "-pages-reverted-" + str(int(now()))
    policy = restart_arg(current)  # Live (derived) policy; the backup's own inspect shows `no`.
    done = []  # Completed swap steps, so recovery undoes only what happened.
    try:
        done.append("stop")
        docker("stop", name)
        docker("rename", name, reverted)
        probe("update", "--restart=no", reverted)
        done.append("derived")
        docker("rename", backup, name)
        done.append("original")
        probe("update", "--restart=" + policy, name)
        docker("start", name)
        health(name)
    except BaseException as error:
        try:
            if "original" in done:
                docker("stop", name)
                docker("rename", name, backup)
                probe("update", "--restart=no", backup)
            if "derived" in done:
                docker("rename", reverted, name)
            probe("update", "--restart=" + policy, name)
            docker("start", name)
        except Exception:
            restore_any(name, backup, reverted, probe, docker, policy)
            raise error  # Recovery error stays as __context__.
        raise
    return "Original API container restored; derived container retained stopped"


def failed_name(name, probe, now):
    """Unused `-pages-failed-<ts>` name; one run can retire two containers in the same second."""
    failed = name + "-pages-failed-" + str(int(now()))
    while probe("inspect", failed):
        failed += "-1"
    return failed


def held_policy(held):
    """Policy for a promoted rollback container.

    A policy other than `no` predates the retire update and is the live one; otherwise fall
    back to Plane's compose default.
    """
    policy = restart_arg(held)
    if policy == "no":
        policy = "unless-stopped"
        # Said at once: a later failure in this run must not hide the change.
        say("Restart policy set to unless-stopped; adjust if your deployment differs")
    return policy


def reconcile(current, name, backup, docker, probe, health, now):
    """Undo a run that died mid-swap: return the original as running `name`, or refuse.

    current is None when no container is named `name`.
    """
    held = json.loads(docker("inspect", backup))[0]
    refuse = ValueError("Existing rollback container; reconcile before a new deployment")
    if source_label(held) is not None:
        raise refuse
    if current is None:
        policy = held_policy(held)
    elif source_label(current) is not None and not current["State"].get("Running"):
        # Derived created but never started: retire it as a failed apply.
        policy = restart_arg(current)  # Copied from the original before its retirement.
        failed = failed_name(name, probe, now)
        docker("rename", name, failed)
        probe("update", "--restart=no", failed)
    else:
        raise refuse
    docker("rename", backup, name)
    probe("update", "--restart=" + policy, name)
    docker("start", name)
    health(name)


def apply(current, payload, docker, probe, create, health, now):
    name, digest = payload["container"], payload["digest"]
    backup = backup_name(name)
    applied = current is not None and source_label(current) == digest
    if applied and current["State"].get("Running"):
        health(name)
        return "Pages image already applied; API healthy"
    if current is None or probe("inspect", backup):
        reconcile(current, name, backup, docker, probe, health, now)
        current = json.loads(docker("inspect", name))[0]
    elif applied:
        health(name)  # Stopped derived without a rollback container: refuse via health.
        return "Pages image already applied; API healthy"
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
    policy = restart_arg(current)  # Read before retiring; restored when promoted back.
    try:
        docker("stop", name)
        docker("rename", name, backup)
    except BaseException:
        probe("start", name)  # Never leave the live API stopped; its policy is unchanged.
        raise
    probe("update", "--restart=no", backup)  # After the rename: a failed rename keeps the policy.
    try:
        create(name, body)
        docker("start", name)
        health(name)
    except BaseException:
        if probe("inspect", name):
            probe("stop", name)
            failed = failed_name(name, probe, now)
            docker("rename", name, failed)
            probe("update", "--restart=no", failed)
        docker("rename", backup, name)
        probe("update", "--restart=" + policy, name)
        docker("start", name)
        raise
    return "Pages derived image applied; API healthy; original container retained for rollback"


def deploy(payload, docker, probe, create, health, now):
    """Run one operation against the Docker host; return the operator message.

    docker(*args, **kw) -> bytes raises on failure; probe(*args) -> bool never raises;
    create(name, body) raises unless created; health(name) raises unless healthy.
    """
    operation = payload["operation"]
    try:
        current = json.loads(docker("inspect", payload["container"]))[0]
    except subprocess.CalledProcessError:
        # A missing API with a rollback container is a half-finished apply or revert.
        if operation not in ("apply", "revert") or not probe(
            "inspect", backup_name(payload["container"])
        ):
            raise
        current = None
    if operation == "check":
        return check(current, payload, health)
    if operation == "revert":
        return revert(current, payload, docker, probe, health, now)
    if operation != "apply":
        raise ValueError("Unknown operation")
    return apply(current, payload, docker, probe, create, health, now)


REMOTE = program(
    ["http.client", "io", "json", "socket", "subprocess", "sys", "tarfile", "time"],
    [
        docker,
        docker_ok,
        backup_name,
        restart_arg,
        source_label,
        healthy,
        build_context,
        docker_create,
    ]
    + [
        check,
        restore_any,
        held_policy,
        finish_revert,
        revert,
        failed_name,
        reconcile,
        apply,
        deploy,
    ],
    "say(deploy(json.load(sys.stdin), docker, docker_ok, docker_create,\n"
    "    lambda name: healthy(name, docker_ok, time.sleep), time.time))\n",
)


def valid_live_url(url):
    """Return whether url is plain http(s) with a host and no credentials, query or fragment."""
    try:
        parsed = urlsplit(url)
        parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.hostname
        and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
        and not any(c.isspace() or c == "\\" for c in url)
    )


def main():
    import inspect

    operation = sys.argv[1]
    host, container, live_url = require_env(
        "PLANE_MCP_SSH", "PLANE_API_CONTAINER", "PLANE_LIVE_URL"
    )
    if not valid_live_url(live_url):
        sys.exit("Invalid PLANE_LIVE_URL")
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
        ssh_argv(host, REMOTE),
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
