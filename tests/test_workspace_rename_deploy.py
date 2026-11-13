import base64
import builtins
import hashlib
import io
import json
import stat
import subprocess
import symtable
import sys
import time

import pytest
from fake_docker import FakeDocker, Killed, run_program
from fake_orm import FakeOrm, ManageShell

from plane_toolchain import workspace_rename as wr
from plane_toolchain.remote import PREAMBLE, program
from plane_toolchain.remote import docker as real_docker

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
        {"hashlib", "json", "os", "pathlib", "stat", "subprocess", "time"},
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
    assert events == ["kill", "wait"]
    assert not list(directory.glob("backup.dump*"))


def test_shipped_program_defines_every_global_its_functions_reference():
    imports = {"hashlib", "json", "os", "pathlib", "stat", "subprocess", "time"}
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


# --- T7: the apply state machine, end to end through the assembled snippet ---

OLD, NEW = "old-slug", "new-slug"
PRIVATE = "private-sentence-xyz"  # row content: must never reach the operator's output
LINK = f"https://plane.example.com/{OLD}/browse/X-1/"


def rename_orm():
    html = f'<p>{PRIVATE} <a href="{LINK}">x</a></p>'
    return FakeOrm(
        {
            "workspace": (
                "workspaces",
                {"slug": "text"},
                [
                    {"id": "w1", "slug": OLD, "deleted_at": None},
                    {"id": "w2", "slug": "other", "deleted_at": None},
                ],
            ),
            "issue": (
                "issues",
                {"description_html": "text", "description_json": "json"},
                [
                    {
                        "id": "i1",
                        "workspace_id": "w1",
                        "description_html": html,
                        "description_json": {"type": "link", "href": LINK},
                        "description_binary": b"\x00issue\xff",
                        "description_stripped": f"{PRIVATE} {LINK}",
                        "deleted_at": None,
                    }
                ],
            ),
            "page": (
                "pages",
                {"description_html": "text"},
                [
                    {
                        "id": "p1",
                        "workspace_id": "w1",
                        "description_html": html,
                        "description_json": {"type": "doc"},
                        "description_binary": b"\x01page",
                        "description_stripped": None,
                        "deleted_at": None,
                    }
                ],
            ),
            "comment": (
                "issue_comments",
                {"comment_html": "text"},
                [
                    {
                        "id": "c1",
                        "workspace_id": "w1",
                        "comment_html": f'<a href="/{OLD}/x">{PRIVATE}</a>',
                        "comment_stripped": PRIVATE,
                        "comment_json": {"type": "link", "attrs": {"href": f"/{OLD}/x"}},
                        "deleted_at": None,
                    }
                ],
            ),
            "notification": (
                "notifications",
                {"data": "json"},
                [
                    {
                        "id": "n1",
                        "workspace_id": "w1",
                        "data": {"workspace_slug": OLD},
                        "deleted_at": None,
                    }
                ],
            ),
            "description": (
                "descriptions",
                {"description_html": "text", "description_json": "json"},
                [
                    {
                        "id": "d1",
                        "workspace_id": "w1",
                        "description_html": f'<a href="{LINK}">x</a>',
                        "description_json": {"type": "link", "href": LINK},
                        "description_stripped": f"see {LINK}",
                        "deleted_at": None,
                    }
                ],
            ),
            "email_log": (
                "email_notification_logs",
                {"data": "json"},
                [{"id": "e1", "data": {"issue": {"workspace_slug": OLD}}, "deleted_at": None}],
            ),
        }
    )


def snapshot(orm):
    """Rows as JSON (binary as base64), so bytes and memoryview originals compare equal."""
    encode = lambda v: {"bytes": base64.b64encode(bytes(v)).decode()}  # noqa: E731
    return json.loads(json.dumps(orm.snapshot(), sort_keys=True, default=encode))


