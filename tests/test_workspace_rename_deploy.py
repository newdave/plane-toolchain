import hashlib
import json
import stat
import subprocess

import pytest
from fake_docker import FakeDocker, Killed, run_program

from plane_toolchain import workspace_rename as wr
from plane_toolchain.remote import docker as real_docker
from plane_toolchain.remote import program

NAMES = wr.service_names({})
ALL = [
    "plane-db",
    "plane-redis",
    "plane",
    "plane-space",
    "plane-admin",
    "plane-worker",
    "plane-beat-worker",
    "plane-live",
    "plane-mq",
]


@pytest.fixture
def fake():
    fake = FakeDocker()
    for name in [*ALL, "plane-api"]:
        fake.add(name)
    return fake


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def mutating(fake):
    return [c for c in fake.calls if c[0] in ("stop", "start", "restart")]


def stop(fake, clock=None, names=NAMES):
    clock = clock or Clock()
    return wr.stop_services(names, fake.docker, clock, clock.sleep, fake.checkpoint)


def receipt_dir(tmp_path):
    directory = wr.secure_receipt_dir(tmp_path / "r")
    wr.new_receipt(directory, "a", "b")
    return directory


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
        if c[0] in ("stop", "restart") or "rabbitmqctl" in c
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
    stop(fake)
    assert "4 jobs still queued" in capsys.readouterr().out
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
        {"hashlib", "json", "os", "pathlib", "stat", "subprocess", "time"},
        [real_docker, wr.write_atomic, *wr.SHIPPED],
        entry,
        {"DIR": str(directory), **wr.SHIPPED_CONSTANTS},
    )
    monkeypatch.setattr(subprocess, "Popen", fake.popen)
    run_program(source, {}, fake, monkeypatch)
    assert (directory / "backup.dump").read_bytes() == fake.pg_dump
    assert all(fake.containers[n]["running"] for n in ALL)
    assert len(json.loads((directory / "receipt.json").read_text())["backup_sha256"]) == 64
