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
import urllib.parse
from pathlib import Path

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


def answer(probe, *args):
    """`probe(*args)`, or None when Docker did not answer in time (the probe raised
    RuntimeError). None is an unknown result: never read it as "absent" or as success."""
    try:
        return probe(*args)
    except RuntimeError:
        return None


def set_restart(probe, policy, container):
    """`docker update --restart`; say so on stderr when it fails or times out, since a retained
    container that keeps its old policy could start beside the live API."""
    if not answer(probe, "update", "--restart=" + policy, container):
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
        # A probe that times out is not healthy yet; it is retried within the budget.
        if answer(probe, "exec", name, "python", "-c", api) and (
            not pages or answer(probe, "exec", name, "python", "-c", route)
        ):
            return
        if clock() - start >= budget:
            break
        sleep(1)
    raise ValueError("API health check failed")


def dockerfile(base_image):
    """Dockerfile text deriving the running image; no credentials or runtime environment."""
    return (
        "FROM " + base_image + "\nCOPY plane_pages.py " + NEUTRAL_PAGES_BACKEND_FILE + "\n"
        "RUN python -c \"from pathlib import Path; p=Path('/code/plane/api/urls/__init__.py'); "
        "p.write_text(p.read_text()+"
        "'\\nfrom "
        + NEUTRAL_PAGES_BACKEND_MODULE
        + " import routes\\nurlpatterns += routes()\\n')\"\n"
        "RUN python -m py_compile " + NEUTRAL_PAGES_BACKEND_FILE + "\n"
    ).encode()


def build_context(base_image, source):
    """Return a tar build context deriving the exact running image."""
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for filename, content in [
            ("Dockerfile", dockerfile(base_image)),
            ("plane_pages.py", source),
        ]:
            info = tarfile.TarInfo(filename)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return stream.getvalue()


def validate_container_name(name):
    """Return `name` when it is a plain Docker container name; raise otherwise.

    Names reach docker as operands, in `--filter name=` and in the create URL, so a leading dash,
    Unicode or any other unusual character is refused (Docker's own name rule).
    """
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        raise ValueError("Invalid deployment destination")
    return name


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
            "/containers/create?name=" + urllib.parse.quote(name, safe=""),
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
    docker's stderr (last 5 lines, 200 characters each, with the base image reference, its
    repository path and any `lookup <host>` DNS host redacted): the build context holds no
    runtime configuration, so none can appear.
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
        # Docker echoes the base image reference; keep the registry host and repository path out
        # of the message, longest first so the shorter ones do not split a longer match.
        repository = base.partition("@")[0]
        for reference in (
            base,
            repository,
            repository.partition("/")[2],
            repository.partition("/")[0],
        ):
            text = text.replace(reference, "<base image>") if reference else text
        # A DNS failure names the host actually dialled, which may differ from the reference.
        text = re.sub(r"\blookup \S+", "lookup <host>", text)
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


def kept_note(newest):
    """`; kept newest reverted container: ...` for prune, or '' when there is none."""
    return "; kept newest reverted container (revert recovery): " + newest if newest else ""


def prune(name, docker, probe, confirm=False):
    """Remove (or, without `confirm`, list) stopped retired derived containers of `name`.

    Only a container that carries the Pages source label and is named exactly
    `<name>-pages-(reverted|failed)-<digits>[-<digits>...]` qualifies: never the live API, the
    rollback container, a running container, an `-aside` parking name or anything unlabelled
    (an unlabelled one may be the upstream original). The newest `-pages-reverted-` one is also
    kept, labelled or not, and a container that vanishes before its inspect is skipped.
    """
    exact = re.compile(re.escape(name) + r"-pages-(reverted|failed)-\d+(-\d+)*")
    candidates = [n for n in retained_names(name, docker) if exact.fullmatch(n)]
    # The newest parked `-pages-reverted-` container is revert recovery evidence: `finish_revert`
    # reads the live restart policy from it and `interrupted_revert` takes its presence as proof
    # a revert died. Removing it could strand a stopped API, so it is never a candidate.
    reverted = [n for n in candidates if n.startswith(name + "-pages-reverted-")]
    newest = reverted[-1] if reverted else None
    eligible, running = [], []
    for container in candidates:
        if container == newest:
            continue
        try:
            held = json.loads(docker("inspect", container))[0]
        except subprocess.CalledProcessError:
            continue  # Removed since the listing: nothing left to prune.
        if source_label(held) is None:
            continue
        (running if held["State"].get("Running") else eligible).append(container)
    if not eligible and not running:
        return "No retired containers to prune" + kept_note(newest)
    parts = []
    if not confirm:
        parts.append("Dry run, nothing removed. Would remove: " + (", ".join(eligible) or "none"))
    else:
        removed = [c for c in eligible if answer(probe, "rm", c)]  # a timeout: not removed
        stuck = [c for c in eligible if c not in removed]
        parts.append("Pruned: " + ", ".join(removed) if removed else "Nothing pruned")
        if stuck:
            parts.append("could not remove: " + ", ".join(stuck))
    if running:
        parts.append("kept running: " + ", ".join(running))
    if not confirm and eligible:
        parts.append("set PLANE_PAGES_PRUNE=yes to remove them")
    return "; ".join(parts) + kept_note(newest)