class Host:
    """One fake host: Docker, the API container's manage.py shell over a FakeOrm, a receipt dir."""

    def __init__(self, tmp_path):
        self.fake = FakeDocker()
        for name in [*ALL, "plane-api"]:
            self.fake.add(name)
        self.orm = rename_orm()
        self.orm.cache["session:x"] = "kept until flush"
        self.shell = ManageShell(self.orm)
        self.fake.on_manage_shell = self.shell
        self.directory = tmp_path / "receipts"
        self.clock = Clock()

    def payload(self, operation="apply", **extra):
        return {
            "operation": operation,
            "from": OLD,
            "to": NEW,
            "api": "plane-api",
            "snippet": wr.snippet_source(),
            "receipt_dir": str(self.directory),
            "live_url": "http://live.example:3000",
            **extra,
        }

    def seams(self):
        return {
            "docker": self.fake.docker,
            "popen": self.fake.popen,
            "clock": self.clock,
            "sleep": self.clock.sleep,
            "crash": self.fake.checkpoint,
        }

    def deploy(self, operation="apply", **extra):
        return wr.deploy(self.payload(operation, **extra), self.seams())

    def receipt(self):
        return json.loads((self.directory / "receipt.json").read_text())

    def journal(self):
        return wr.load_journal(self.directory)

    def ops(self):
        return [request["op"] for request in self.shell.requests]

    def request(self, op):
        return [r["args"] for r in self.shell.requests if r["op"] == op]

    def running(self):
        return {name: c["running"] for name, c in self.fake.containers.items()}


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


def run_remote(source, payload, host, monkeypatch, crash=None):
    """Execute an assembled program as `python3 -c` would; return its exit status."""
    fake = host.fake
    monkeypatch.setattr(subprocess, "run", fake.subprocess_run)
    monkeypatch.setattr(subprocess, "Popen", fake.popen)
    monkeypatch.setattr(time, "sleep", lambda seconds: fake.calls.append(("sleep", seconds)))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    namespace = {"__name__": "__main__", "CRASH": crash or (lambda label: None)}
    try:
        exec(compile(source, "<string>", "exec"), namespace)  # noqa: S102
    except SystemExit as exit:
        return 1 if exit.code not in (0, None) else 0
    return 0


def crashing_program():
    """The shipped functions with an entry whose crash seam is the test's CRASH global."""
    return program(
        wr.IMPORTS,
        wr.FUNCTIONS,
        wr.entry("{**host_seams(), 'crash': CRASH}"),
        wr.SHIPPED_CONSTANTS,
    )


def make_refusal(host, case):
    """Arrange a condition under which apply must refuse before touching anything."""
    if case == "new_taken":
        host.orm.models["workspace"].rows.append({"id": "w3", "slug": NEW, "deleted_at": None})
    elif case == "new_deleted":
        host.orm.models["workspace"].rows.append({"id": "w3", "slug": NEW, "deleted_at": "x"})
    elif case == "old_missing":
        host.orm.models["workspace"].rows[0]["slug"] = "elsewhere"
    elif case == "restricted":
        host.orm.restricted_slugs.add(NEW)
    elif case == "missing_container":
        del host.fake.containers["plane-worker"]
    elif case == "other_receipt":
        wr.new_receipt(wr.secure_receipt_dir(host.directory), "a", "b")
    elif case == "shell_crash":
        host.fake.on_manage_shell = lambda text: ("", 1)
    else:
        raise AssertionError(case)


REFUSALS = [
    "new_taken",
    "new_deleted",
    "old_missing",
    "restricted",
    "missing_container",
    "other_receipt",
    "shell_crash",
]


@pytest.mark.parametrize("case", REFUSALS)
def test_check_exits_nonzero_when_apply_would_refuse(host, case, monkeypatch, capsys):
    make_refusal(host, case)
    assert run_remote(wr.REMOTE, host.payload("check"), host, monkeypatch) == 1
    assert "Refused" in capsys.readouterr().err
    with pytest.raises(RuntimeError):
        host.deploy("apply")
    assert not mutating(host.fake)


