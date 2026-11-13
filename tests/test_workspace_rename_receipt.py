import json
import os
import stat

import pytest

from plane_toolchain import workspace_rename as wr


@pytest.fixture
def home(tmp_path):
    return tmp_path / "state" / "workspace-rename"


def test_default_receipt_dir_and_env_override(tmp_path):
    assert (
        wr.receipt_dir({}, home=tmp_path)
        == tmp_path / ".local/state/plane-toolchain/workspace-rename"
    )
    assert (
        wr.receipt_dir({"PLANE_RENAME_RECEIPT_DIR": str(tmp_path / "x")}, home=tmp_path)
        == tmp_path / "x"
    )


def test_receipt_dir_created_0700_files_0600(home):
    wr.secure_receipt_dir(home)
    wr.new_receipt(home, "acme", "bolt")
    wr.append_journal(home, {"kind": "workspace", "id": "1", "slug": "acme"})
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    for name in ("receipt.json", "records.jsonl"):
        assert stat.S_IMODE((home / name).stat().st_mode) == 0o600


def test_refuses_dir_with_loose_permissions_or_symlink_or_foreign_owner(tmp_path, home):
    home.mkdir(parents=True)
    home.chmod(0o755)
    with pytest.raises(RuntimeError):
        wr.secure_receipt_dir(home)
    home.chmod(0o700)
    with pytest.raises(RuntimeError):
        wr.secure_receipt_dir(home, uid=os.geteuid() + 1)
    link = tmp_path / "link"
    link.symlink_to(home)
    with pytest.raises(RuntimeError):
        wr.secure_receipt_dir(link)


def test_refuses_file_with_loose_permissions_or_symlink(tmp_path, home):
    wr.new_receipt(home, "acme", "bolt")
    (home / "receipt.json").chmod(0o644)
    with pytest.raises(RuntimeError):
        wr.load_receipt(home, "acme", "bolt")
    (home / "receipt.json").chmod(0o600)
    other = tmp_path / "other.json"
    other.write_text("{}")
    other.chmod(0o600)
    (home / "receipt.json").unlink()
    (home / "receipt.json").symlink_to(other)
    with pytest.raises(RuntimeError):
        wr.load_receipt(home, "acme", "bolt")


def test_receipt_write_is_atomic_and_fsyncs_file_and_dir(home, monkeypatch):
    wr.new_receipt(home, "acme", "bolt")
    kinds = []
    real = os.fsync
    monkeypatch.setattr(
        os, "fsync", lambda fd: (kinds.append(stat.S_ISDIR(os.fstat(fd).st_mode)), real(fd))
    )
    wr.advance(home, "stopped")
    assert sorted(kinds) == [False, True]

    def boom(*args):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        wr.advance(home, "backed_up")
    assert wr.load_receipt(home, "acme", "bolt")["step"] == "stopped"


