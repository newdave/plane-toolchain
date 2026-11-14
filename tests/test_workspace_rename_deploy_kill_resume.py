"""Workspace rename host program: kill and resume at every step."""

import builtins
import hashlib
import stat
import symtable
import pytest
from fake_docker import Killed
from plane_toolchain import workspace_rename as wr
from plane_toolchain.remote import PREAMBLE
from support_workspace_rename_deploy import (
    DEATHS,
    KILL_LABELS,
    NEW,
    OLD,
    PRE_SLUG_FAILURES,
    clear,
    crashing_program,
    inject,
    reference_end_state,
    resumed_order,
    revert,
    run_remote,
    snapshot,
)
from support_workspace_rename_deploy import host  # noqa: F401


@pytest.mark.parametrize("label", KILL_LABELS)
def test_kill_at_every_step_then_resume_reaches_same_end_state(host, label, tmp_path):
    reference = reference_end_state(tmp_path)
    host.fake.crash_at(label)
    with pytest.raises(Killed):
        host.deploy()
    assert host.receipt()["step"] != "complete"
    assert "Rename complete" in host.deploy()
    assert snapshot(host.orm) == snapshot(reference.orm)
    assert all(host.running().values())
    assert host.journal() == reference.journal()
    assert host.receipt() == reference.receipt()
    assert (host.directory / "backup.dump").read_bytes() == host.fake.pg_dump
    assert sum("pg_dump" in " ".join(c) for c in host.fake.calls) == 1  # never a second backup
    for op in ("step_slug", "step_rows", "health"):
        for args in host.request(op)[1:]:  # every resumed call names the journalled workspace
            assert args["workspace_id"] == "w1"


@pytest.mark.parametrize("label", KILL_LABELS)
def test_kill_at_every_step_then_revert_restores_original_state(host, label):
    original = snapshot(host.orm)
    host.fake.crash_at(label)
    with pytest.raises(Killed):
        host.deploy()
    revert(host)
    assert snapshot(host.orm) == original
    assert all(host.running().values())
    if host.journal():  # health ran with the slugs swapped
        last = host.request("health")[-1]
        assert (last["old"], last["new"], last["workspace_id"]) == (NEW, OLD, "w1")


def test_sigpipe_sighup_preamble_present_in_remote_program():
    assert PREAMBLE in wr.REMOTE
    assert "signal.SIGPIPE, signal.SIG_IGN" in PREAMBLE and "signal.SIGHUP" in PREAMBLE
    assert wr.REMOTE.index(PREAMBLE) < wr.REMOTE.index("def deploy(")
    assert wr.REMOTE.startswith("import ") and "signal" in wr.REMOTE.splitlines()[0]


def test_remote_program_defines_every_global_its_functions_reference():
    table = symtable.symtable(wr.REMOTE, "<program>", "exec")
    defined = {s.get_name() for s in table.get_symbols() if s.is_assigned() or s.is_imported()}
    defined |= {s.get_name() for s in table.get_symbols() if s.is_namespace()}
    defined |= set(dir(builtins))
    missing = {}

    def walk(scope):
        for child in scope.get_children():
            for symbol in child.get_symbols():
                if symbol.is_global() and symbol.get_name() not in defined:
                    missing.setdefault(child.get_name(), set()).add(symbol.get_name())
            walk(child)

    walk(table)
    assert not missing, f"REMOTE functions use names it does not define: {missing}"


def test_apply_and_revert_share_one_copy_of_each_run_helper():
    # PTC-64: apply and revert share one state machine; no helper is duplicated per operation.
    shared = (
        "reason_of",
        "refused",
        "drain_state",
        "warnings_text",
        "run_context",
        "run_call",
        "run_stop",
        "run_start_noting",
        "run_steps",
    )
    for name in shared:
        assert wr.REMOTE.count(f"\ndef {name}(") == 1, name
    for nested in ("def reason_of(", "def call(", "def stop("):
        assert f"    {nested}" not in wr.REMOTE, nested