def test_check_dry_run_end_to_end_with_fake_docker_and_fake_orm_writes_nothing(
    host, monkeypatch, capsys
):
    before = snapshot(host.orm)
    assert run_remote(wr.REMOTE, host.payload("check"), host, monkeypatch) == 0
    out = capsys.readouterr().out
    assert "Check passed" in out and "planned" in out
    assert snapshot(host.orm) == before
    assert host.orm.cache == {"session:x": "kept until flush"}
    assert not [e for e in host.orm.log if e[0] in ("update", "begin")]
    assert not mutating(host.fake)
    assert not host.directory.exists()  # not even the receipt directory
    assert host.ops() == ["check", "flush_cache"]
    assert host.request("flush_cache") == [{"dry_run": True}]
    assert not host.shell.live.calls
    assert PRIVATE not in out


def test_apply_happy_path_call_order_and_final_receipt_complete(host, monkeypatch, capsys):
    assert run_remote(wr.REMOTE, host.payload(), host, monkeypatch) == 0
    out = capsys.readouterr()
    assert "Rename complete" in out.out
    receipt = host.receipt()
    assert receipt["step"] == "complete" and receipt["phase"] == "apply"
    dump = (host.directory / "backup.dump").read_bytes()
    assert receipt["backup_sha256"] == hashlib.sha256(dump).hexdigest()
    assert host.ops() == ["check", "step_slug", "step_rows", "flush_cache", "health"]
    order = []
    for call in host.fake.calls:
        text = " ".join(call)
        if call[0] in ("stop", "start", "restart"):
            order.append(call[0] + " " + call[1])
        elif "manage.py" in call:
            order.append("shell")
        elif "pg_dump" in text:
            order.append("pg_dump")
    shells = [i for i, item in enumerate(order) if item == "shell"]
    assert order[: shells[0] + 1] == ["shell"]  # read-only check before any stop
    assert order[shells[0] + 1 : shells[1]] == [
        "stop plane-beat-worker",
        "stop plane-worker",
        "stop plane",
        "stop plane-space",
        "stop plane-admin",
        "restart plane-live",
        "pg_dump",
    ]
    assert order[shells[1] :] == [
        "shell",  # step_slug
        "shell",  # step_rows
        "shell",  # flush_cache
        "start plane-admin",
        "start plane-space",
        "start plane",
        "start plane-worker",
        "start plane-beat-worker",
        "shell",  # health
    ]
    assert all(host.running().values())
    workspaces = {r["id"]: r["slug"] for r in host.orm.models["workspace"].rows}
    assert workspaces == {"w1": NEW, "w2": "other"}
    assert OLD not in json.dumps(snapshot(host.orm)["issue"])
    assert host.orm.cache == {}
    assert {"kind": "workspace", "id": "w1", "slug": OLD} in host.journal()
    assert PRIVATE not in out.out + out.err  # journal records are never printed
    # workspace_id from step_slug reaches step_rows and health
    assert host.request("step_rows")[0]["workspace_id"] == "w1"
    assert host.request("health")[0]["workspace_id"] == "w1"


def test_apply_refuses_other_rename_receipt(host):
    wr.new_receipt(wr.secure_receipt_dir(host.directory), "a", "b")
    with pytest.raises(RuntimeError, match="different rename"):
        host.deploy()
    assert not mutating(host.fake) and not host.ops()
    assert host.receipt()["from"] == "a"


@pytest.mark.parametrize("case", [c for c in REFUSALS if c != "other_receipt"])
def test_apply_refuses_on_any_check_failure_without_stopping_services(host, case):
    make_refusal(host, case)
    before = snapshot(host.orm)
    with pytest.raises(RuntimeError, match="Refused"):
        host.deploy()
    assert not mutating(host.fake)
    assert not (host.directory / "receipt.json").exists()
    assert snapshot(host.orm) == before
    assert "step_slug" not in host.ops()