def test_journal_append_fsyncs_each_line(home, monkeypatch):
    wr.secure_receipt_dir(home)
    calls = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append(fd), real(fd)))
    wr.append_journal(home, {"kind": "workspace", "id": "1", "slug": "acme"})
    first = len(calls)
    wr.append_journal(
        home, {"kind": "row", "table": "t", "id": "2", "fields": {"b": "x", "a": "y"}}
    )
    assert first >= 1 and len(calls) > first
    lines = (home / "records.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert lines[1] == json.dumps(json.loads(lines[1]), sort_keys=True, separators=(",", ":"))


def test_state_only_moves_forward(home):
    wr.new_receipt(home, "acme", "bolt")
    for step in ("stopped", "backed_up", "slug_done"):
        assert wr.advance(home, step)["step"] == step
    with pytest.raises(RuntimeError):
        wr.advance(home, "stopped")
    assert wr.advance(home, "slug_done")["step"] == "slug_done"  # same step is a safe re-run
    with pytest.raises(RuntimeError):
        wr.advance(home, "nonsense")


def test_full_apply_then_revert_sequences(home):
    wr.new_receipt(home, "acme", "bolt")
    for step in (
        "stopped",
        "backed_up",
        "slug_done",
        "rows_done",
        "cache_done",
        "started",
        "complete",
    ):
        wr.advance(home, step)
    for step in ("reverting", "restored", "started", "reverted"):
        assert wr.advance(home, step)["step"] == step
    with pytest.raises(RuntimeError):
        wr.advance(home, "complete")
    with pytest.raises(RuntimeError):
        wr.advance(home, "restored")


def test_revert_can_start_from_partial_apply_state(home):
    wr.new_receipt(home, "acme", "bolt")
    wr.advance(home, "slug_done")
    assert wr.advance(home, "reverting")["step"] == "reverting"


def test_refuses_receipt_for_other_rename(home):
    wr.new_receipt(home, "acme", "bolt")
    with pytest.raises(RuntimeError):
        wr.load_receipt(home, "acme", "other")
    with pytest.raises(RuntimeError):
        wr.new_receipt(home, "acme", "other")


def test_new_receipt_resumes_same_rename(home):
    wr.new_receipt(home, "acme", "bolt")
    wr.advance(home, "stopped")
    assert wr.new_receipt(home, "acme", "bolt")["step"] == "stopped"


def test_load_receipt_absent_is_none(home):
    wr.secure_receipt_dir(home)
    assert wr.load_receipt(home, "acme", "bolt") is None


def test_torn_trailing_journal_line_ignored_on_resume(home):
    wr.append_journal(home, {"kind": "workspace", "id": "1", "slug": "acme"})
    with open(home / "records.jsonl", "a") as handle:
        handle.write('{"kind":"row","ta')
    assert wr.load_journal(home) == [{"kind": "workspace", "id": "1", "slug": "acme"}]


def test_corrupt_middle_journal_line_refused(home):
    wr.append_journal(home, {"kind": "workspace", "id": "1", "slug": "acme"})
    with open(home / "records.jsonl", "a") as handle:
        handle.write("garbage\n")
    wr.append_journal(home, {"kind": "workspace", "id": "2", "slug": "acme"})
    with pytest.raises(RuntimeError):
        wr.load_journal(home)


def test_receipt_never_printed(home, capsys):
    canary = "CANARY-secret-body"
    wr.new_receipt(home, "acme", "bolt")
    wr.append_journal(home, {"kind": "row", "table": "t", "id": "1", "fields": {"c": canary}})
    with open(home / "records.jsonl", "a") as handle:
        handle.write("not json " + canary + "\n")
    wr.append_journal(home, {"kind": "row", "table": "t", "id": "2", "fields": {}})
    with pytest.raises(RuntimeError) as caught:
        wr.load_journal(home)
    (home / "receipt.json").chmod(0o644)
    with pytest.raises(RuntimeError) as loose:
        wr.load_receipt(home, "acme", "bolt")
    out = capsys.readouterr()
    assert canary not in out.out + out.err + str(caught.value) + str(loose.value)


def test_append_after_torn_final_line_keeps_every_record(home):
    first = {"kind": "workspace", "id": "1", "slug": "acme"}
    second = {"kind": "row", "table": "t", "id": "2", "fields": {}}
    wr.append_journal(home, first)
    with open(home / "records.jsonl", "ab") as handle:
        handle.write(b'{"kind":"row","ta')
    wr.append_journal(home, second)
    assert wr.load_journal(home) == [first, second]
    assert (home / "records.jsonl").read_bytes().endswith(b"\n")
    wr.append_journal(home, first)
    assert wr.load_journal(home) == [first, second, first]


def test_append_after_torn_first_line_only(home):
    wr.secure_receipt_dir(home)
    (home / "records.jsonl").write_bytes(b'{"kind":"wor')
    (home / "records.jsonl").chmod(0o600)
    record = {"kind": "workspace", "id": "1", "slug": "acme"}
    wr.append_journal(home, record)
    assert wr.load_journal(home) == [record]


def test_append_keeps_whole_record_missing_only_its_newline(home):
    kept = {"kind": "workspace", "id": "1", "slug": "acme"}
    new = {"kind": "row", "table": "t", "id": "2", "fields": {}}
    wr.append_journal(home, kept)
    path = home / "records.jsonl"
    path.write_bytes(path.read_bytes().rstrip(b"\n"))
    wr.append_journal(home, new)
    assert wr.load_journal(home) == [kept, new]


def test_append_repair_fsyncs_the_truncation(home, monkeypatch):
    wr.append_journal(home, {"kind": "workspace", "id": "1", "slug": "acme"})
    with open(home / "records.jsonl", "ab") as handle:
        handle.write(b'{"kind":"row","ta')
    calls = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append(fd), real(fd)))
    wr.append_journal(home, {"kind": "row", "table": "t", "id": "2", "fields": {}})
    assert len(calls) >= 2  # one after the repair, one after the new line


def test_torn_tail_alone_still_loads(home):
    record = {"kind": "workspace", "id": "1", "slug": "acme"}
    wr.append_journal(home, record)
    with open(home / "records.jsonl", "ab") as handle:
        handle.write(b'{"kind":"row","ta')
    assert wr.load_journal(home) == [record]
