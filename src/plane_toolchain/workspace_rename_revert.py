"""The revert state machine of the workspace rename (shipped to the host)."""

import os

from plane_toolchain.remote import say
from plane_toolchain.workspace_rename_receipt import (
    journal_workspace,
    load_journal,
    load_receipt,
    set_receipt_field,
    step_order,
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
    warnings_text,
)
from plane_toolchain.workspace_rename_snippet import is_stuck, no_summary, report, run_snippet


def revert_mark(run):
    set_receipt_field(run["directory"], "reverted_from", run["origin"], run["uid"])


def revert_restore_rows(run):
    directory, uid, crash = run["directory"], run["uid"], run["crash"]
    set_receipt_field(directory, "restore_started", True, uid)
    crash("before_restore")
    args = {
        "old": run["old"],
        "new": run["new"],
        "journal": run["records"],
        "workspace_id": run["workspace_id"],
    }
    summary, events = run_snippet(
        run["api"],
        run["snippet"],
        "restore",
        args,
        directory,
        run["popen"],
        uid,
        run["names"].values(),
        run["idle"],
    )
    report("restore", summary, events)
    crash("after_restore")
    if summary is None:
        raise RuntimeError(no_summary("restore", events))
    skipped = summary.get("skipped", 0) if summary.get("partial") else 0
    if not skipped and summary.get("ok") is not True:
        raise RuntimeError("restore failed")
    try:
        set_receipt_field(directory, "restore_partial", skipped, uid)
    except Exception as problem:  # noqa: BLE001 - a partial restore must still finish
        if not skipped:
            raise
        run["notes"].append(
            "could not record restore_partial in the receipt: " + reason_of(problem)
        )
    run["partial"] = skipped or None


def revert_verify(run):
    if run["workspace_id"]:
        run_health(run, run["new"], run["old"], run["workspace_id"])
    else:  # nothing journalled: OLD must be live and NEW free
        run_call(run, "check", {"old": run["old"], "new": run["new"]})


def revert_restore(run):
    say("Stopping services for the restore")
    run_stop(run)
    if run["restore_needed"]:
        revert_restore_rows(run)
    run_call(run, "flush_cache", {})


def revert_start(run):
    run_start(run)


def revert_steps():
    """The revert step table: step -> (effect, what a failure leaves behind).

    revert_failure turns the second item into the operator message."""
    return {
        "reverting": (
            revert_mark,
            "Nothing was changed by the revert; services were left as they are",
        ),
        "restored": (
            revert_restore,
            "Services stay stopped (some may still run if stopping failed). The restore is "
            "all-or-nothing and safe to repeat",
        ),
        "started": (revert_start, "The data is restored, but some services did not start"),
        "reverted": (revert_verify, "Services are running; the receipt is left at started"),
    }


def revert_failure(target, reason):
    """Failure policy for every revert step; never returns."""
    if is_stuck(reason):
        raise RuntimeError(
            f"Revert stopped at {target}: {reason}. Services were not restarted and the "
            "receipt was not changed. Stop the snippet process in the API container by hand "
            "(its environment has PLANE_RENAME_RUN), then rerun revert."
        )
    state = drain_state(reason)
    if state is None:
        state = revert_steps()[target][1]
    raise RuntimeError(
        f"Revert stopped at {target}: {reason}. {state}. Fix the cause and rerun revert. "
        "The journal and backup.dump are kept; restoring backup.dump by hand is the last "
        "resort."
    )


def revert_settle(run):
    """After a partial restore: restart services and recheck; never returns then."""
    partial = run["partial"]
    if not partial:
        return
    notes = run["notes"]
    run_start_noting(run, notes)
    try:
        revert_verify(run)
    except Exception as problem:  # noqa: BLE001
        notes.append("health check failed: " + reason_of(problem))
    run["finished"] = True
    raise RuntimeError(
        f"Revert incomplete: the restore was partial ({partial} rows skipped, listed above "
        "by id); this is not a full revert. The slug and every other journalled row are "
        "restored and services were started. The skipped rows changed after the rename and "
        "were not overwritten. Review them, then rerun revert to recheck (it stops services "
        "first). Restoring backup.dump by hand is the last resort and loses later edits."
        + warnings_text(notes)
    )


