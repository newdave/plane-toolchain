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
import re
import socket
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from plane_toolchain.legacy import (
    NEUTRAL_PAGES_BACKEND_FILE,
    NEUTRAL_PAGES_BACKEND_MODULE,
    NEUTRAL_PAGES_BACKUP_SUFFIX,
    NEUTRAL_PAGES_IMAGE_PREFIX,
    NEUTRAL_PAGES_LABEL,
    PAGES,
    PAGES_BACKUP_SUFFIX,
    PAGES_LABEL,
    refusal,
)
from plane_toolchain.remote import (
    docker,
    docker_ok,
    guarded,
    hold_sigterm,
    program,
    release_sigterm,
    require_env,
    run_remote,
    say,
)


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
    # Compose labels stay on purpose (PTC-67): the retained original keeps them anyway, and with
    # them a `compose up` converges on one container per service. See docs/extensions.md.
    labels = dict(body.get("Labels") or {})
    body["Labels"] = dict(labels, **{NEUTRAL_PAGES_LABEL: digest})
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
    """Neutral rollback container name: the only one this helper writes."""
    return name + NEUTRAL_PAGES_BACKUP_SUFFIX


def listed_names(name, docker):
    """Container names exactly matching `name` or its backups; a `ps` failure raises (fail closed)."""
    listing = docker("ps", "-a", "--filter", "name=" + name, "--format", "{{.Names}}")
    return set(listing.decode().split())


def legacy_state(current, name, listed, docker):
    """Sorted legacy names persisted on the host: backup container, source label; [] when clean.

    `listed` is the exact-name set from `listed_names`. Inspects only listed containers, and an
    inspect failure raises so the run refuses. Names only; never configuration. Makes no change.
    """
    found = set()
    if name + PAGES_BACKUP_SUFFIX in listed:
        found.add(name + PAGES_BACKUP_SUFFIX)
    held = None
    if backup_name(name) in listed:
        held = json.loads(docker("inspect", backup_name(name)))[0]
    for container in (current, held):
        if container is not None and PAGES_LABEL in (container["Config"].get("Labels") or {}):
            found.add(PAGES_LABEL)
    return sorted(found)


def restart_arg(container):
    """`docker update --restart` value for a container's current policy."""
    policy = container["HostConfig"].get("RestartPolicy") or {}
    name = policy.get("Name") or "no"
    count = policy.get("MaximumRetryCount")
    return name + ":" + str(count) if name == "on-failure" and count else name


def set_restart(probe, policy, container):
    """`docker update --restart`; say so on stderr when it fails, since a retained container
    that keeps its old policy could start beside the live API."""
    if not probe("update", "--restart=" + policy, container):
        say(
            "Restart policy update failed for " + container + "; check it with docker inspect",
            sys.stderr,
        )


def source_label(container):
    """Deployed source digest from the neutral label; None when unlabelled."""
    return (container["Config"].get("Labels") or {}).get(NEUTRAL_PAGES_LABEL)


def healthy(name, probe, sleep, attempts=45, pages=True, budget=120, clock=time.monotonic):
    """Wait for the API, and the Pages route when pages, inside the container; raise on timeout.

    Stops after `attempts` tries or `budget` seconds, whichever comes first, so slow probes
    cannot stretch the wait to minutes.

    The upstream original has no Pages route, so restoring it checks the API only.
    """
    api = (
        "import urllib.request; "
        "urllib.request.urlopen('http://127.0.0.1:8000/api/instances/',timeout=3)"
    )
    # Unauthenticated dummy ids: 401/403 proves the route exists and auth is enforced.
    route = (
        "import sys, urllib.error, urllib.request\n"
        "try:\n"
        "    urllib.request.urlopen('http://127.0.0.1:8000/api/v1/workspaces/health-probe/projects/"
        "00000000-0000-0000-0000-000000000000/ce-pages/', timeout=3)\n"
        "except urllib.error.HTTPError as error:\n"
        "    sys.exit(0 if error.code in (401, 403) else 1)\n"
        "sys.exit(1)"
    )
    start = clock()
    for _ in range(attempts):
        if probe("exec", name, "python", "-c", api) and (
            not pages or probe("exec", name, "python", "-c", route)
        ):
            return
        if clock() - start >= budget:
            break
        sleep(1)
    raise ValueError("API health check failed")


