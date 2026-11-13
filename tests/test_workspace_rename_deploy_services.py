"""Workspace rename host program: services, drain and backup."""

import builtins
import hashlib
import json
import stat
import subprocess
import symtable
import pytest
from fake_docker import FakeDocker, Killed, run_program
from plane_toolchain import workspace_rename as wr
from plane_toolchain.remote import program
from plane_toolchain.remote import docker as real_docker
from support_workspace_rename_deploy import ALL, Clock, NAMES, mutating, receipt_dir, stop
from support_workspace_rename_deploy import fake  # noqa: F401


def test_service_names_default_and_env_override():
    assert NAMES == {
        "db": "plane-db",
        "redis": "plane-redis",
        "web": "plane",
        "space": "plane-space",
        "admin": "plane-admin",
        "worker": "plane-worker",
        "beat": "plane-beat-worker",
        "live": "plane-live",
        "mq": "plane-mq",
    }
    assert wr.service_names({"PLANE_MQ_CONTAINER": "mq2"})["mq"] == "mq2"


def test_stop_order_beat_then_drain_then_worker_then_web_space_admin(fake):
    stop(fake)
    order = [
        c[1] if c[0] in ("stop", "restart") else "drain"
        for c in fake.calls
        if c[0] in ("stop", "restart") or any("rabbitmqctl" in a for a in c)
    ]
    first_drain = order.index("drain")
    last_drain = len(order) - 1 - order[::-1].index("drain")
    assert order[:first_drain] == ["plane-beat-worker"]
    assert order[last_drain + 1 :] == [
        "plane-worker",
        "plane",
        "plane-space",
        "plane-admin",
        "plane-live",
    ]


def test_drain_waits_until_queue_empty_or_60s_then_reports_remaining_without_purge(fake, capsys):
    clock = Clock()
    fake.queue_depths = [5, 3, 0]
    assert wr.drain_queue(NAMES["mq"], fake.docker, clock, clock.sleep) == 0
    assert 0 < clock.now < 60

    clock = Clock()
    fake.queue_depths = [4]
    assert wr.drain_queue(NAMES["mq"], fake.docker, clock, clock.sleep) == 4
    assert 60 <= clock.now < 70
    fake.calls.clear()
    with pytest.raises(RuntimeError, match="4 jobs still queued"):  # PTC-63: a refusal now
        stop(fake)
    assert fake.containers[NAMES["worker"]]["running"]  # nothing past beat was stopped
    clock = Clock()
    wr.stop_services(NAMES, fake.docker, clock, clock.sleep, fake.checkpoint, allow_undrained=True)
    assert "4 jobs still queued; not purged" in capsys.readouterr().out
    assert not any(
        word in " ".join(c)
        for c in fake.calls
        if c[0] == "exec"
        for word in ("purge", "delete_queue", "reset")
    )


def test_live_restarted_api_db_redis_left_running(fake):
    stop(fake)
    assert ("restart", "plane-live") in fake.calls
    touched = {c[1] for c in mutating(fake)}
    assert not touched & {"plane-api", "plane-db", "plane-redis", "plane-mq"}
    for name in ("plane-api", "plane-db", "plane-redis", "plane-live"):
        assert fake.containers[name]["running"]
    for name in ("plane", "plane-space", "plane-admin", "plane-worker", "plane-beat-worker"):
        assert not fake.containers[name]["running"]


def test_start_reverse_order(fake):
    stop(fake)
    fake.calls.clear()
    wr.start_services(NAMES, fake.docker, fake.checkpoint)
    assert [c for c in fake.calls if c[0] == "start"] == [
        ("start", n)
        for n in ("plane-admin", "plane-space", "plane", "plane-worker", "plane-beat-worker")
    ]
    assert all(fake.containers[n]["running"] for n in ALL)


def test_drain_probes_the_configured_vhost_without_secrets_in_argv(fake):
    clock = Clock()
    wr.drain_queue(NAMES["mq"], fake.docker, clock, clock.sleep)
    probe = fake.queue_probes[0]
    assert '-p "${RABBITMQ_DEFAULT_VHOST:-/}"' in probe and "sh -c" in probe
    assert "plane" not in probe.replace(NAMES["mq"], "")


def test_drain_reports_celery_queue_not_found_instead_of_drained(fake, capsys):
    fake.celery_listed = False
    clock = Clock()
    assert wr.drain_queue(NAMES["mq"], fake.docker, clock, clock.sleep) is None
    assert clock.now == 0
    stop(fake)
    out = capsys.readouterr().out
    assert "celery queue not found" in out and "Queue drained" not in out
    assert not fake.containers[NAMES["web"]]["running"]  # a warning, not a failure


def test_only_docker_stop_start_restart_used(fake, tmp_path):
    stop(fake)
    wr.start_services(NAMES, fake.docker, fake.checkpoint)
    wr.backup_database("plane-db", receipt_dir(tmp_path), fake.popen)
    wr.detect_saltbox(NAMES, fake.docker)
    assert {c[0] for c in fake.calls} <= {"stop", "start", "restart", "exec", "inspect"}


def test_backup_streams_pg_dump_to_0600_file_and_records_sha256(fake, tmp_path):
    fake.pg_dump = b"PGDMP" + bytes(range(256)) * 1000
    directory = receipt_dir(tmp_path)
    digest = wr.backup_database("plane-db", directory, fake.popen)
    dump = directory / "backup.dump"
    assert dump.read_bytes() == fake.pg_dump
    assert stat.S_IMODE(dump.stat().st_mode) == 0o600
    assert digest == hashlib.sha256(fake.pg_dump).hexdigest()
    assert json.loads((directory / "receipt.json").read_text())["backup_sha256"] == digest
    assert ("exec", "plane-db", "sh", "-c", wr.PG_DUMP) in fake.calls
    assert not list(directory.glob("*.tmp"))


