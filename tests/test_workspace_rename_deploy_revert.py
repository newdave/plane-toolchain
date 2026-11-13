"""Workspace rename host program: revert."""

import pytest
from fake_docker import Killed
from plane_toolchain import workspace_rename as wr
from support_workspace_rename_deploy import (
    NEW,
    OLD,
    PRIVATE,
    REVERT_FAILURES,
    REVERT_KILL_LABELS,
    REVERT_REFUSALS,
    SERVICES,
    crashing_program,
    files,
    first_op,
    mutating,
    revert,
    revert_inject,
    run_remote,
    sabotage,
    snapshot,
    watch_restore,
)
from support_workspace_rename_deploy import host  # noqa: F401


def test_revert_after_complete_restores_slug_and_all_records_byte_exact(host, monkeypatch, capsys):
    original = snapshot(host.orm)
    host.deploy()
    journal = host.journal()
    seen = watch_restore(host)
    assert run_remote(wr.REMOTE, host.payload("revert"), host, monkeypatch) == 0
    out = capsys.readouterr()
    assert "Revert complete" in out.out and "Traceback" not in out.err
    assert snapshot(host.orm) == original  # binary, JSON and text columns byte-exact
    assert {r["id"]: r["slug"] for r in host.orm.models["workspace"].rows} == {
        "w1": OLD,
        "w2": "other",
    }
    assert host.request("restore") == [
        {"old": OLD, "new": NEW, "journal": journal, "workspace_id": "w1"}
    ]
    assert seen and not any(seen[0][name] for name in SERVICES)  # stopped during restore
    assert all(host.running().values())
    assert PRIVATE not in out.out + out.err


@pytest.mark.parametrize("step", wr.step_order("apply"))
def test_revert_from_partial_state_each_step(host, step):
    original = snapshot(host.orm)
    order = wr.step_order("apply")
    if step == "complete":
        host.deploy()
    else:
        host.fake.crash_at("before_" + order[order.index(step) + 1])
        with pytest.raises(Killed):
            host.deploy()
    assert host.receipt()["step"] == step
    revert(host)
    assert snapshot(host.orm) == original
    assert all(host.running().values())
    assert host.receipt()["reverted_from"] == step
    restored = bool(host.request("restore"))
    assert restored == (order.index(step) >= order.index("slug_done"))


def test_revert_reuses_stop_flush_start_health(host):
    host.deploy()
    mark, ops = len(host.fake.calls), len(host.ops())
    revert(host)
    order = []
    for call in host.fake.calls[mark:]:
        if call[0] in ("stop", "start", "restart"):
            order.append(call[0] + " " + call[1])
    assert order == [
        "stop plane-beat-worker",
        "stop plane-worker",
        "stop plane",
        "stop plane-space",
        "stop plane-admin",
        "restart plane-live",
        "start plane-admin",
        "start plane-space",
        "start plane",
        "start plane-worker",
        "start plane-beat-worker",
    ]
    assert host.ops()[ops:] == ["health", "restore", "flush_cache", "health"]  # probe first
    assert host.request("flush_cache")[-1] == {}
    health = host.request("health")[-1]
    assert (health["old"], health["new"], health["workspace_id"]) == (NEW, OLD, "w1")
    assert host.orm.cache == {}


@pytest.mark.parametrize("case", sorted(REVERT_REFUSALS))
def test_revert_refuses_without_receipt_or_with_unknown_state(host, case, monkeypatch, capsys):
    arrange, reason = REVERT_REFUSALS[case]
    arrange(host)
    existed = host.directory.exists()
    before = snapshot(host.orm)
    receipt = host.receipt() if (host.directory / "receipt.json").exists() else None
    host.fake.calls.clear()
    assert run_remote(wr.REMOTE, host.payload("revert"), host, monkeypatch) == 1
    err = capsys.readouterr().err
    assert "Refused" in err and reason in err and "Traceback" not in err
    assert not mutating(host.fake) and "restore" not in host.ops()
    assert snapshot(host.orm) == before
    assert host.directory.exists() == existed  # not even the receipt directory
    if receipt is not None:
        assert host.receipt() == receipt


