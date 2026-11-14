"""The apply state machine of the workspace rename (shipped to the host)."""

import json
import os

from plane_toolchain.remote import say
from plane_toolchain.workspace_rename_docker import backup_database, detect_saltbox
from plane_toolchain.workspace_rename_receipt import (
    advance,
    finish_rotation,
    journal_workspace,
    leftover_files,
    load_journal,
    load_receipt,
    new_receipt,
    reset_receipt,
    set_receipt_field,
    sha256_file,
    step_order,
    verify_backup,
)
from plane_toolchain.workspace_rename_run import (
    drain_state,
    reason_of,
    refused,
    run_call,
    run_context,
    run_health,
    run_preflight,
    run_slug_state,
    run_start,
    run_start_noting,
    run_steps,
    run_stop,
    stale_binary_gate,
    warnings_text,
)
from plane_toolchain.workspace_rename_snippet import is_stuck


def apply_stop(run):
    run_stop(run)


def apply_backup(run):
    directory, uid, names = run["directory"], run["uid"], run["names"]
    dump = directory / "backup.dump"
    if run["backup_action"] == "adopt" and os.path.lexists(dump):
        set_receipt_field(directory, "backup_sha256", sha256_file(dump), uid)
        say("Adopted backup.dump with no recorded SHA-256 (crash after the dump)")
    elif os.path.lexists(dump):
        say("Backup already taken; SHA-256 matches")
    else:
        backup_database(
            names["db"],
            directory,
            run["popen"],
            run["crash"],
            uid,
            hide=(*names.values(), run["api"]),
        )
        say("Database backed up")


def apply_slug(run):
    args = {"old": run["old"], "new": run["new"]}
    workspace_id = run["workspace_id"]
    if workspace_id:
        args["workspace_id"] = workspace_id
    summary = run_call(run, "step_slug", args)
    renamed = summary.get("workspace_id")
    journalled = journal_workspace(load_journal(run["directory"], run["uid"]))
    if not renamed or journalled != renamed or workspace_id not in (None, renamed):
        raise RuntimeError("slug step workspace does not match the journal")
    run["workspace_id"] = renamed


def apply_rows(run):
    args = {
        "old": run["old"],
        "new": run["new"],
        "workspace_id": run["workspace_id"],
        **run["options"],
    }
    run_call(run, "step_rows", args)


def apply_cache(run):
    run_call(run, "flush_cache", {})


def apply_start(run):
    run_start(run)


def apply_health(run):
    run_health(run, run["old"], run["new"], run["workspace_id"])


def snippet_stuck(at, reason):
    """A snippet process that could not be confirmed stopped; never returns."""
    raise RuntimeError(
        f"Rename stopped at {at}: {reason}. Services were not restarted and the receipt was "
        "not reset. Stop the snippet process in the API container by hand (its environment "
        "has PLANE_RENAME_RUN), then rerun apply to resume or run revert."
    )


def pre_slug_failure(run, step, target, reason):
    """Failure policy before the slug change: restart services, reset the receipt.

    Never returns."""
    notes = []
    run_start_noting(run, notes)
    try:
        reset_receipt(run["directory"], run["clock"], run["uid"], run["crash"])
    except Exception as problem:  # noqa: BLE001
        notes.append("receipt reset failed: " + reason_of(problem))
    restarted = "Services restarted" if not notes else "Services not all restarted"
    raise RuntimeError(
        f"Rename failed before the slug change ({target}): {reason}. {restarted}; no data "
        "changed. The receipt is back at pending and the backup was set aside, so the next "
        "apply stops services and takes a fresh backup. Fix the cause and rerun apply."
        + warnings_text(notes)
    )


def post_slug_failure(run, step, target, reason):
    """Failure policy after the slug change: services and receipt stay as they are.

    Nothing is rolled back. Never returns."""
    if target == "complete":
        raise RuntimeError(
            f"Rename health check failed: {reason}. Receipt left at started; services "
            "are running. Investigate, then rerun apply to recheck, or run revert."
        )
    if target == "started":
        state = "Some services did not start"
    elif drain_state(reason) is not None:
        state = drain_state(reason)
    elif target == step:  # stopping services again on resume failed
        state = "Some services may still be running; stop them by hand"
    else:
        state = "Services stay stopped"
    moving = "" if target == step else f" (moving to {target})"
    raise RuntimeError(
        f"Rename stopped at {step}{moving}: {reason}. {state}. Rerun apply to resume, "
        "or run revert; nothing is rolled back automatically."
    )