def build_context(base_image, source):
    """Return a tar build context deriving the exact running image."""
    # No credentials or runtime environment in context.
    dockerfile = (
        "FROM " + base_image + "\nCOPY plane_pages.py " + NEUTRAL_PAGES_BACKEND_FILE + "\n"
        "RUN python -c \"from pathlib import Path; p=Path('/code/plane/api/urls/__init__.py'); "
        "p.write_text(p.read_text()+"
        "'\\nfrom "
        + NEUTRAL_PAGES_BACKEND_MODULE
        + " import routes\\nurlpatterns += routes()\\n')\"\n"
        "RUN python -m py_compile " + NEUTRAL_PAGES_BACKEND_FILE + "\n"
    ).encode()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for filename, content in [("Dockerfile", dockerfile), ("plane_pages.py", source)]:
            info = tarfile.TarInfo(filename)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return stream.getvalue()


def docker_create(name, body, timeout=30):
    """Create a container through the Docker API socket; the body stays in this process.

    The socket has a timeout and is always closed. A failure names only the HTTP status or the
    kind of error, never the body, the socket path or the response text.
    """

    class DockerHTTP(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(timeout)
            self.sock.connect("/var/run/docker.sock")

    failed = "Derived API container creation failed ("
    connection = DockerHTTP("localhost")
    try:
        connection.request(
            "POST",
            "/containers/create?name=" + name,
            body=json.dumps(body),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        response.read()
        status = response.status
    except (OSError, http.client.HTTPException) as failure:
        raise ValueError(failed + type(failure).__name__ + ")") from None
    finally:
        connection.close()
    if status != 201:
        raise ValueError(failed + "HTTP " + str(status) + ")")


def build_image(base, source, digest, docker):
    """Build the derived image tag for `digest` from `base` and `source`; return the tag.

    stdout is discarded. On failure the error carries the exit code and a bounded tail of
    docker's stderr (last 5 lines, 200 characters each): the build context holds no runtime
    configuration, so none can appear.
    """
    image = NEUTRAL_PAGES_IMAGE_PREFIX + digest[:16]
    try:
        docker(
            "build",
            "-t",
            image,
            "-",
            input=build_context(base, source),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as failure:
        text = (failure.stderr or b"").decode(errors="replace")
        # Docker echoes the base image reference; keep the registry host out of the message.
        for reference in (base, base.partition("@")[0], base.partition("/")[0]):
            text = text.replace(reference, "<base image>") if reference else text
        lines = text.splitlines()
        tail = " | ".join(line.strip()[:200] for line in lines[-5:] if line.strip())
        raise ValueError(
            "Image build failed (exit "
            + str(failure.returncode)
            + ")"
            + (": " + tail if tail else "")
        ) from None
    return image


def retained_names(name, docker):
    """Sorted rollback and retired containers of `name`; empty if the listing fails."""
    prefixes = (
        backup_name(name),
        name + "-pages-reverted-",
        name + "-pages-failed-",
    )
    pattern = "^" + name + "(" + NEUTRAL_PAGES_BACKUP_SUFFIX + "|-pages-(reverted|failed)-)"
    try:
        listing = docker("ps", "-a", "--filter", "name=" + pattern, "--format", "{{.Names}}")
    except subprocess.CalledProcessError:
        return []
    return sorted(n for n in listing.decode().split() if n.startswith(prefixes))


def retained_note(name, docker):
    """` Retained containers: ...` for the operator message, or '' when none (up to 20 named)."""
    names = retained_names(name, docker)
    if not names:
        return ""
    more = "; " + str(len(names) - 20) + " more" if len(names) > 20 else ""
    note = "; retained containers: " + ", ".join(names[:20]) + more
    if any(not n.startswith(backup_name(name)) for n in names):
        note += "; run prune to remove retired ones"
    return note


def prune(name, docker, probe, confirm=False):
    """Remove (or, without `confirm`, list) stopped retired derived containers of `name`.

    Only a container that carries the Pages source label and is named exactly
    `<name>-pages-(reverted|failed)-<digits>[-<digits>...]` qualifies: never the live API, the
    rollback container, a running container, an `-aside` parking name or anything unlabelled
    (an unlabelled one may be the upstream original).
    """
    exact = re.compile(re.escape(name) + r"-pages-(reverted|failed)-\d+(-\d+)*")
    candidates = [n for n in retained_names(name, docker) if exact.fullmatch(n)]
    eligible, running = [], []
    for container in candidates:
        held = json.loads(docker("inspect", container))[0]
        if source_label(held) is None:
            continue
        (running if held["State"].get("Running") else eligible).append(container)
    if not eligible and not running:
        return "No retired containers to prune"
    parts = []
    if not confirm:
        parts.append("Dry run, nothing removed. Would remove: " + (", ".join(eligible) or "none"))
    else:
        removed = [c for c in eligible if probe("rm", c)]
        stuck = [c for c in eligible if c not in removed]
        parts.append("Pruned: " + ", ".join(removed) if removed else "Nothing pruned")
        if stuck:
            parts.append("could not remove: " + ", ".join(stuck))
    if running:
        parts.append("kept running: " + ", ".join(running))
    if not confirm and eligible:
        parts.append("set PLANE_PAGES_PRUNE=yes to remove them")
    return "; ".join(parts)


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
            set_restart(probe, policy, name)
            if probe("start", name):
                return
            if not probe("rename", name, aside):
                return
            set_restart(probe, "no", aside)
        for source in (reverted, backup):
            if probe("inspect", source) and probe("rename", source, name):
                set_restart(probe, policy, name)
                if probe("start", name):
                    return
                if probe("rename", name, source):
                    set_restart(probe, "no", source)
        if probe("inspect", aside):
            if probe("rename", aside, name):
                set_restart(probe, policy, name)
                probe("start", name)
    finally:
        # Return a parked container to its former name: derived -> reverted, original -> backup.
        if probe("inspect", aside):
            try:
                home = reverted if source_label(json.loads(docker("inspect", aside))[0]) else backup
                if not probe("inspect", home):
                    probe("rename", aside, home)
            except Exception:  # noqa: S110 - best effort; the original error is re-raised
                pass


def promotion_policy(held, name, docker, probe):
    """Policy for the original about to run as `name`; keeps every parked derived off."""
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
        set_restart(probe, "no", parked_name)  # Never auto-start beside the API.
    return policy


def finish_revert(name, backup, docker, probe, health):
    """Complete a revert killed after `rename name -> reverted`: promote the original."""
    held = json.loads(docker("inspect", backup))[0]  # Raises when the backup is missing.
    if source_label(held) is not None:
        docker("inspect", name)  # A labelled backup is a derived: not ours to promote; raise.
    policy = promotion_policy(held, name, docker, probe)
    docker("rename", backup, name)
    set_restart(probe, policy, name)
    docker("start", name)
    health(name, pages=False)  # On failure the original stays running.
    return "Original API container restored"


def start_restored(current, name, docker, probe, health):
    """Complete a revert killed after `rename backup -> name`: the original is not running."""
    policy = promotion_policy(current, name, docker, probe)
    set_restart(probe, policy, name)
    docker("start", name)
    health(name, pages=False)
    return "Original API container restored"


def revert(current, payload, backup, docker, probe, health, now):
    name = payload["container"]
    if current is None:
        return finish_revert(name, backup, docker, probe, health)
    if not probe("inspect", backup):
        if source_label(current) is None:
            if not current["State"].get("Running"):
                return start_restored(current, name, docker, probe, health)
            return "Original API container already restored"
        docker("inspect", backup)  # Fail before any change when no rollback container exists.
    # Any source label marks our derived container, including one built from older source.
    if source_label(current) is None:
        raise ValueError("API changed; refusing rollback")
    reverted = name + "-pages-reverted-" + str(int(now()))
    policy = restart_arg(current)  # Live (derived) policy; the backup's own inspect shows `no`.
    done = []  # Completed swap steps, so recovery undoes only what happened.
    try:
        done.append("stop")
        docker("stop", name)
        docker("rename", name, reverted)
        set_restart(probe, "no", reverted)
        done.append("derived")
        docker("rename", backup, name)
        done.append("original")
        set_restart(probe, policy, name)
        docker("start", name)
        health(name, pages=False)
    except BaseException as error:
        mask = hold_sigterm()  # A SIGTERM now waits until the recovery is done.
        try:
            try:
                if "original" in done:
                    docker("stop", name)
                    docker("rename", name, backup)
                    set_restart(probe, "no", backup)
                if "derived" in done:
                    docker("rename", reverted, name)
                set_restart(probe, policy, name)
                docker("start", name)
            except Exception:
                restore_any(name, backup, reverted, probe, docker, policy)
                raise error  # Recovery error stays as __context__.
        finally:
            release_sigterm(mask)
        raise
    return "Original API container restored; derived container retained stopped" + retained_note(
        name, docker
    )


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
        set_restart(probe, "no", failed)
    else:
        raise refuse
    docker("rename", backup, name)
    set_restart(probe, policy, name)
    docker("start", name)
    health(name, pages=False)


def derive(base_container, payload, docker):
    """Build the derived image from base_container's image; return the create body for `name`."""
    base = json.loads(docker("image", "inspect", base_container["Image"]))[0]
    if not base.get("RepoDigests"):
        raise ValueError("Running image has no immutable registry digest")
    digest = payload["digest"]
    image = build_image(base["RepoDigests"][0], payload["source"].encode(), digest, docker)
    namespace = {"NEUTRAL_PAGES_LABEL": NEUTRAL_PAGES_LABEL}  # the shipped replacement reads this
    exec(payload["replacement"], namespace)  # noqa: S102 - source shipped by main()
    return namespace["replacement"](base_container, image, payload["live_url"], digest)


def apply(current, payload, backup, docker, probe, create, health, now):
    name, digest = payload["container"], payload["digest"]
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
    body = derive(current, payload, docker)
    policy = restart_arg(current)  # Read before retiring; restored when promoted back.
    try:
        docker("stop", name)
        docker("rename", name, backup)
    except BaseException:
        probe("start", name)  # Never leave the live API stopped; its policy is unchanged.
        raise
    set_restart(probe, "no", backup)  # After the rename: a failed rename keeps the policy.
    try:
        create(name, body)
        docker("start", name)
        health(name)
    except BaseException:
        mask = hold_sigterm()  # A SIGTERM now waits until the rollback is done.
        try:
            if probe("inspect", name):
                probe("stop", name)
                failed = failed_name(name, probe, now)
                docker("rename", name, failed)
                set_restart(probe, "no", failed)
            docker("rename", backup, name)
            set_restart(probe, policy, name)
            docker("start", name)
        finally:
            release_sigterm(mask)
        raise
    return (
        "Pages derived image applied; API healthy; original container retained for rollback"
        + retained_note(name, docker)
        + "; run revert before any Compose or orchestrator update of this service"
    )


def build(payload, docker):
    """Build the derived image from the upstream backend image; never touch a container."""
    base = payload.get("backend_image")
    if not base:
        worker = json.loads(docker("inspect", payload["worker"]))[0]["Config"]["Image"]
        tag = worker.rsplit("/", 1)[-1].partition("@")[0].partition(":")[2]
        if not tag:
            raise ValueError("Worker image has no tag; set PLANE_BACKEND_IMAGE")
        base = "makeplane/plane-backend:" + tag
    try:
        digests = json.loads(docker("image", "inspect", base))[0].get("RepoDigests")
    except subprocess.CalledProcessError:
        digests = None
    if not digests:
        raise ValueError("Upstream image missing or has no registry digest")
    image = build_image(digests[0], payload["source"].encode(), payload["digest"], docker)
    return image + " " + payload["digest"]


def deploy(payload, docker, probe, create, health, now):
    """Run one operation against the Docker host; return the operator message.

    docker(*args, **kw) -> bytes raises on failure; probe(*args) -> bool never raises;
    create(name, body) raises unless created; health(name, pages=True) raises unless healthy
    (pages=False when the upstream original, which has no Pages route, is promoted).
    """
    operation = payload["operation"]
    if operation == "build":
        return build(payload, docker)
    name = payload["container"]
    backup = backup_name(name)
    missing = None
    listed = listed_names(name, docker)
    if name in listed:
        current = json.loads(docker("inspect", name))[0]  # A listed container's failure raises.
    else:
        current, missing = None, subprocess.CalledProcessError(1, ["docker", "inspect", name])
    # Tripwire: refuse legacy-named state before any other step, whatever the operation.
    found = legacy_state(current, name, listed, docker)
    if found:
        raise ValueError(refusal(found))
    if operation == "prune":
        return prune(name, docker, probe, payload.get("confirm", False))
    if missing is not None:
        # A missing API with a rollback container is a half-finished apply or revert.
        if operation not in ("apply", "revert") or not probe("inspect", backup):
            raise missing
    if operation == "check":
        return check(current, payload, health)
    if operation == "revert":
        return revert(current, payload, backup, docker, probe, health, now)
    if operation != "apply":
        raise ValueError("Unknown operation")
    return apply(current, payload, backup, docker, probe, create, health, now)


REMOTE = program(
    ["http.client", "io", "json", "re", "socket", "subprocess", "sys", "tarfile", "time"],
    [
        docker,
        docker_ok,
        backup_name,
        refusal,
        listed_names,
        legacy_state,
        restart_arg,
        set_restart,
        source_label,
        healthy,
        build_context,
        docker_create,
        build_image,
        hold_sigterm,
        release_sigterm,
        retained_names,
        retained_note,
        prune,
    ]
    + [
        check,
        restore_any,
        held_policy,
        promotion_policy,
        finish_revert,
        start_restored,
        revert,
        failed_name,
        reconcile,
        derive,
        apply,
        build,
        deploy,
    ],
    guarded(
        "say(deploy(json.load(sys.stdin), docker, docker_ok, docker_create,\n"
        "    lambda name, pages=True: healthy(\n"
        "        name, lambda *a: docker_ok(*a, timeout=20), time.sleep, pages=pages\n"
        "    ), time.time))\n"
    ),
    PAGES,
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
    source = Path(__file__).with_name("pages_backend.py").read_text()
    digest = hashlib.sha256(source.encode()).hexdigest()
    if operation == "build":
        (host,) = require_env("PLANE_MCP_SSH")
        worker = os.environ.get("PLANE_WORKER_CONTAINER") or "plane-worker"
        image = os.environ.get("PLANE_BACKEND_IMAGE") or None
        if host.startswith("-") or not worker.replace("-", "").replace("_", "").isalnum():
            raise ValueError("Invalid deployment destination")
        if image and (image.startswith("-") or any(c.isspace() for c in image)):
            raise ValueError("Invalid PLANE_BACKEND_IMAGE")
        payload = dict(operation=operation, worker=worker, backend_image=image, source=source)
        payload["digest"] = digest
        run_remote(host, REMOTE, payload)
        return
    if operation == "prune":
        host, container = require_env("PLANE_MCP_SSH", "PLANE_API_CONTAINER")
        if host.startswith("-") or not container.replace("-", "").replace("_", "").isalnum():
            raise ValueError("Invalid deployment destination")
        confirm = os.environ.get("PLANE_PAGES_PRUNE") == "yes"
        run_remote(host, REMOTE, dict(operation=operation, container=container, confirm=confirm))
        return
    host, container, live_url = require_env(
        "PLANE_MCP_SSH", "PLANE_API_CONTAINER", "PLANE_LIVE_URL"
    )
    if not valid_live_url(live_url):
        sys.exit("Invalid PLANE_LIVE_URL")
    if host.startswith("-") or not container.replace("-", "").replace("_", "").isalnum():
        raise ValueError("Invalid deployment destination")
    payload = dict(
        operation=operation,
        container=container,
        live_url=live_url,
        source=source,
        digest=digest,
        replacement="import copy\n" + inspect.getsource(replacement),
    )
    run_remote(host, REMOTE, payload)


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