@pytest.mark.parametrize("apply_label", [None, "after_slug_done", "after_rows_done"])
@pytest.mark.parametrize("label", REVERT_KILL_LABELS)
def test_revert_kill_matrix_then_rerun_revert_converges(host, label, apply_label):
    original = snapshot(host.orm)
    if apply_label:
        host.fake.crash_at(apply_label)
        with pytest.raises(Killed):
            host.deploy()
    else:
        host.deploy()
    kept = files(host)
    seen = watch_restore(host)
    host.fake.crash_at(label)
    with pytest.raises(Killed):
        host.deploy("revert")
    assert host.receipt()["step"] != "reverted"
    revert(host)
    assert snapshot(host.orm) == original
    assert all(host.running().values())
    assert files(host) == kept  # backup and journal untouched
    for states in seen:  # every restore ran with the services stopped
        assert not any(states[name] for name in SERVICES)


def test_revert_kill_and_rerun_through_the_assembled_program(host, monkeypatch, capsys):
    original = snapshot(host.orm)
    host.deploy()
    host.fake.crash_at("after_restore")
    with pytest.raises(Killed):
        run_remote(
            crashing_program(), host.payload("revert"), host, monkeypatch, host.fake.checkpoint
        )
    assert host.receipt()["step"] == "reverting"
    assert run_remote(wr.REMOTE, host.payload("revert"), host, monkeypatch) == 0
    assert "Revert complete" in capsys.readouterr().out
    assert snapshot(host.orm) == original


@pytest.mark.parametrize("failure", sorted(REVERT_FAILURES))
def test_revert_failure_and_restart_then_rerun_converges(host, failure, monkeypatch, capsys):
    original = snapshot(host.orm)
    host.deploy()
    kept = files(host)
    real = host.fake.on_manage_shell
    revert_inject(host, failure, monkeypatch)
    seen = watch_restore(host)  # wraps any sabotage, so it sees every restore call
    if failure.startswith("advance"):  # the monkeypatched advance lives in this module only
        with pytest.raises(RuntimeError) as raised:
            host.deploy("revert")
        err = str(raised.value)
    else:
        assert run_remote(wr.REMOTE, host.payload("revert"), host, monkeypatch) == 1
        err = capsys.readouterr().err
    assert "Traceback" not in err and "rerun revert" in err.lower()
    receipt = host.receipt()
    expected = REVERT_FAILURES[failure]
    assert (receipt["phase"] if expected == "apply" else receipt["step"]) == expected
    if expected == "reverting" and failure != "stop_fails":
        assert not any(host.running()[name] for name in SERVICES)  # stopped for the rerun
    host.fake.celery_listed = True
    host.orm.models["workspace"].fail_update = False
    host.shell.api["status"] = 200
    host.fake.on_manage_shell = real
    seen.extend(watch_restore(host))
    revert(host)
    assert snapshot(host.orm) == original
    assert all(host.running().values())
    assert files(host) == kept
    for states in seen:
        assert not any(states[name] for name in SERVICES)


def test_revert_does_not_restore_pg_dump(host):
    host.deploy()
    dump = (host.directory / "backup.dump").read_bytes()
    digest = host.receipt()["backup_sha256"]
    mark = len(host.fake.calls)
    revert(host)
    texts = [" ".join(map(str, call)) for call in host.fake.calls[mark:]]
    assert not [t for t in texts if "pg_restore" in t or "pg_dump" in t or "psql" in t]
    assert (host.directory / "backup.dump").read_bytes() == dump
    assert host.receipt()["backup_sha256"] == digest


def test_revert_marks_receipt_reverted_and_keeps_journal(host):
    host.shell.die_at = ("step_slug", 3)  # a rotated journal from a pre-slug failure
    with pytest.raises(RuntimeError, match="Services restarted"):
        host.deploy()
    host.deploy()
    kept = files(host)
    assert any(name.startswith("records-") for name in kept)
    revert(host)
    receipt = host.receipt()
    assert (receipt["phase"], receipt["step"], receipt["reverted_from"]) == (
        "revert",
        "reverted",
        "complete",
    )
    assert files(host) == kept  # journal, rotated journal and backup all kept
    host.fake.calls.clear()
    assert "Revert already complete" in host.deploy("revert")
    assert not mutating(host.fake)
    assert files(host) == kept


def test_revert_skips_restore_when_nothing_was_journalled(host, capsys):
    original = snapshot(host.orm)
    host.fake.crash_at("after_stopped")
    with pytest.raises(Killed):
        host.deploy()
    revert(host)
    out = capsys.readouterr().out
    assert "Nothing was journalled" in out
    assert "restore" not in host.ops()
    assert host.request("check")[-1] == {"old": OLD, "new": NEW}  # verified with no id
    assert snapshot(host.orm) == original and all(host.running().values())