def test_kill_and_resume_through_the_assembled_program(host, monkeypatch):
    host.fake.crash_at("after_slug_done")
    with pytest.raises(Killed):
        run_remote(crashing_program(), host.payload(), host, monkeypatch, host.fake.checkpoint)
    assert host.receipt()["step"] == "backed_up"
    assert run_remote(wr.REMOTE, host.payload(), host, monkeypatch) == 0
    assert host.receipt()["step"] == "complete"
    assert host.request("step_slug")[-1]["workspace_id"] == "w1"


@pytest.mark.parametrize("failure", PRE_SLUG_FAILURES)
def test_failure_and_restart_at_every_pre_slug_step_then_resume_stops_first(
    host, failure, monkeypatch, tmp_path
):
    reference = reference_end_state(tmp_path)
    inject(host, failure, monkeypatch)
    with pytest.raises(RuntimeError, match="Services restarted"):
        host.deploy()
    assert all(host.running().values())
    receipt = host.receipt()
    assert receipt["step"] == "pending" and "backup_sha256" not in receipt
    assert not (host.directory / "backup.dump").exists()
    clear(host)
    mark = len(host.fake.calls)
    assert "Rename complete" in host.deploy()
    order = resumed_order(host.fake.calls[mark:])
    assert "stop" in order and "pg_dump" in order
    # stop before the fresh backup, and the backup before step_slug (after check, dry flush)
    assert (
        order.index("stop") < order.index("pg_dump") < len(order) - 1 - order[::-1].index("shell")
    )
    assert order[: order.index("stop")] == ["shell", "shell"]
    assert snapshot(host.orm) == snapshot(reference.orm)
    assert all(host.running().values())


@pytest.mark.parametrize("failure", PRE_SLUG_FAILURES)
def test_failure_and_restart_at_every_pre_slug_step_then_revert_restores_original_state(
    host, failure, monkeypatch
):
    original = snapshot(host.orm)
    inject(host, failure, monkeypatch)
    with pytest.raises(RuntimeError, match="Services restarted"):
        host.deploy()
    clear(host)
    revert(host)
    assert snapshot(host.orm) == original
    assert all(host.running().values())


def test_reset_rotates_stale_backup_and_takes_a_fresh_dump(host):
    host.orm.models["workspace"].fail_update = True
    with pytest.raises(RuntimeError, match="Services restarted"):
        host.deploy()
    rotated = host.receipt()["rotated_backups"]
    assert len(rotated) == 1 and (host.directory / rotated[0]).read_bytes() == b"PGDMP-fake-dump"
    assert stat.S_IMODE((host.directory / rotated[0]).stat().st_mode) == 0o600
    clear(host)
    host.orm.models["workspace"].rows[1]["slug"] = "edited-meanwhile"  # services were live
    host.fake.pg_dump = b"PGDMP-second-dump"
    assert "Rename complete" in host.deploy()
    assert (host.directory / "backup.dump").read_bytes() == b"PGDMP-second-dump"
    assert (host.directory / rotated[0]).read_bytes() == b"PGDMP-fake-dump"  # kept
    assert host.receipt()["backup_sha256"] == hashlib.sha256(b"PGDMP-second-dump").hexdigest()
    assert sum("pg_dump" in " ".join(c) for c in host.fake.calls) == 2