def test_backup_failure_aborts_before_any_data_change(fake, tmp_path):
    directory = receipt_dir(tmp_path)
    fake.pg_dump_code = 1
    with pytest.raises(RuntimeError, match="backup failed"):
        wr.backup_database("plane-db", directory, fake.popen)
    assert not (directory / "backup.dump").exists()
    assert not list(directory.glob("*.tmp"))
    assert "backup_sha256" not in json.loads((directory / "receipt.json").read_text())
    assert not any("manage.py" in c for c in fake.calls)


def test_backup_empty_output_refused(fake, tmp_path):
    directory = receipt_dir(tmp_path)
    fake.pg_dump = b""
    with pytest.raises(RuntimeError, match="empty"):
        wr.backup_database("plane-db", directory, fake.popen)
    assert not (directory / "backup.dump").exists()
    assert "backup_sha256" not in json.loads((directory / "receipt.json").read_text())


def test_no_credentials_in_argv_or_output(fake, tmp_path, capsys):
    secret = "do-not-print"  # noqa: S105 - fake env marker
    stop(fake)
    wr.start_services(NAMES, fake.docker, fake.checkpoint)
    directory = receipt_dir(tmp_path)
    wr.backup_database("plane-db", directory, fake.popen)
    wr.detect_saltbox(NAMES, fake.docker)
    assert secret not in json.dumps(fake.calls)
    assert '"$POSTGRES_USER"' in wr.PG_DUMP and "-U" in wr.PG_DUMP
    out = capsys.readouterr()
    assert secret not in out.out + out.err
    assert secret not in (directory / "receipt.json").read_text()


def test_saltbox_detection_reported_info_only(fake, capsys):
    plain = FakeDocker()
    for name in ALL:
        plain.add(name)
    assert wr.detect_saltbox(NAMES, plain.docker) is False
    fake.containers["plane-db"]["labels"][wr.SALTBOX_LABEL] = "true"
    assert wr.detect_saltbox(NAMES, fake.docker) is True
    assert "Saltbox" in capsys.readouterr().out
    stop(plain)
    stop(fake)
    assert mutating(plain) == mutating(fake)


def test_crash_at_is_one_shot_and_uncatchable_by_exception_handlers(fake):
    fake.crash_at("after_stop_plane-worker")
    with pytest.raises(Killed):
        stop(fake)
    assert not fake.containers["plane-worker"]["running"]
    assert fake.containers["plane"]["running"]  # web not yet stopped
    stop(fake)  # one-shot: the re-run completes
    assert not fake.containers["plane-admin"]["running"]


def test_manage_shell_stdin_routed_to_handler(fake):
    fake.on_manage_shell = lambda text: (text.upper() + "\n", 0)
    out = fake.docker("exec", "-i", "plane-api", "python", "manage.py", "shell", input=b"print(1)")
    assert out == b"PRINT(1)\n"


def test_shipped_program_stops_backs_up_and_starts(fake, tmp_path, monkeypatch):
    directory = tmp_path / "r"
    entry = (
        "names = service_names({})\n"
        "stop_services(names, docker, time.time, time.sleep)\n"
        "secure_receipt_dir(DIR)\n"
        "new_receipt(DIR, 'a', 'b')\n"
        "backup_database('plane-db', DIR, subprocess.Popen)\n"
        "start_services(names, docker)\n"
    )
    source = program(
        wr.IMPORTS,
        [real_docker, *wr.SHIPPED],
        entry,
        {"DIR": str(directory), **wr.SHIPPED_CONSTANTS},
    )
    monkeypatch.setattr(subprocess, "Popen", fake.popen)
    run_program(source, {}, fake, monkeypatch)
    assert (directory / "backup.dump").read_bytes() == fake.pg_dump
    assert all(fake.containers[n]["running"] for n in ALL)
    assert len(json.loads((directory / "receipt.json").read_text())["backup_sha256"]) == 64


def test_backup_refuses_existing_dump_and_starts_no_pg_dump(fake, tmp_path):
    directory = receipt_dir(tmp_path)
    digest = wr.backup_database("plane-db", directory, fake.popen)
    fake.calls.clear()
    fake.pg_dump = b"different"
    with pytest.raises(RuntimeError, match=r"backup\.dump already exists"):
        wr.backup_database("plane-db", directory, fake.popen)
    assert (directory / "backup.dump").read_bytes() == b"PGDMP-fake-dump"
    assert json.loads((directory / "receipt.json").read_text())["backup_sha256"] == digest
    assert not any("pg_dump" in " ".join(c) for c in fake.calls)
    assert not list(directory.glob("*.tmp"))


def test_backup_read_error_kills_and_waits_for_child(tmp_path):
    events = []

    class Broken:
        def read(self, size):
            raise OSError("pipe broke")

    class Process:
        stdout = Broken()

        def kill(self):
            events.append("kill")

        def wait(self):
            events.append("wait")

    directory = receipt_dir(tmp_path)
    with pytest.raises(OSError, match="pipe broke"):
        wr.backup_database("plane-db", directory, lambda argv, **kw: Process())
    assert events[-2:] == ["kill", "wait"]  # the dump child; the size probe failed the same way
    assert not list(directory.glob("backup.dump*"))


def test_shipped_program_defines_every_global_its_functions_reference():
    imports = wr.IMPORTS
    source = program(
        imports,
        [real_docker, *wr.SHIPPED],
        "",
        {"DIR": "x", **wr.SHIPPED_CONSTANTS},
    )
    table = symtable.symtable(source, "<program>", "exec")
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
    assert not missing, f"shipped functions use names the program does not define: {missing}"