def test_health_failure_leaves_receipt_started_not_complete_and_exits_nonzero(
    host, monkeypatch, capsys
):
    host.shell.api["status"] = 503
    assert run_remote(wr.REMOTE, host.payload(), host, monkeypatch) == 1
    err = capsys.readouterr().err
    assert "health" in err.lower() and "revert" in err
    assert host.receipt()["step"] == "started"
    assert all(host.running().values())
    host.shell.api["status"] = 200
    assert "Rename complete" in host.deploy()  # a rerun rechecks health only
    assert host.ops()[-2:] == ["health", "health"]
    assert host.receipt()["step"] == "complete"


# Kill points: before and after each transition's effect (the receipt advances after it),
# plus kills inside the T6 host operations.
KILL_LABELS = [
    f"{when}_{step}" for step in wr.step_order("apply")[1:] for when in ("before", "after")
] + [
    "after_stop_plane-beat-worker",
    "after_drain",
    "after_restart_plane-live",
    "after_backup_file",
    "after_start_plane-admin",
    "before_start_plane-beat-worker",
]


def reference_end_state(tmp_path):
    reference = Host(tmp_path / "reference")
    reference.deploy()
    return reference


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


def revert_by_hand(host):
    """The operator's revert from the T7 building blocks (T8 makes it a state machine)."""
    names = wr.service_names({})
    wr.stop_services(names, host.fake.docker, host.clock, host.clock.sleep)
    records = host.journal()
    workspace_id = wr.journal_workspace(records)
    snippet = wr.snippet_source()

    def call(op, args):
        return wr.snippet_ok("plane-api", snippet, op, args, host.directory, host.fake.popen)

    if records:
        call("restore", {"old": OLD, "new": NEW, "journal": records})
    call("flush_cache", {})
    wr.start_services(names, host.fake.docker)
    if workspace_id:
        call("health", {"old": NEW, "new": OLD, "workspace_id": workspace_id})


@pytest.mark.parametrize("label", KILL_LABELS)
def test_kill_at_every_step_then_revert_restores_original_state(host, label):
    original = snapshot(host.orm)
    host.fake.crash_at(label)
    with pytest.raises(Killed):
        host.deploy()
    revert_by_hand(host)
    assert snapshot(host.orm) == original
    assert all(host.running().values())
    if host.journal():  # health ran with the slugs swapped
        assert host.request("health")[-1] == {"old": NEW, "new": OLD, "workspace_id": "w1"}


def test_resume_does_not_rebackup_when_backup_sha_matches(host, capsys):
    host.fake.crash_at("after_backed_up")
    with pytest.raises(Killed):
        host.deploy()
    digest = host.receipt()["backup_sha256"]
    host.fake.pg_dump = b"a different dump"
    assert "Rename complete" in host.deploy()
    assert sum("pg_dump" in " ".join(c) for c in host.fake.calls) == 1
    assert host.receipt()["backup_sha256"] == digest
    assert "SHA-256 matches" in capsys.readouterr().out


def test_resume_refuses_backup_sha_mismatch(host):
    host.fake.crash_at("after_backed_up")
    with pytest.raises(Killed):
        host.deploy()
    (host.directory / "backup.dump").write_bytes(b"tampered")
    host.fake.calls.clear()
    with pytest.raises(RuntimeError, match="does not match the recorded SHA-256"):
        host.deploy()
    assert not mutating(host.fake)
    assert "step_slug" not in host.ops()
    assert host.receipt()["step"] == "stopped"