def slug_unclear(run, step, target, reason):
    """Failure policy for step_slug: probe read-only, then continue, reset or stop.

    Returns only when NEW is held by the journalled workspace; the receipt is then advanced."""
    directory, uid = run["directory"], run["uid"]
    journalled = journal_workspace(load_journal(directory, uid))
    state = run_slug_state(run, journalled)
    if state == "original":
        pre_slug_failure(run, step, target, reason)
    if state != "renamed":
        raise RuntimeError(
            f"Rename slug step outcome unknown: {reason}. Services stay stopped; the "
            "receipt is at backed_up. Check which workspace holds each slug, then rerun "
            "apply or run revert."
        ) from None
    say("Slug step unclear, but NEW is held by the journalled workspace; continuing")
    run["workspace_id"] = journalled
    advance(directory, target, uid)


def apply_steps():
    """The apply step table: step -> (effect, failure policy).

    A policy is called as policy(run, step, target, reason), `step` being the last step
    reached. It raises, except slug_unclear, which returns once it has advanced the receipt.
    Steps before the slug change reset; steps after it leave the receipt and the services."""
    return {
        "stopped": (apply_stop, pre_slug_failure),
        "backed_up": (apply_backup, pre_slug_failure),
        "slug_done": (apply_slug, slug_unclear),
        "rows_done": (apply_rows, post_slug_failure),
        "cache_done": (apply_cache, post_slug_failure),
        "started": (apply_start, post_slug_failure),
        "complete": (apply_health, post_slug_failure),
    }


def apply_recover(run, step, target, failure):
    """run_steps' recovery for apply: a stuck snippet stops everything, else the step's policy."""
    reason = reason_of(failure)
    if is_stuck(reason):
        snippet_stuck(step, reason)
    apply_steps()[target][1](run, step, target, reason)


def any_running(run):
    """True when a service runs, False when none does, None when inspect cannot say."""
    try:
        for key in ("beat", "worker", "web", "space", "admin"):
            state = json.loads(run["docker"]("inspect", run["names"][key]))[0].get("State") or {}
            if state.get("Running"):
                return True
    except Exception:  # noqa: BLE001 - an unreadable state is unknown, never "stopped"
        return None
    return False


def slug_reached(step):
    """True when `step` is slug_done or later in the apply order."""
    order = step_order("apply")
    return step in order and order.index(step) >= order.index("slug_done")


def resume_stop(run, step, slug_committed):
    """On resume between stopped and cache_done: stop the services again; return the step.

    Services that ran again since a kill, before the slug change, void the backup: the receipt
    is reset and the step is pending. When inspect cannot say whether they run, refuse and keep
    the backup: rotating a good backup on a guess would force a new dump for nothing."""
    running = False if slug_committed or slug_reached(step) else any_running(run)
    if running is None:
        refusal = RuntimeError(
            f"Rename resume refused at {step}: cannot tell whether the services run (docker "
            "inspect failed). Nothing was reset and the backup was kept. Check Docker, then rerun apply."
        )
        refusal.unknown_services = True
        raise refusal
    if running:
        say("Services ran again since the kill; setting the backup aside for a fresh one")
        reset_receipt(run["directory"], run["clock"], run["uid"], run["crash"])
        run["workspace_id"], run["backup_action"] = None, None
        return "pending"
    say("Making sure services are stopped before resuming")
    run_stop(run)
    return step


def resume_failed(run, step, reason):
    """Failure policy for the stop on resume; never returns."""
    if is_stuck(reason):
        snippet_stuck(step, reason)
    if slug_reached(step):
        post_slug_failure(run, step, step, reason)
    workspace_id = run["workspace_id"]
    if workspace_id is not None:  # a slug step may have committed: never guess
        state = run_slug_state(run, workspace_id)
        if state == "renamed":
            post_slug_failure(run, step, step, reason)
        if state != "original":
            raise RuntimeError(
                f"Rename resume failed at {step}: {reason}; slug state unknown. Services "
                "were left as they are and nothing was reset. Check which workspace holds "
                "each slug and which services run, then rerun apply or run revert."
            ) from None
    pre_slug_failure(run, step, step, reason)