def test_revert_skips_restore_when_the_slug_change_never_committed(host, capsys):
    original = snapshot(host.orm)
    host.fake.crash_at("before_slug_done")
    with pytest.raises(Killed):
        host.deploy()
    wr.append_journal(host.directory, {"kind": "workspace", "id": "w1", "slug": OLD})
    revert(host)
    out = capsys.readouterr().out
    assert "never committed" in out
    assert "restore" not in host.ops()
    health = host.request("health")[-1]
    assert (health["old"], health["new"], health["workspace_id"]) == (NEW, OLD, "w1")
    assert snapshot(host.orm) == original


def test_partial_revert_is_not_complete_and_exits_nonzero(host, monkeypatch, capsys):
    original = snapshot(host.orm)
    issue = host.orm.models["issue"].rows[0]
    saved = dict(issue)
    host.deploy()
    issue["description_html"] = "<p>edited after the rename</p>"
    assert run_remote(wr.REMOTE, host.payload("revert"), host, monkeypatch) == 1
    out = capsys.readouterr()
    text = out.out + out.err
    assert "Skipped issues i1 (changed)" in text
    assert "not a full revert" in text and "Revert complete" not in text
    assert "rerun revert" in out.err.lower() and "Traceback" not in out.err
    assert PRIVATE not in text
    receipt = host.receipt()
    assert receipt["step"] == "reverting" and receipt["restore_partial"] == 1
    assert all(host.running().values())  # the rest is restored; services are back
    issue.update(saved)  # the operator puts the row back by hand
    revert(host)
    assert snapshot(host.orm) == original
    assert host.receipt()["restore_partial"] == 0


def test_revert_interrupt_gives_guidance_without_traceback(host, monkeypatch, capsys):
    host.deploy()

    def interrupted(text):
        if first_op(text) == "restore":
            raise KeyboardInterrupt
        return host.shell(text)

    host.fake.on_manage_shell = interrupted
    assert run_remote(wr.REMOTE, host.payload("revert"), host, monkeypatch) == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err and "interrupted" in err.lower() and "rerun revert" in err


def test_apply_after_a_finished_revert_says_so_and_asks_to_archive_the_receipt(host):
    host.deploy()
    revert(host)
    host.fake.calls.clear()
    for operation in ("apply", "check"):
        with pytest.raises(RuntimeError, match="revert is complete") as refused:
            host.deploy(operation)
        assert "move the receipt directory" in str(refused.value)
    assert not mutating(host.fake) and host.receipt()["step"] == "reverted"


@pytest.mark.parametrize("name", ["records.jsonl", "records-20261113T000000Z.jsonl", "backup.dump"])
def test_apply_refuses_a_journal_or_backup_without_a_receipt(host, name):
    directory = wr.secure_receipt_dir(host.directory)
    (directory / name).write_bytes(b"left over")
    (directory / name).chmod(0o600)
    before = snapshot(host.orm)
    for operation in ("apply", "check"):
        with pytest.raises(RuntimeError, match="without a receipt") as refused:
            host.deploy(operation)
        assert name in str(refused.value) and "move" in str(refused.value)
    assert not mutating(host.fake) and "step_slug" not in host.ops()
    assert not (directory / "receipt.json").exists()
    assert (directory / name).read_bytes() == b"left over"
    assert snapshot(host.orm) == before


def test_partial_restore_survives_a_failed_receipt_write(host, monkeypatch):
    host.deploy()
    host.orm.models["issue"].rows[0]["description_html"] = "<p>edited after the rename</p>"
    real = wr.set_receipt_field

    def failing(directory, name, value, uid=None):
        if name == "restore_partial":
            raise OSError("disk full")
        return real(directory, name, value, uid)

    monkeypatch.setattr(wr, "set_receipt_field", failing)
    with pytest.raises(RuntimeError, match="Revert incomplete") as refused:
        host.deploy("revert")
    message = str(refused.value)
    assert "not a full revert" in message and "restore_partial" in message
    assert "rerun revert" in message
    assert all(host.running().values())  # services are not left stopped
    assert host.receipt()["step"] == "reverting"


def test_flush_failure_after_a_partial_restore_leaves_guidance(host):
    host.deploy()
    host.orm.models["issue"].rows[0]["description_html"] = "<p>edited after the rename</p>"
    sabotage(host, "flush_cache", "unrun")
    with pytest.raises(RuntimeError, match="Revert stopped at restored") as refused:
        host.deploy("revert")
    assert "rerun revert" in str(refused.value)
    assert host.receipt()["step"] == "reverting"