def revert_recover(run, step, target, failure):
    if run["finished"]:  # the report from revert_settle
        raise failure
    revert_failure(target, reason_of(failure))


def run_revert(data, seams):
    """Revert one workspace rename from its receipt and journal; resumable like apply.

    Same payload and seams as deploy. Steps (effect, then advance): reverting (the receipt
    marker, with `reverted_from` naming the apply step), restored (stop services, restore from
    the current records.jsonl only, flush the cache), started, reverted (health with the slugs
    swapped and the journalled `workspace_id`). Every refusal comes before the first stop, and
    the restore runs only with services stopped. The restore is skipped when nothing was
    journalled, or when slug_state shows the slug change never committed and no restore was
    started. A partial restore restarts services but leaves the receipt at reverting, so a
    rerun stops services and restores again; it is never reported as complete. The backup and
    the journal are never deleted, and the pg_dump backup is never restored here: that is a
    manual last resort.
    """
    run = run_context(data, seams)
    old, new, directory = run["old"], run["new"], run["directory"]
    uid = run["uid"]
    applied, order = step_order("apply"), step_order("revert")

    # Preflight: read-only. It never creates the receipt directory.
    try:
        run_preflight(run, "revert")
        if not os.path.lexists(directory / "receipt.json"):
            raise RuntimeError("no receipt for this rename; nothing to revert")
        receipt = load_receipt(directory, old, new, uid)
        phase, step = receipt.get("phase"), receipt.get("step")
        if phase not in ("apply", "revert") or step not in step_order(phase):
            raise RuntimeError("receipt state is unknown")
        if step == "reverted":
            return "Revert already complete"
        records = load_journal(directory, uid)  # the current journal only, never a rotated one
        workspace_id = journal_workspace(records)
        if records and workspace_id is None:
            raise RuntimeError("journal has no workspace record")
        origin = step if phase == "apply" else receipt.get("reverted_from")
        committed = origin in applied and applied.index(origin) >= applied.index("slug_done")
        restore_needed = False
        if phase == "apply" or step == "reverting":
            if not records and committed:
                raise RuntimeError(
                    "journal is empty but the receipt is past the slug change; restore "
                    "backup.dump by hand as the last resort"
                )
            if records:
                state = run_slug_state(run, workspace_id)
                if state == "unknown":
                    raise RuntimeError(
                        "slug state unknown: the journalled workspace holds neither slug as "
                        "expected; check which workspace holds each slug"
                    )
                restore_needed = (
                    state == "renamed" or committed or bool(receipt.get("restore_started"))
                )
                if not restore_needed:
                    say("The slug change never committed; no restore needed")
            else:
                if run_slug_state(run, None) != "original":
                    raise RuntimeError(
                        "slug state unknown: nothing was journalled, but OLD is not live or NEW "
                        "is taken; check which workspace holds each slug, and restore "
                        "backup.dump by hand as the last resort"
                    )
                say("Nothing was journalled; no restore needed")
    except RuntimeError as failure:
        raise refused(failure) from None

    if phase == "revert":
        say("Resuming the revert from step " + step)

    run.update(
        records=records,
        workspace_id=workspace_id,
        origin=origin,
        restore_needed=restore_needed,
        partial=None,
        finished=False,
        notes=[],
    )
    run_steps(
        run,
        order,
        None if phase == "apply" else step,
        lambda target: revert_steps()[target][0](run),
        lambda step, target, failure: revert_recover(run, step, target, failure),
        lambda: revert_settle(run),
    )
    return f"Revert complete: {new} is back to {old}"


SHIPPED = (
    revert_mark,
    revert_restore_rows,
    revert_verify,
    revert_restore,
    revert_start,
    revert_steps,
    revert_failure,
    revert_settle,
    revert_recover,
    run_revert,
)
