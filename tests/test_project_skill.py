"""The plane-tasks skill ships through the project installer (PTC-116)."""

import json
import re
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
installer = runpy.run_path(str(ROOT / "scripts/install_project.py"))
apply, revert = installer["apply"], installer["revert"]
TEMPLATE = ROOT / "templates/project/skills/plane-tasks/SKILL.md"
GUIDE = ROOT / "docs/agent-workflow.md"
PATHS = (".claude/skills/plane-tasks/SKILL.md", ".agents/skills/plane-tasks/SKILL.md")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def test_apply_installs_skill_for_both_clients_and_records_it(tmp_path):
    apply(tmp_path)
    receipt = json.loads((tmp_path / ".plane/install.json").read_text())
    for rel in PATHS:
        assert (tmp_path / rel).read_text() == TEMPLATE.read_text()
        assert receipt["entries"][rel] == {"kind": "skill", "created": True}
    assert set(PATHS) <= installer["PLANNED"]


def test_revert_removes_skill_and_the_directories_apply_created(tmp_path):
    apply(tmp_path)
    (tmp_path / ".plane/local.env").unlink()
    revert(tmp_path)
    assert not list(tmp_path.rglob("SKILL.md"))
    assert not list(tmp_path.iterdir())


def test_revert_keeps_directories_that_already_existed(tmp_path):
    (tmp_path / ".claude/skills/other").mkdir(parents=True)
    (tmp_path / ".claude/skills/other/SKILL.md").write_text("mine\n")
    apply(tmp_path)
    revert(tmp_path)
    assert (tmp_path / ".claude/skills/other/SKILL.md").read_text() == "mine\n"
    assert not (tmp_path / ".claude/skills/plane-tasks").exists()
    assert not (tmp_path / ".agents").exists()


def test_existing_skill_is_never_overwritten(tmp_path):
    path = tmp_path / PATHS[0]
    path.parent.mkdir(parents=True)
    path.write_text("mine\n")
    with pytest.raises(ValueError, match="already exists"):
        apply(tmp_path)
    assert path.read_text() == "mine\n"
    assert not (tmp_path / ".plane/install.json").exists()


def test_edited_skill_refuses_revert(tmp_path):
    apply(tmp_path)
    path = tmp_path / PATHS[1]
    path.write_text(path.read_text() + "extra\n")
    with pytest.raises(ValueError, match="changed since installation"):
        revert(tmp_path)
    assert (tmp_path / ".plane/install.json").exists()


def test_revert_accepts_crlf_checkout_of_the_skill(tmp_path):
    apply(tmp_path)
    for rel in PATHS:
        with (tmp_path / rel).open("w", encoding="utf-8", newline="") as out:
            out.write(TEMPLATE.read_text().replace("\n", "\r\n"))
    revert(tmp_path)
    assert not list(tmp_path.rglob("SKILL.md"))


def test_symlinked_skills_directory_is_refused(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    target = tmp_path / "target"
    (target / ".agents").mkdir(parents=True)
    (target / ".agents/skills").symlink_to(elsewhere)
    target_before = sorted(p.name for p in target.rglob("*"))
    with pytest.raises(ValueError, match="symlink"):
        apply(target)
    assert not list(elsewhere.iterdir())
    assert sorted(p.name for p in target.rglob("*")) == target_before


def test_interrupted_apply_is_reverted_with_the_skill(tmp_path, monkeypatch):
    scope = installer["apply"].__globals__
    real = scope["write"]

    def failing(path, text, mode=None):
        if path.name == "SKILL.md":
            raise OSError("disk full")
        real(path, text, mode)

    monkeypatch.setitem(scope, "write", failing)
    with pytest.raises(OSError, match="disk full"):
        apply(tmp_path)
    monkeypatch.setitem(scope, "write", real)
    revert(tmp_path)
    assert not list(tmp_path.rglob("SKILL.md"))
    for client in (".claude", ".agents"):
        assert not (tmp_path / client / "skills/plane-tasks").exists()


@pytest.mark.parametrize("path", [TEMPLATE, GUIDE], ids=["skill", "guide"])
def test_shipped_text_has_no_downstream_paths_or_private_identities(path):
    text = path.read_text()
    for banned in ("terrain", "docs/tasks/", "docs/prps/", ".agent-state", "superpowers"):
        assert banned not in text.lower(), banned
    assert not UUID.search(text)
    assert not re.search(r"https?://(?!github\.com/newdave/plane-toolchain)", text)


def test_skill_states_the_rules_it_ports():
    text = TEMPLATE.read_text()
    for needed in (
        "name: plane-tasks",
        "blocked-by",
        "ce_intake",
        "`issue` ID",
        "ce_page",
        "expected_updated_at",
        "force=true",
        "Plane-Tasks:",
        "column 0",
        "[A-Z][A-Z0-9]*-\\d+[A-Z]?",
        "duplicate_to",
        "rejected",
        "awaiting-decision",
        "health check",
        "## Context",
        "## Scope",
        "## Acceptance",
        "Relation deletion is unsupported",
    ):
        assert needed in text, needed
    assert "remain unsupported" not in text


def test_skill_is_formatter_stable_markdown():
    text = TEMPLATE.read_text()
    assert text.startswith("---\nname: plane-tasks\n")
    assert text.endswith("\n") and not text.endswith("\n\n")
    assert "\r" not in text and "\t" not in text
    for number, line in enumerate(text.splitlines(), 1):
        assert line == line.rstrip(), number
        assert not re.match(r"\s*[*+] ", line), number
        assert not line.startswith("|"), number
    assert "\n\n\n" not in text


def test_revert_survives_a_directory_that_cannot_be_removed(tmp_path, monkeypatch, capsys):
    apply(tmp_path)
    (tmp_path / ".plane/local.env").unlink()
    real = Path.rmdir

    def failing(self):
        if self.name == "plane-tasks" and ".agents" in self.parts:
            raise OSError("busy")
        real(self)

    monkeypatch.setattr(Path, "rmdir", failing)
    revert(tmp_path)
    assert not (tmp_path / ".plane/install.json").exists()
    assert "Left in place" in capsys.readouterr().out
    assert (tmp_path / ".agents/skills/plane-tasks").is_dir()
    assert not (tmp_path / ".claude").exists()


def test_revert_leaves_a_directory_swapped_for_a_symlink(tmp_path):
    apply(tmp_path)
    (tmp_path / ".plane/local.env").unlink()
    receipt = json.loads((tmp_path / ".plane/install.json").read_text())
    assert ".codex" in receipt["directories"]
    (tmp_path / ".codex/config.toml").unlink()
    (tmp_path / ".codex/hooks.json").unlink()
    (tmp_path / ".codex").rmdir()
    elsewhere = tmp_path.parent / f"{tmp_path.name}-elsewhere"
    elsewhere.mkdir()
    (tmp_path / ".codex").symlink_to(elsewhere)
    with pytest.raises(ValueError, match="removed since installation|symlink"):
        revert(tmp_path)
    assert elsewhere.is_dir()
