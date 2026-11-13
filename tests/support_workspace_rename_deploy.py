"""Shared helpers and fixtures for the split test_workspace_rename_deploy files."""

import ast
import base64
import io
import json
import signal
import subprocess
import sys
import time
import pytest
from fake_docker import FakeDocker
from fake_orm import FakeOrm, ManageShell
from plane_toolchain import workspace_rename as wr
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


def revert(host):
    """The real revert state machine (T8), run to completion."""
    message = host.deploy("revert")
    assert "Revert complete" in message
    assert host.receipt()["phase"] == "revert" and host.receipt()["step"] == "reverted"
    return message


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


# Snippet process death right after a journal line, before that row's write. step_slug lines:
# workspace, c1, d1, n1, e1 (one transaction, rolled back). step_rows lines: i1, p1 (per row).
DEATHS = [("step_slug", 1), ("step_slug", 3), ("step_rows", 1), ("step_rows", 2)]


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