def test_sigpipe_sighup_preamble_present_in_remote_program():
    assert PREAMBLE in wr.REMOTE
    assert "signal.SIGPIPE, signal.SIG_IGN" in PREAMBLE and "signal.SIGHUP" in PREAMBLE
    assert wr.REMOTE.index(PREAMBLE) < wr.REMOTE.index("def deploy(")
    assert wr.REMOTE.startswith("import ") and "signal" in wr.REMOTE.splitlines()[0]


# --- binding points from the T3, T4 and T6 reviews ---


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


def test_kill_and_resume_through_the_assembled_program(host, monkeypatch):
    host.fake.crash_at("after_slug_done")
    with pytest.raises(Killed):
        run_remote(crashing_program(), host.payload(), host, monkeypatch, host.fake.checkpoint)
    assert host.receipt()["step"] == "backed_up"
    assert run_remote(wr.REMOTE, host.payload(), host, monkeypatch) == 0
    assert host.receipt()["step"] == "complete"
    assert host.request("step_slug")[-1]["workspace_id"] == "w1"


class Lines:
    """Process stdout that checks the journal file before handing out each next line."""

    def __init__(self, lines, directory):
        self.lines, self.directory = lines, directory

    def __iter__(self):
        for index, line in enumerate(self.lines):
            journalled = sum('"event": "journal"' in x for x in self.lines[:index])
            records = wr.load_journal(self.directory) if self.directory.exists() else []
            assert len(records) == journalled, "journal line not persisted before reading on"
            yield line.encode() + b"\n"


def scripted(lines, directory):
    class Process:
        def __init__(self):
            self.stdin = io.BytesIO()
            self.stdout = Lines(lines, directory)

        def wait(self):
            return 0

        def kill(self):
            pass

    return lambda argv, **kw: Process()


def test_journal_events_fsynced_before_next_line_and_never_printed(tmp_path, capsys):
    directory = wr.secure_receipt_dir(tmp_path / "r")
    record = {"kind": "row", "table": "issues", "id": "i1", "fields": {"x": PRIVATE}}
    lines = [
        "noise that is not JSON " + PRIVATE,
        json.dumps({"event": "journal", "record": {"kind": "workspace", "id": "w1", "slug": OLD}}),
        json.dumps({"event": "journal", "record": record}),
        json.dumps({"event": "summary", "ok": True, "step": "slug", "workspace_id": "w1"}),
    ]
    summary = wr.snippet_ok("api", "", "step_slug", {}, directory, scripted(lines, directory))
    assert summary["workspace_id"] == "w1"
    assert wr.load_journal(directory)[1] == record
    out = capsys.readouterr()
    assert PRIVATE not in out.out + out.err


@pytest.mark.parametrize(
    "lines",
    [
        [],
        [json.dumps({"event": "summary", "ok": False, "step": "rows"})],
        [json.dumps({"event": "error", "reason": "row_failed"})],
    ],
)
def test_missing_summary_or_ok_false_is_failure(tmp_path, lines):
    directory = wr.secure_receipt_dir(tmp_path / "r")
    with pytest.raises(RuntimeError):
        wr.snippet_ok("api", "", "step_rows", {}, directory, scripted(lines, directory))


def test_journal_event_outside_an_apply_step_is_refused(tmp_path):
    directory = tmp_path / "r"
    lines = [json.dumps({"event": "journal", "record": {"kind": "workspace"}})]
    with pytest.raises(RuntimeError, match="unexpected journal"):
        wr.run_snippet("api", "", "check", {}, directory, scripted(lines, directory))
    assert not directory.exists()


def test_request_is_one_json_argument_line_before_the_snippet(host):
    host.deploy("check")
    assert host.shell.requests[0] == {"op": "check", "args": {"old": OLD, "new": NEW}}
    piped = [c for c in host.fake.calls if "manage.py" in c][0]
    assert piped == ("exec", "-i", "plane-api", "python", "manage.py", "shell")