def check(current, payload, health):
    if source_label(current) != payload["digest"]:
        raise ValueError("Pages image does not match source")
    health(payload["container"])
    return "Pages image and API health verified (digest " + payload["digest"] + ")"


def sigterm_held(action):
    """Run `action` with SIGTERM held; a deferred SIGTERM lands after it, the report kept.

    The one place apply's retire step blocks SIGTERM: a signal between its rename and its
    bookkeeping would otherwise leave the API stopped under the backup name."""
    mask = hold_sigterm()
    failure = None
    try:
        action()
    except BaseException as caught:
        failure = caught
        raise
    finally:
        release_sigterm(mask, failure)


def recover_swap(mask, error, undo, fallback=None):
    """The recovery half shared by apply and revert: undo a failed swap with SIGTERM held.

    Call it inside the `except BaseException as error` of the swap, then re-raise. The first
    statement of that handler is `mask = hold_sigterm()`, so nothing, not even building the
    `undo` and `fallback` callables, runs unprotected; this releases `mask`. `undo`
    puts the containers back. When `undo` raises an Exception and a `fallback` is given, the
    fallback (apply: recover_original; revert: restore_any) runs and `error` is raised, with
    the recovery error as its __context__. A SIGTERM that arrives meanwhile waits until the
    recovery is done (release_sigterm reports `error` first)."""
    try:
        if fallback is None:
            undo()
        else:
            try:
                undo()
            except Exception:
                fallback()
                raise error
    finally:
        release_sigterm(mask, error)


def restore_any(name, backup, reverted, probe, docker, policy):
    """Best effort after failed recovery: leave exactly one started `name` container.

    A retained container gets `--restart=no` only after its rename succeeds; a promoted one
    gets `policy`, the live deployment's policy read before any retirement (best effort).

    A probe that times out leaves the state unknown: nothing more is started, `name` is stopped
    (best effort) and "Rollback incomplete" names where the containers may be. Never raises a
    timeout, so the caller still raises its original error.
    """
    aside = reverted + "-aside"
    try:
        restore_any_steps(name, backup, reverted, aside, probe, policy)
    except RuntimeError:
        say(
            "Rollback incomplete: Docker did not answer in time; "
            + settle_unknown(name, probe, docker)[1]
            + "; the original and derived containers may be under "
            + ", ".join((name, reverted, backup, aside))
            + ". Inspect them, then run revert again.",
            sys.stderr,
        )
    finally:
        # Return a parked container to its former name: derived -> reverted, original -> backup.
        if answer(probe, "inspect", aside):
            try:
                home = reverted if source_label(json.loads(docker("inspect", aside))[0]) else backup
                if answer(probe, "inspect", home) is False:
                    answer(probe, "rename", aside, home)
            except Exception:  # noqa: S110 - best effort; the original error is re-raised
                pass


def restore_any_steps(name, backup, reverted, aside, probe, policy):
    """restore_any's cascade; a probe timeout raises RuntimeError out of it."""
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


def recover_original(name, backup, probe, docker, policy, now, parked=None):
    """Best effort after a failed apply rollback; never start the derived container.

    `restore_any` starts whatever holds `name`, which is right for revert but here could be the
    failed derived container. A container is started as `name` only when a successful inspect
    shows it is unlabelled (the original). A Pages-labelled one is stopped and moved aside; an
    unknown or stuck state leaves things stopped where possible and says where each container
    is. `parked` says where the rollback moved the stopped derived container, when it tried:
    `<name> (stopped)`, or `unknown, possibly ...` when that rename did not answer in time.
    A probe that times out is such an unknown state; this never raises it, so the caller still
    raises its original error.
    """
    # Where the derived container is, kept current by the steps so a timeout can say it.
    where = {"derived": parked}
    try:
        recover_original_steps(name, backup, probe, docker, policy, now, where)
    except RuntimeError:
        kind, held = settle_unknown(name, probe, docker)
        if kind == "original":
            recovery_incomplete(where["derived"] or "not under " + name, held)
        elif kind == "absent":  # the original never left its backup name, and stays stopped
            recovery_incomplete(where["derived"] or "unknown", backup + " (stopped)")
        elif kind == "derived":
            recovery_incomplete(held, "unknown, possibly " + backup)
        else:
            recovery_incomplete(
                where["derived"] or "unknown, possibly " + held,
                "unknown, possibly " + backup + " or " + held,
            )


