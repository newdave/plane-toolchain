import ast
import base64
import builtins
import hashlib
import io
import json
import signal
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
    terminate = signal.getsignal(signal.SIGTERM)  # the entry installs its own handler
    try:
        exec(compile(source, "<string>", "exec"), namespace)  # noqa: S102
    except SystemExit as exit:
        return 1 if exit.code not in (0, None) else 0
    finally:
        signal.signal(signal.SIGTERM, terminate)
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
    assert host.ops() == [
        "check",
        "flush_cache",  # dry run, in the preflight
        "step_slug",
        "step_rows",
        "flush_cache",
        "health",
    ]
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
    assert order[: shells[1] + 1] == ["shell", "shell"]  # read-only preflight before any stop
    assert order[shells[1] + 1 : shells[2]] == [
        "stop plane-beat-worker",
        "stop plane-worker",
        "stop plane",
        "stop plane-space",
        "stop plane-admin",
        "restart plane-live",
        "pg_dump",
    ]
    assert order[shells[2] :] == [
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
    assert host.ops()[-3:] == ["health", "flush_cache", "health"]
    assert host.request("flush_cache")[-1] == {"dry_run": True}
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


def revert(host):
    """The real revert state machine (T8), run to completion."""
    message = host.deploy("revert")
    assert "Revert complete" in message
    assert host.receipt()["phase"] == "revert" and host.receipt()["step"] == "reverted"
    return message


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

    class Gone:  # the kill script's answer: nothing left running in the container
        stdout = io.BytesIO(b"gone\n")

        def wait(self):
            return 0

        def kill(self):
            pass

    return lambda argv, **kw: Gone() if "-c" in argv else Process()


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
    assert piped[:3] == ("exec", "-i", "-e") and piped[3].startswith("PLANE_RENAME_RUN=")
    assert piped[4:] == ("plane-api", "python", "manage.py", "shell")


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
    assert host.receipt()["step"] == "pending" and snapshot(host.orm) == before


def test_slug_step_failure_restarts_services(host):
    host.orm.models["workspace"].fail_update = True
    with pytest.raises(RuntimeError, match="Services restarted"):
        host.deploy()
    assert all(host.running().values())
    assert host.receipt()["step"] == "pending"
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


# --- PR #48 review fixes ---


def first_op(text):
    """The op of a piped program, from its `RENAME_REQUEST = ...loads('<json>')` line."""
    line = text.split("\n", 1)[0]
    return json.loads(ast.literal_eval(line[line.index(".loads(") + 7 : -1]))["op"]


def sabotage(host, op, mode):
    """Break the next `op` call. drop_summary: it runs, but its summary is lost; unrun: it exits
    1 without running; mismatch: its summary names another workspace; dead: it and every later
    call exit 1 without running, until the test restores host.shell."""
    state = {"armed": True, "dead": False}

    def handler(text):
        hit = state["armed"] and first_op(text) == op
        if hit:
            state["armed"] = False
            state["dead"] = mode == "dead"
        if state["dead"] or (hit and mode == "unrun"):
            return "", 1
        out, code = host.shell(text)
        if hit and mode == "drop_summary":
            out = "".join(line for line in out.splitlines(True) if '"summary"' not in line)
        if hit and mode == "mismatch":
            out = out.replace('"workspace_id": "w1"', '"workspace_id": "w9"')
        return out, code

    host.fake.on_manage_shell = handler


def fail_advance_once(monkeypatch, target):
    real = wr.advance

    def advance(directory, step, uid=None):
        if step == target:
            monkeypatch.setattr(wr, "advance", real)
            raise OSError("disk full")
        return real(directory, step, uid)

    monkeypatch.setattr(wr, "advance", advance)


# One-shot failures before slug_done; each must restart services and reset the receipt.
def inject(host, failure, monkeypatch):
    if failure == "drain_unverified":
        host.fake.celery_listed = False
    elif failure == "stop_fails":
        host.fake.fail.append(("stop", "plane-space"))
    elif failure == "advance_stopped_fails":
        fail_advance_once(monkeypatch, "stopped")
    elif failure == "backup_fails":
        host.fake.pg_dump_code = 1
    elif failure == "advance_backed_up_fails":
        fail_advance_once(monkeypatch, "backed_up")
    elif failure == "slug_update_fails":
        host.orm.models["workspace"].fail_update = True
    elif failure == "slug_unrun_exit":
        sabotage(host, "step_slug", "unrun")
    elif failure == "slug_snippet_dies":
        host.shell.die_at = ("step_slug", 3)
    else:
        raise AssertionError(failure)


def clear(host):
    host.fake.celery_listed = True
    host.fake.pg_dump_code = 0
    host.orm.models["workspace"].fail_update = False
    host.fake.on_manage_shell = host.shell


PRE_SLUG_FAILURES = [
    "drain_unverified",
    "stop_fails",
    "advance_stopped_fails",
    "backup_fails",
    "advance_backed_up_fails",
    "slug_update_fails",
    "slug_unrun_exit",
    "slug_snippet_dies",
]


def resumed_order(calls):
    """Positions of the first stop, the pg_dump and the step_slug shell in a call list."""
    items = []
    for call in calls:
        if call[:2] == ("stop", "plane-beat-worker"):
            items.append("stop")
        elif "pg_dump" in " ".join(call):
            items.append("pg_dump")
        elif "manage.py" in call:
            items.append("shell")
    return items


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


def test_kill_after_slug_commit_with_services_running_keeps_the_backup(host):
    host.fake.crash_at("after_slug_done")
    with pytest.raises(Killed):
        host.deploy()
    host.fake.containers["plane"]["running"] = True
    assert "Rename complete" in host.deploy()
    assert "rotated_backups" not in host.receipt()  # the dump is the pre-change state
    assert sum("pg_dump" in " ".join(c) for c in host.fake.calls) == 1


def test_restop_failure_after_slug_commit_is_a_post_slug_failure(host):
    host.fake.crash_at("after_slug_done")
    with pytest.raises(Killed):
        host.deploy()
    host.fake.fail.append(("stop", "plane-space"))
    with pytest.raises(RuntimeError, match="Rerun apply to resume") as failure:
        host.deploy()
    assert "no data changed" not in str(failure.value)
    assert "(backed_up)" not in str(failure.value)  # no "stopped at X (X)" duplicate
    receipt = host.receipt()
    assert receipt["step"] == "backed_up" and "rotated_backups" not in receipt
    assert (host.directory / "backup.dump").exists()
    assert not [c for c in host.fake.calls if c[0] == "start"]
    assert "Rename complete" in host.deploy()


def test_restop_failure_with_unknown_slug_state_leaves_services_alone(host):
    host.fake.crash_at("after_slug_done")
    with pytest.raises(Killed):
        host.deploy()
    real = host.shell
    calls = {"n": 0}

    def flaky(text):  # the preflight probe works; every later snippet call fails
        calls["n"] += 1
        return real(text) if calls["n"] <= 2 else ("", 1)

    host.fake.on_manage_shell = flaky
    host.fake.fail.append(("stop", "plane-space"))
    mark = len(host.fake.calls)
    with pytest.raises(RuntimeError, match="slug state unknown"):
        host.deploy()
    assert not [c for c in host.fake.calls[mark:] if c[0] == "start"]
    assert host.receipt()["step"] == "backed_up"
    assert (host.directory / "backup.dump").exists()


def test_reset_rotates_the_journal_so_revert_restores_the_latest_originals(host):
    host.shell.die_at = ("step_slug", 3)  # journals workspace, c1, d1, then rolls back
    with pytest.raises(RuntimeError, match="Services restarted"):
        host.deploy()
    rotated = host.receipt()["rotated_journals"]
    assert not (host.directory / "records.jsonl").exists()
    assert '"id":"c1"' in (host.directory / rotated[0]).read_text()  # kept, never deleted
    comment = host.orm.models["comment"].rows[0]
    comment["comment_html"] = f'<a href="/{OLD}/x">edited by a user</a>'  # services were live
    edited = snapshot(host.orm)
    assert "Rename complete" in host.deploy()
    revert(host)  # reads only the current records.jsonl
    assert snapshot(host.orm) == edited


@pytest.mark.parametrize("step", ["slug_done", "rows_done", "cache_done"])
def test_resume_after_slug_done_restops_services(host, step):
    host.fake.crash_at("after_" + wr.step_order("apply")[wr.step_order("apply").index(step) + 1])
    with pytest.raises(Killed):
        host.deploy()
    assert host.receipt()["step"] == step
    host.fake.containers["plane"]["running"] = True
    mark = len(host.fake.calls)
    host.deploy()
    calls = host.fake.calls[mark:]
    stop = calls.index(("stop", "plane"))
    shells = [i for i, c in enumerate(calls) if "manage.py" in c]
    assert stop < shells[-1]


def test_step_slug_without_summary_but_renamed_continues_with_services_stopped(host):
    sabotage(host, "step_slug", "drop_summary")
    assert "Rename complete" in host.deploy()
    calls = host.fake.calls
    rows = [i for i, c in enumerate(calls) if "manage.py" in c][4]  # check, dry, slug, probe
    assert not [c for c in calls[:rows] if c[0] == "start"]
    assert "health" in host.ops()[: host.ops().index("step_rows")]  # the read-only probe


def test_step_slug_workspace_mismatch_probes_and_continues_when_renamed(host):
    sabotage(host, "step_slug", "mismatch")
    assert "Rename complete" in host.deploy()
    assert host.request("step_rows")[0]["workspace_id"] == "w1"


def test_step_slug_unrun_with_old_slug_held_is_a_pre_slug_failure(host):
    sabotage(host, "step_slug", "unrun")
    with pytest.raises(RuntimeError, match="Services restarted"):
        host.deploy()
    assert host.receipt()["step"] == "pending" and all(host.running().values())


def test_step_slug_ambiguous_outcome_stops_with_services_stopped(host):
    sabotage(host, "step_slug", "dead")
    with pytest.raises(RuntimeError, match="outcome unknown") as failure:
        host.deploy()
    assert "no data changed" not in str(failure.value)
    assert "Services stay stopped" in str(failure.value)
    assert not host.running()["plane"]
    assert not [c for c in host.fake.calls if c[0] == "start"]
    assert host.receipt()["step"] == "backed_up"
    clear(host)
    assert "Rename complete" in host.deploy()


def test_start_services_tries_every_service_and_names_failures(fake):
    stop(fake)
    fake.fail.append(("start", "plane-space"))
    fake.fail.append(("start", "plane-worker"))
    with pytest.raises(RuntimeError, match="could not start: space, worker"):
        wr.start_services(NAMES, fake.docker)
    started = [c[1] for c in fake.calls if c[0] == "start"]
    assert started == ["plane-admin", "plane-space", "plane", "plane-worker", "plane-beat-worker"]
    assert fake.containers["plane-beat-worker"]["running"]


def test_entry_reports_oserror_with_guidance_and_no_traceback(host, monkeypatch, capsys):
    blocker = host.directory.parent / "not-a-directory"
    blocker.write_text("")
    payload = host.payload(receipt_dir=str(blocker / "receipts"))
    assert run_remote(wr.REMOTE, payload, host, monkeypatch) == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err and "rerun apply" in err
    assert "Rename interrupted (FileExistsError)" in err


def test_entry_reports_interrupt_with_guidance_and_no_traceback(host, monkeypatch, capsys):
    def interrupted(text):
        if first_op(text) == "step_rows":
            raise KeyboardInterrupt
        return host.shell(text)

    host.fake.on_manage_shell = interrupted
    assert run_remote(wr.REMOTE, host.payload(), host, monkeypatch) == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err and "interrupted" in err.lower() and "revert" in err
    assert host.receipt()["step"] == "slug_done"


def test_entry_turns_a_real_sigterm_into_guidance_without_traceback():
    # The program blocks reading its payload, before any docker call; then SIGTERM arrives.
    process = subprocess.Popen(  # noqa: S603 - this interpreter, the assembled program
        [sys.executable, "-I", "-c", wr.REMOTE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(1)
    process.send_signal(signal.SIGTERM)
    out, err = process.communicate(timeout=10)
    assert process.returncode == 1
    assert b"Rename interrupted (KeyboardInterrupt)" in err and b"Traceback" not in err


def test_resume_runs_full_check_unless_at_or_past_slug_done(host, tmp_path):
    host.orm.models["workspace"].fail_update = True  # journals the workspace, then rolls back
    with pytest.raises(RuntimeError):
        host.deploy()
    journal = host.directory / host.receipt()["rotated_journals"][0]  # set aside by the reset
    assert '"kind":"workspace"' in journal.read_text()
    clear(host)
    mark = len(host.shell.requests)
    host.deploy()
    assert [r["op"] for r in host.shell.requests[mark:]][0] == "check"
    other = Host(tmp_path / "other")
    other.fake.crash_at("after_rows_done")
    with pytest.raises(Killed):
        other.deploy()
    mark = len(other.shell.requests)
    other.deploy()
    assert "check" not in [r["op"] for r in other.shell.requests[mark:]]


def test_apply_preflight_runs_cache_dry_run_and_saltbox_before_any_stop(host, capsys):
    host.deploy()
    first_stop = next(i for i, c in enumerate(host.fake.calls) if c[0] == "stop")
    before = host.fake.calls[:first_stop]
    assert ("inspect", "plane-db") in before
    assert host.shell.requests[1] == {"op": "flush_cache", "args": {"dry_run": True}}
    assert sum("manage.py" in c for c in before) == 2
    assert "Saltbox-managed host" in capsys.readouterr().out


# Snippet process death right after a journal line, before that row's write. step_slug lines:
# workspace, c1, d1, n1, e1 (one transaction, rolled back). step_rows lines: i1, p1 (per row).
DEATHS = [("step_slug", 1), ("step_slug", 3), ("step_rows", 1), ("step_rows", 2)]


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


# --- T8: revert from the receipt ---


def files(host):
    """Every file in the receipt directory with its bytes (journal, backup, rotations)."""
    return {
        p.name: p.read_bytes() for p in sorted(host.directory.iterdir()) if p.name != "receipt.json"
    }


def watch_restore(host):
    """Record the service states each time restore runs; return the list."""
    seen, real = [], host.fake.on_manage_shell

    def handler(text):
        if first_op(text) == "restore":
            seen.append(host.running())
        return real(text)

    host.fake.on_manage_shell = handler
    return seen


SERVICES = ["plane-beat-worker", "plane-worker", "plane", "plane-space", "plane-admin"]


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


def unknown_state(host):
    wr.new_receipt(wr.secure_receipt_dir(host.directory), OLD, NEW)
    receipt = host.receipt()
    receipt["step"] = "somewhere"
    wr.write_receipt(host.directory, receipt)


def unknown_phase(host):
    wr.new_receipt(wr.secure_receipt_dir(host.directory), OLD, NEW)
    receipt = host.receipt()
    receipt["phase"] = "elsewhere"
    wr.write_receipt(host.directory, receipt)


def no_workspace_record(host):
    wr.new_receipt(wr.secure_receipt_dir(host.directory), OLD, NEW)
    wr.append_journal(host.directory, {"kind": "row", "table": "issues", "id": "i1", "fields": {}})


def empty_journal_past_slug(host):
    host.deploy()
    (host.directory / "records.jsonl").rename(host.directory / "moved.jsonl")


def empty_journal_old_gone(host):
    wr.new_receipt(wr.secure_receipt_dir(host.directory), OLD, NEW)  # pending, no journal
    host.orm.models["workspace"].rows[0]["slug"] = "third"  # OLD is not live any more


def slug_elsewhere(host):
    host.deploy()
    host.orm.models["workspace"].rows[0]["slug"] = "third"


REVERT_REFUSALS = {
    "no_receipt_dir": (lambda host: None, "no receipt"),
    "no_receipt": (lambda host: wr.secure_receipt_dir(host.directory), "no receipt"),
    "other_rename": (
        lambda host: wr.new_receipt(wr.secure_receipt_dir(host.directory), "a", "b"),
        "different rename",
    ),
    "unknown_step": (unknown_state, "unknown"),
    "unknown_phase": (unknown_phase, "unknown"),
    "no_workspace_record": (no_workspace_record, "no workspace record"),
    "empty_journal_past_slug": (empty_journal_past_slug, "journal is empty"),
    "slug_state_unknown": (slug_elsewhere, "slug state unknown"),
    "empty_journal_slug_unknown": (empty_journal_old_gone, "slug state unknown"),
}


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


# Revert kill points: before and after each revert transition, around the restore, and
# inside the T6 stop and start operations.
REVERT_KILL_LABELS = [
    f"{when}_{step}" for step in wr.step_order("revert") for when in ("before", "after")
] + [
    "before_restore",
    "after_restore",
    "after_stop_plane-beat-worker",
    "after_drain",
    "after_restart_plane-live",
    "after_start_plane-admin",
    "before_start_plane-beat-worker",
]


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


def revert_inject(host, failure, monkeypatch):
    if failure == "stop_fails":
        host.fake.fail.append(("stop", "plane-space"))
    elif failure == "drain_unverified":
        host.fake.celery_listed = False
    elif failure == "advance_reverting_fails":
        fail_advance_once(monkeypatch, "reverting")
    elif failure == "restore_unrun":
        sabotage(host, "restore", "unrun")
    elif failure == "restore_summary_lost":
        sabotage(host, "restore", "drop_summary")  # committed, but the host cannot tell
    elif failure == "restore_write_fails":
        host.orm.models["workspace"].fail_update = True
    elif failure == "flush_fails":
        sabotage(host, "flush_cache", "unrun")
    elif failure == "advance_restored_fails":
        fail_advance_once(monkeypatch, "restored")
    elif failure == "start_fails":
        host.fake.fail.append(("start", "plane"))
    elif failure == "health_fails":
        host.shell.api["status"] = 503
    else:
        raise AssertionError(failure)


# Each one-shot failure, and the receipt step (or phase) the revert must leave behind.
REVERT_FAILURES = {
    "stop_fails": "reverting",
    "drain_unverified": "reverting",
    "advance_reverting_fails": "apply",
    "restore_unrun": "reverting",
    "restore_summary_lost": "reverting",
    "restore_write_fails": "reverting",
    "flush_fails": "reverting",
    "advance_restored_fails": "reverting",
    "start_fails": "restored",
    "health_fails": "started",
}


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