@pytest.mark.parametrize("failure", ["celery_missing", "probe_fails", "stop_fails"])
def test_stop_failure_after_beat_restarts_services(host, failure):
    if failure == "celery_missing":
        host.fake.celery_listed = False
    elif failure == "probe_fails":
        host.fake.fail.append(("exec", "plane-mq"))
    else:
        host.fake.fail.append(("stop", "plane-space"))
    with pytest.raises(RuntimeError, match="Services restarted"):
        host.deploy()
    assert all(host.running().values())
    assert host.receipt()["step"] == "pending"
    assert "step_slug" not in host.ops()


def test_backup_failure_restarts_services_and_changes_no_data(host):
    before = snapshot(host.orm)
    host.fake.pg_dump_code = 1
    with pytest.raises(RuntimeError, match="Services restarted"):
        host.deploy()
    assert all(host.running().values())
    assert host.receipt()["step"] == "stopped" and snapshot(host.orm) == before


def test_slug_step_failure_restarts_services(host):
    host.orm.models["workspace"].fail_update = True
    with pytest.raises(RuntimeError, match="Services restarted"):
        host.deploy()
    assert all(host.running().values())
    assert host.receipt()["step"] == "backed_up"
    assert host.orm.models["workspace"].rows[0]["slug"] == OLD


def test_dump_without_recorded_sha_is_adopted_on_resume(host, capsys):
    host.fake.crash_at("after_backup_file")
    with pytest.raises(Killed):
        host.deploy()
    assert "backup_sha256" not in host.receipt()
    assert "Rename complete" in host.deploy()
    dump = (host.directory / "backup.dump").read_bytes()
    assert host.receipt()["backup_sha256"] == hashlib.sha256(dump).hexdigest()
    assert sum("pg_dump" in " ".join(c) for c in host.fake.calls) == 1
    assert "no recorded SHA-256" in capsys.readouterr().out


def test_dump_without_recorded_sha_outside_the_crash_window_refused(host):
    wr.new_receipt(wr.secure_receipt_dir(host.directory), OLD, NEW)
    (host.directory / "backup.dump").write_bytes(b"stray")
    (host.directory / "backup.dump").chmod(0o600)
    with pytest.raises(RuntimeError, match="no recorded SHA-256"):
        host.deploy()
    assert not mutating(host.fake)


def test_rows_failure_after_slug_done_stops_with_services_down_then_resumes(host):
    host.shell.live.fail = True
    with pytest.raises(RuntimeError, match="Services stay stopped") as failure:
        host.deploy()
    assert "revert" in str(failure.value)
    assert host.receipt()["step"] == "slug_done"
    assert not host.running()["plane"] and not host.running()["plane-beat-worker"]
    host.shell.live.fail = False
    assert "Rename complete" in host.deploy()
    assert host.request("step_rows")[-1]["workspace_id"] == "w1"


def test_partial_restore_and_every_skip_reach_the_operator(host, capsys):
    host.deploy()
    host.orm.models["issue"].rows[0]["description_html"] = "<p>edited after the rename</p>"
    summary, events = wr.run_snippet(
        "plane-api",
        wr.snippet_source(),
        "restore",
        {"old": OLD, "new": NEW, "journal": host.journal()},
        host.directory,
        host.fake.popen,
    )
    wr.report("restore", summary, events)
    out = capsys.readouterr().out
    assert summary["partial"] is True
    assert "Skipped issues i1 (changed)" in out
    assert "Partial restore" in out and "not a full revert" in out
    assert PRIVATE not in out


def test_revert_receipt_phase_refused_by_apply(host):
    wr.new_receipt(wr.secure_receipt_dir(host.directory), OLD, NEW)
    wr.advance(host.directory, "reverting")
    with pytest.raises(RuntimeError, match="revert"):
        host.deploy()
    assert not mutating(host.fake)


def test_complete_receipt_is_a_no_op(host):
    host.deploy()
    host.fake.calls.clear()
    assert "already complete" in host.deploy()
    assert not mutating(host.fake)