def settle_unknown(name, probe, docker):
    """After a timeout: inspect `name` once more; return (kind, text) for the message.

    kind is "original" (unlabelled: left as it is, never stopped), "absent" (Docker said "No
    such"), "derived" or "unknown" (the inspect failed or timed out too). A derived or unknown holder is stopped, best effort.
    """
    try:
        held = json.loads(docker("inspect", name))[0]
    except subprocess.CalledProcessError as error:
        if b"No such" in (error.stderr or b""):  # Docker's answer for a missing container
            return "absent", name + " (absent)"
        held = None  # Any other daemon error says nothing about `name`
    except Exception:
        held = None
    if held is not None and source_label(held) is None:
        return "original", name + (" (running)" if held["State"].get("Running") else " (stopped)")
    stopped = answer(probe, "stop", name)
    text = name + (" (stopped)" if stopped else " (stop failed or timed out, may still be running)")
    return ("derived" if held is not None else "unknown"), text


def recovery_incomplete(derived, original):
    say(
        "Rollback incomplete: derived container "
        + derived
        + "; original "
        + original
        + ". Run apply again to reconcile, or inspect them.",
        sys.stderr,
    )


def recover_original_steps(name, backup, probe, docker, policy, now, where):
    """recover_original's steps; a probe timeout raises RuntimeError out of them. `where`
    ["derived"] says where the derived container is, when known (every container named
    stopped was stopped), and is updated before and after each move of it."""
    incomplete = recovery_incomplete
    derived = where["derived"]
    try:
        if name not in listed_names(name, docker):
            holder = None
        else:
            holder = json.loads(docker("inspect", name))[0]
    except Exception:
        # Whatever holds `name` is the derived container or unknown: stop it, never start it.
        held = name + (
            " (stopped)"
            if answer(probe, "stop", name)
            else " (stop failed or timed out, may still be running)"
        )
        incomplete(
            derived or "unknown, possibly " + held, "unknown, possibly " + backup + " or " + held
        )
        return
    if holder is not None and source_label(holder) is None:
        set_restart(probe, policy, name)  # The original already holds the name.
        if not probe("start", name):
            incomplete(derived or "not under " + name, name + " (stopped)")
        return
    if holder is not None:
        if not probe("stop", name):
            incomplete(name + " (stop failed, may still be running)", backup + " (stopped)")
            return
        failed = unused_name(name + "-pages-failed-", probe, now)
        where["derived"] = "unknown, possibly " + name + " or " + failed + " (stopped)"
        if not probe("rename", name, failed):
            incomplete(name + " (stopped)", backup + " (stopped)")
            return
        derived = where["derived"] = failed + " (stopped)"
        set_restart(probe, "no", failed)
    if not probe("rename", backup, name):
        incomplete(derived or "absent", backup + " (stopped)")
        return
    set_restart(probe, policy, name)
    if not probe("start", name):
        incomplete(derived or "absent", name + " (stopped)")


def promotion_policy(held, name, docker, probe):
    """Policy for the original about to run as `name`; keeps every parked derived off."""
    prefix = name + "-pages-reverted-"
    try:  # Optional lookup: a failing `ps` falls through to the default policy.
        listing = docker("ps", "-a", "--filter", "name=" + prefix, "--format", "{{.Names}}")
    except subprocess.CalledProcessError:
        listing = b""
    parked = sorted(n for n in listing.decode().split() if n.startswith(prefix))
    # Only the newest plain `<prefix><digits>[-<digits>...]` is the live derived; older ones may
    # be stale. A `-1` suffix marks a second revert in the same second; `-aside` is not plain.
    plain = [n for n in parked if re.fullmatch(r"\d+(-\d+)*", n[len(prefix) :])]
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