def test_reset_killed_after_receipt_write_finishes_rotation_on_resume(host):
    host.orm.models["workspace"].fail_update = True
    host.fake.crash_at("after_reset_receipt")
    with pytest.raises(Killed):
        host.deploy()
    receipt = host.receipt()
    assert receipt["step"] == "pending" and (host.directory / "backup.dump").exists()
    assert (host.directory / "records.jsonl").exists()
    host.deploy("check")  # check accepts the half-rotated state and writes nothing
    assert (host.directory / "backup.dump").read_bytes() == b"PGDMP-fake-dump"
    clear(host)
    host.fake.pg_dump = b"PGDMP-second-dump"
    assert "Rename complete" in host.deploy()
    rotated = host.receipt()["rotated_backups"][0]
    assert (host.directory / rotated).read_bytes() == b"PGDMP-fake-dump"
    journal = host.receipt()["rotated_journals"][0]
    assert wr.load_journal(host.directory / ".", None) != []
    assert '"kind":"workspace"' in (host.directory / journal).read_text()
    assert "rotating" not in host.receipt()


def test_kill_resume_restops_services_before_continuing(host):
    host.fake.crash_at("after_backed_up")
    with pytest.raises(Killed):
        host.deploy()
    mark = len(host.fake.calls)
    host.deploy()
    order = resumed_order(host.fake.calls[mark:])
    assert order.index("stop") < order.index("shell", 2)  # stop before step_slug
    assert "pg_dump" not in order  # services stayed down: the matching backup is kept


def test_kill_resume_with_services_running_takes_a_fresh_backup(host):
    host.fake.crash_at("after_backed_up")
    with pytest.raises(Killed):
        host.deploy()
    host.fake.containers["plane"]["running"] = True  # someone started web after the kill
    host.fake.pg_dump = b"PGDMP-second-dump"
    mark = len(host.fake.calls)
    assert "Rename complete" in host.deploy()
    order = resumed_order(host.fake.calls[mark:])
    assert order.index("stop") < order.index("pg_dump") < order.index("shell", 2)
    rotated = host.receipt()["rotated_backups"]
    assert (host.directory / rotated[0]).read_bytes() == b"PGDMP-fake-dump"
    assert (host.directory / "backup.dump").read_bytes() == b"PGDMP-second-dump"


@pytest.mark.parametrize("op,line", DEATHS)
def test_snippet_death_after_journal_emit_then_resume_reaches_same_end_state(
    host, op, line, tmp_path
):
    reference = reference_end_state(tmp_path)
    host.shell.die_at = (op, line)
    with pytest.raises(RuntimeError):
        host.deploy()
    if op == "step_rows":
        assert host.receipt()["step"] == "slug_done" and not host.running()["plane"]
        rows = {r["id"]: r["description_html"] for r in host.orm.models["issue"].rows}
        assert (OLD in rows["i1"]) == (line == 1)  # line 2: i1 written, p1 journalled only
    assert "Rename complete" in host.deploy()
    assert snapshot(host.orm) == snapshot(reference.orm)


@pytest.mark.parametrize("op,line", DEATHS)
def test_snippet_death_after_journal_emit_then_revert_restores_original_state(host, op, line):
    original = snapshot(host.orm)
    host.shell.die_at = (op, line)
    with pytest.raises(RuntimeError):
        host.deploy()
    revert(host)
    assert snapshot(host.orm) == original


def test_any_running_is_unknown_when_inspect_fails():
    def broken(*args):
        raise RuntimeError("docker inspect failed")

    names = wr.service_names({})
    assert wr.any_running({"docker": broken, "names": names}) is None
    assert wr.any_running({"docker": lambda *a: b"not json", "names": names}) is None


def test_kill_resume_refuses_when_service_state_is_unknown_and_keeps_the_backup(host, monkeypatch):
    host.fake.crash_at("after_backed_up")
    with pytest.raises(Killed):
        host.deploy()
    before = host.receipt()["step"]
    with monkeypatch.context() as patch:
        patch.setattr(wr, "any_running", lambda run: None)
        with pytest.raises(RuntimeError, match="cannot tell whether the services run"):
            host.deploy()
    receipt = host.receipt()
    assert receipt["step"] == before and not receipt.get("rotated_backups")
    assert (host.directory / "backup.dump").read_bytes() == b"PGDMP-fake-dump"
    assert "Rename complete" in host.deploy()  # docker answers again: the run resumes