def run_apply(data, seams):
    """Run `check` (read-only), `apply` or `revert` (both resumable) for one workspace rename.

    data: operation, from, to, api (container), snippet (source), optional receipt_dir,
    services (PLANE_*_CONTAINER overrides), live_url and api_url. seams: docker, popen, clock,
    sleep, optional crash(label) and uid. Returns the operator message; raises RuntimeError.

    Apply writes the pending receipt first, then runs each step as effect, then advance:
    pending, stopped, backed_up, slug_done, rows_done, cache_done, started, complete. Every
    refusal comes before the first stop. A resume between stopped and cache_done stops the
    services again first (idempotent). A failure before the slug change restarts the services
    and resets the receipt to pending with the backup and journal rotated, so the next apply
    stops again and takes a fresh dump; a kill keeps the receipt, and its matching backup is
    reused unless a service ran again before the slug change (then it is rotated too). A
    failure to stop again on resume after a possible slug commit is probed, never reset. An
    unclear step_slug is probed read-only: renamed continues, original is a pre-slug failure,
    anything else stops with services down. After the slug change, a failure stops with
    services down for the operator to resume (apply) or revert. There is no automatic rollback.

    Journal durability: the snippet waits for this process's `ack` (sent after the fsync) before
    each write a journal line covers (see run_snippet), so a kill of this process between the
    print and the fsync leaves that row unwritten, never unjournalled.
    """
    run = run_context(data, seams, ("check", "apply"))
    operation, old, new = run["operation"], run["old"], run["new"]
    directory, uid, names = run["directory"], run["uid"], run["names"]
    docker = run["docker"]
    order = step_order("apply")

    def at_or_past(step, mark):
        return step in order and order.index(step) >= order.index(mark)

    # Preflight: read-only, and it writes nothing (not even the receipt directory) for check.
    try:
        run_preflight(run, operation)
        receipt = None
        if operation == "apply" or os.path.lexists(directory):
            receipt = load_receipt(directory, old, new, uid)
        if receipt is not None and receipt.get("phase") != "apply":
            if receipt.get("step") == "reverted":
                raise RuntimeError(
                    "this rename was reverted and the revert is complete; archive or move the "
                    "receipt directory before a new apply"
                )
            raise RuntimeError("receipt is in the revert phase; run revert")
        if receipt is None and os.path.lexists(directory):
            leftover = leftover_files(directory)
            if leftover:
                raise RuntimeError(
                    "found " + ", ".join(leftover) + " without a receipt: a journal or backup "
                    "from an earlier rename; move them or the receipt directory aside before a "
                    "new apply"
                )
        step = receipt["step"] if receipt else None
        records = load_journal(directory, uid) if receipt else []
        workspace_id = journal_workspace(records)
        if step == "complete":
            if workspace_id is None:
                raise RuntimeError("journal has no workspace record; refusing")
            run_health(run, old, new, workspace_id)
            return (
                "Rename already complete and healthy. To run another rename (including the "
                "opposite one), archive the receipt directory first: move it aside whole, "
                "keeping backup.dump, records.jsonl and any rotated files; never delete them"
            )
        backup_action = verify_backup(directory, receipt, uid)
        if workspace_id is None and at_or_past(step, "slug_done"):
            raise RuntimeError("journal has no workspace record; refusing")
        # The full check runs unless the slug step is done: at or past slug_done, or at
        # backed_up when a probe shows NEW held by the journalled workspace (kill after commit).
        slug_committed = at_or_past(step, "slug_done") or (
            step == "backed_up"
            and workspace_id is not None
            and run_slug_state(run, workspace_id) == "renamed"
        )
        if not slug_committed:
            stale_binary_gate(run, run_call(run, "check", {"old": old, "new": new}))
        run_call(run, "flush_cache", {"dry_run": True})
        detect_saltbox(names, docker)
    except RuntimeError as failure:
        raise refused(failure) from None
    if operation == "check":
        if step:
            say("A receipt for this rename is at step " + step + "; apply resumes from there")
        return "Check passed: apply may run"

    receipt = new_receipt(directory, old, new, uid)  # pending receipt first
    step = receipt["step"]
    if step != "pending":
        say("Resuming the rename from step " + step)

    run["workspace_id"], run["backup_action"] = workspace_id, backup_action
    try:
        if backup_action == "rotate":
            finish_rotation(directory, uid)
        if at_or_past(step, "stopped") and not at_or_past(step, "started"):
            step = resume_stop(run, step, slug_committed)
    except Exception as failure:  # noqa: BLE001
        if getattr(failure, "unknown_services", False):
            raise
        resume_failed(run, step, reason_of(failure))

    run_steps(
        run,
        order,
        step,
        lambda target: apply_steps()[target][0](run),
        lambda step, target, failure: apply_recover(run, step, target, failure),
    )
    return f"Rename complete: {old} is now {new}"


SHIPPED = (
    apply_stop,
    apply_backup,
    apply_slug,
    apply_rows,
    apply_cache,
    apply_start,
    apply_health,
    snippet_stuck,
    pre_slug_failure,
    post_slug_failure,
    slug_unclear,
    apply_steps,
    apply_recover,
    any_running,
    slug_reached,
    resume_stop,
    resume_failed,
    run_apply,
)