def interrupted_revert(name, docker):
    """True when a parked derived container shows a revert died before starting the original.

    Without one, a stopped `name` may be stopped on purpose and is left alone.
    """
    prefix = name + "-pages-reverted-"
    try:
        listing = docker("ps", "-a", "--filter", "name=" + prefix, "--format", "{{.Names}}")
    except subprocess.CalledProcessError:
        return False
    return any(n.startswith(prefix) for n in listing.decode().split())


def start_restored(current, name, docker, probe, health):
    """Complete a revert killed after `rename backup -> name`: the original is not running."""
    policy = promotion_policy(current, name, docker, probe)
    set_restart(probe, policy, name)
    docker("start", name)
    health(name, pages=False)
    return "Original API container restored"


def undo_revert(name, backup, reverted, done, policy, docker, probe):
    """Undo the completed steps of a failed revert swap, then start `name` again."""
    if "original" in done:
        docker("stop", name)
        docker("rename", name, backup)
        set_restart(probe, "no", backup)
    if "derived" in done:
        docker("rename", reverted, name)
    set_restart(probe, policy, name)
    docker("start", name)


def revert(current, payload, backup, docker, probe, health, now):
    name = payload["container"]
    if current is None:
        return finish_revert(name, backup, docker, probe, health)
    if not probe("inspect", backup):
        if source_label(current) is None:
            if not current["State"].get("Running") and interrupted_revert(name, docker):
                return start_restored(current, name, docker, probe, health)
            return "Original API container already restored"
        docker("inspect", backup)  # Fail before any change when no rollback container exists.
    # Any source label marks our derived container, including one built from older source.
    if source_label(current) is None:
        raise ValueError("API changed; refusing rollback")
    reverted = unused_name(name + "-pages-reverted-", probe, now)
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
        mask = hold_sigterm()  # First, so a SIGTERM now waits until the recovery is done.
        recover_swap(
            mask,
            error,
            lambda: undo_revert(name, backup, reverted, done, policy, docker, probe),
            lambda: restore_any(name, backup, reverted, probe, docker, policy),
        )
        raise
    return "Original API container restored; derived container retained stopped" + retained_note(
        name, docker
    )


def unused_name(prefix, probe, now):
    """Unused `<prefix><ts>` name, with `-1` appended until free.

    One run can retire two containers in the same second, and two reverts can share one. The
    suffixed names still match `<digits>(-<digits>)*` and sort after the plain one, so prune
    (newest last) and `promotion_policy` read them as newer.
    """
    candidate = prefix + str(int(now()))
    while probe("inspect", candidate):
        candidate += "-1"
    return candidate


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
        failed = unused_name(name + "-pages-failed-", probe, now)
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


def retire_original(name, backup, docker, probe, swap):
    """Apply's retire step: stop `name` and park it under `backup`, noting it in `swap`."""
    docker("stop", name)
    docker("rename", name, backup)
    swap["retired"] = True
    set_restart(probe, "no", backup)  # After the rename: a failed rename keeps the policy.


