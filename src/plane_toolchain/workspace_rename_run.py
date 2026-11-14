"""Shared run context and step runner of apply and revert (shipped to the host)."""

import os
import pathlib

from plane_toolchain.remote import say
from plane_toolchain.workspace_rename_docker import (
    inspect_services,
    refuse_api_service,
    service_names,
    start_services,
    stop_services,
)
from plane_toolchain.workspace_rename_receipt import advance, receipt_dir
from plane_toolchain.workspace_rename_snippet import (
    SNIPPET_IDLE_SECONDS,
    slug_state,
    snippet_ok,
    sweep_orphans,
)


def seam_uid(seams):
    """The uid that must own the receipt files: the `uid` seam, else this process's euid."""
    return os.geteuid() if seams.get("uid") is None else seams["uid"]


def run_directory(data):
    """The receipt directory of a payload: `receipt_dir`, else the environment default."""
    return pathlib.Path(data.get("receipt_dir") or receipt_dir(os.environ))


def reason_of(failure):
    """The operator text of a failure: a RuntimeError's message, else only its type name."""
    return str(failure) if isinstance(failure, RuntimeError) else type(failure).__name__


def refused(failure):
    """A preflight RuntimeError as a refusal: its message, prefixed `Refused: ` once."""
    text = str(failure)
    return RuntimeError(text if text.startswith("Refused") else "Refused: " + text)


def warnings_text(notes):
    """The ` Warning: <note>.` tail of a failure message, one per note."""
    return "".join(" Warning: " + note + "." for note in notes)


def drain_state(reason):
    """The service state after a failed drain gate, or None when `reason` is not one."""
    if "and the workers are stopped" in reason:  # fast-fail drain gate
        return "Services were left as they are (workers stopped, jobs kept)"
    if "jobs still queued after" in reason:  # 60 s drain timeout, worker was running
        return "Beat is stopped and the other services were left as they are (jobs kept)"
    return None


def snippet_idle(data):
    """The run's silence deadline: a valid `snippet_idle_seconds` in the payload, else the default."""
    idle = data.get("snippet_idle_seconds")
    if isinstance(idle, (int, float)) and not isinstance(idle, bool) and 0 < idle <= 86400:
        return idle
    return SNIPPET_IDLE_SECONDS


def run_context(data, seams, operations=None):
    """What check, apply and revert share for one run, as a dict: the seams (docker, popen,
    clock, crash, uid, and seams itself for the lazily read sleep) and the payload (operation,
    old, new, api, snippet, directory, names, options, undrained, stale_binaries).

    run_apply and run_revert each build one, use the run_* helpers below for the snippet call,
    the service stop and start and the step machine, and keep only their own steps and failure
    messages. Seams are read before the operation check, and the payload after it. (A dict and
    plain functions, not a class: program() ships function source only.)"""
    run = {
        "seams": seams,
        "docker": seams["docker"],
        "popen": seams["popen"],
        "clock": seams["clock"],
        "crash": seams.get("crash") or (lambda label: None),
        "uid": seam_uid(seams),
        "operation": data.get("operation"),
    }
    if operations is not None and run["operation"] not in operations:
        raise RuntimeError("expected check, apply or revert")
    run["old"], run["new"] = data["from"], data["to"]
    run["api"], run["snippet"] = data["api"], data["snippet"]
    run["directory"] = run_directory(data)
    run["names"] = service_names(data.get("services") or {})
    run["options"] = {key: data[key] for key in ("live_url", "api_url") if data.get(key)}
    run["idle"] = snippet_idle(data)
    run["undrained"] = data.get("allow_undrained") is True
    run["stale_binaries"] = data.get("allow_stale_binaries") is True
    return run


def stale_binary_gate(run, summary):
    """Refuse a check whose summary counts `not_rebuilt` rows unless the payload allows it.

    step_slug rewrites Description rows without Live, so their stored editor binary keeps the
    old slug, and a later editor save can bring it back. With `allow_stale_binaries` the run
    goes on and says so."""
    count = summary.get("not_rebuilt") or 0
    if not count:
        return
    text = (
        f"Description editor binaries not rebuilt: {count}; they keep the old slug and a later "
        "editor save can bring it back"
    )
    if not run["stale_binaries"]:
        raise RuntimeError(text + ". Set PLANE_RENAME_ALLOW_STALE_BINARIES=1 to accept that")
    say("Warning: " + text + " (accepted by PLANE_RENAME_ALLOW_STALE_BINARIES)")


def run_call(run, op, args):
    """Run one snippet operation; return its summary. No summary or ok:false raises."""
    return snippet_ok(
        run["api"],
        run["snippet"],
        op,
        args,
        run["directory"],
        run["popen"],
        run["uid"],
        run["names"].values(),
        run["idle"],
    )


def run_health(run, old, new, workspace_id):
    """Health with `old` gone and `new` held by `workspace_id` (and the payload URLs)."""
    args = {"old": old, "new": new, "workspace_id": workspace_id, **run["options"]}
    return run_call(run, "health", args)


def run_slug_state(run, workspace_id):
    """slug_state (a read-only probe) for this run's rename."""
    return slug_state(
        run["api"],
        run["snippet"],
        run["old"],
        run["new"],
        workspace_id,
        run["directory"],
        run["popen"],
        run["uid"],
        idle=run["idle"],
    )


def run_preflight(run, operation):
    """The read-only checks every operation starts with; `operation` picks the sweep."""
    refuse_api_service(run["names"], run["api"])
    ids = inspect_services(run["names"], run["api"], run["docker"])
    refuse_api_service(run["names"], run["api"], ids)
    sweep_orphans(run["api"], run["popen"], operation, run["directory"])


def run_stop(run):
    """Drain and stop the services; an unverified drain raises."""
    sleep = run["seams"]["sleep"]
    remaining = stop_services(
        run["names"], run["docker"], run["clock"], sleep, run["crash"], run["undrained"]
    )
    if remaining is None:  # wrong or empty vhost: the drain was never verified
        raise RuntimeError("celery queue not found; drain not verified")


def run_start(run):
    start_services(run["names"], run["docker"], run["crash"])


def run_start_noting(run, notes):
    """Start the services; a failure is added to `notes` instead of raised."""
    try:
        run_start(run)
    except Exception as problem:  # noqa: BLE001 - report it with the original failure
        notes.append(reason_of(problem) + "; start the remaining services by hand")


def run_steps(run, order, step, effect, recover, settle=None):
    """Run every step after `step` (None: from the first) up to the last of `order`.

    Each step is crash("before_<step>"), effect, crash("after_<step>"), settle (if any), then
    advance. An Exception goes to recover(step, target, failure), with `step` the last step
    reached; it raises, or returns once it has advanced the receipt itself. Killed and other
    BaseExceptions propagate untouched."""
    crash = run["crash"]
    while step != order[-1]:
        target = order[0] if step is None else order[order.index(step) + 1]
        crash("before_" + target)
        try:
            effect(target)
            crash("after_" + target)
            if settle is not None:
                settle()
            advance(run["directory"], target, run["uid"])
        except Exception as failure:
            recover(step, target, failure)
        step = target


SHIPPED = (
    seam_uid,
    run_directory,
    reason_of,
    refused,
    warnings_text,
    drain_state,
    run_context,
    stale_binary_gate,
    run_call,
    run_health,
    run_slug_state,
    snippet_idle,
    run_preflight,
    run_stop,
    run_start,
    run_start_noting,
    run_steps,
)