def undo_apply(name, backup, policy, docker, probe, now, swap):
    """Undo a failed apply swap: put the original back under `name`, started.

    `swap` says whether the original was retired and, once tried, where the stopped derived
    container went; this updates `parked` as it goes."""
    if not swap["retired"]:
        # Never leave the live API stopped; its policy is unchanged.
        started = answer(probe, "start", name)
        if started is None:
            recovery_incomplete("not created", name + " (start timed out, state unknown)")
        elif not started:
            # A retire rename that took effect and timed out leaves the original under
            # the backup name, so the start of `name` fails: name where it really is.
            held = answer(probe, "inspect", backup)
            if held is None:
                original = name + " or " + backup + " (state unknown)"
            else:
                original = (backup if held else name) + " (stopped)"
            recovery_incomplete("not created", original)
        return
    if probe("inspect", name):
        if not probe("stop", name):  # Never rename a running derived aside.
            raise subprocess.CalledProcessError(1, ["docker", "stop", name])
        failed = unused_name(name + "-pages-failed-", probe, now)
        # The rename can take effect and then time out: say both names.
        swap["parked"] = "unknown, possibly " + name + " or " + failed + " (stopped)"
        docker("rename", name, failed)
        swap["parked"] = failed + " (stopped)"
        set_restart(probe, "no", failed)
    docker("rename", backup, name)
    set_restart(probe, policy, name)
    docker("start", name)


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
    # retired: the original now sits under its backup name. parked: where the rollback moved the
    # stopped derived container, once it tried.
    swap = {"retired": False, "parked": None}
    try:
        # SIGTERM waits across the retire: a signal between the rename and `retired` or the
        # policy update would otherwise leave the API stopped under the backup name.
        sigterm_held(lambda: retire_original(name, backup, docker, probe, swap))
        create(name, body)
        docker("start", name)
        health(name)
    except BaseException as error:
        mask = hold_sigterm()  # First, so a SIGTERM now waits until the rollback is done.
        recover_swap(
            mask,
            error,
            lambda: undo_apply(name, backup, policy, docker, probe, now, swap),
            # Only a retired original has a fallback; a failed first stop just propagates.
            (lambda: recover_original(name, backup, probe, docker, policy, now, swap["parked"]))
            if swap["retired"]
            else None,
        )
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

    docker(*args, **kw) -> bytes raises on failure; probe(*args) -> bool is False only when
    the command ran and failed, and raises only when Docker does not answer in time;
    create(name, body) raises unless created; health(name, pages=True) raises unless healthy
    (pages=False when the upstream original, which has no Pages route, is promoted).
    """
    operation = payload["operation"]
    if operation == "build":
        validate_container_name(payload["worker"])
        return build(payload, docker)
    name = validate_container_name(payload["container"])
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
    [
        "http.client",
        "io",
        "json",
        "re",
        "socket",
        "subprocess",
        "sys",
        "tarfile",
        "time",
        "urllib.parse",
    ],
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
        dockerfile,
        build_context,
        docker_create,
        build_image,
        validate_container_name,
        hold_sigterm,
        release_sigterm,
        retained_names,
        retained_note,
        kept_note,
        prune,
    ]
    + [
        check,
        answer,
        sigterm_held,
        recover_swap,
        restore_any,
        restore_any_steps,
        recover_original,
        recovery_incomplete,
        settle_unknown,
        recover_original_steps,
        held_policy,
        promotion_policy,
        finish_revert,
        interrupted_revert,
        start_restored,
        undo_revert,
        revert,
        unused_name,
        reconcile,
        derive,
        retire_original,
        undo_apply,
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


def replacement_source():
    """Source of `replacement` as shipped to the host."""
    import inspect

    return "import copy\n" + inspect.getsource(replacement)


def shipped_digest(source):
    """Digest of everything that decides the derived image and container: the backend source,
    `replacement` (the create body) and the Dockerfile. Each part is length-prefixed."""
    digest = hashlib.sha256()
    for part in (source, replacement_source(), dockerfile("<base image>").decode()):
        data = part.encode()
        digest.update(str(len(data)).encode() + b":" + data)
    return digest.hexdigest()


def valid_live_url(url):
    """Return whether url is plain http(s) with a host and no credentials, query or fragment."""
    try:
        parsed = urllib.parse.urlsplit(url)
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
    operation = sys.argv[1]
    source = Path(__file__).with_name("pages_backend.py").read_text()
    digest = shipped_digest(source)
    if operation == "build":
        (host,) = require_env("PLANE_MCP_SSH")
        worker = os.environ.get("PLANE_WORKER_CONTAINER") or "plane-worker"
        image = os.environ.get("PLANE_BACKEND_IMAGE") or None
        if host.startswith("-"):
            raise ValueError("Invalid deployment destination")
        validate_container_name(worker)
        if image and (image.startswith("-") or any(c.isspace() for c in image)):
            raise ValueError("Invalid PLANE_BACKEND_IMAGE")
        payload = dict(operation=operation, worker=worker, backend_image=image, source=source)
        payload["digest"] = digest
        run_remote(host, REMOTE, payload)
        return
    if operation == "prune":
        host, container = require_env("PLANE_MCP_SSH", "PLANE_API_CONTAINER")
        if host.startswith("-"):
            raise ValueError("Invalid deployment destination")
        validate_container_name(container)
        confirm = os.environ.get("PLANE_PAGES_PRUNE") == "yes"
        run_remote(host, REMOTE, dict(operation=operation, container=container, confirm=confirm))
        return
    host, container, live_url = require_env(
        "PLANE_MCP_SSH", "PLANE_API_CONTAINER", "PLANE_LIVE_URL"
    )
    if not valid_live_url(live_url):
        sys.exit("Invalid PLANE_LIVE_URL")
    if host.startswith("-"):
        raise ValueError("Invalid deployment destination")
    validate_container_name(container)
    payload = dict(
        operation=operation,
        container=container,
        live_url=live_url,
        source=source,
        digest=digest,
        replacement=replacement_source(),
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
