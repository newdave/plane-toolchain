"""Project installer adds Plane integration without overwriting another project's work."""

import json
import os
import runpy
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
from plane_toolchain import session as plane_session
from plane_toolchain.config import set_enabled
from plane_toolchain.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
installer = runpy.run_path(str(ROOT / "scripts/install_project.py"))
apply, revert = installer["apply"], installer["revert"]


def snapshot(path):
    return {str(p.relative_to(path)): p.read_text() for p in sorted(path.rglob("*")) if p.is_file()}


def test_fresh_install_idempotent_and_clean_revert(tmp_path):
    apply(tmp_path, {"PLANE_WORKSPACE": "demo"})
    apply(tmp_path)
    mcp = json.loads((tmp_path / ".mcp.json").read_text())
    assert mcp["mcpServers"]["plane"]["args"] == [".plane/plane.sh", "mcp"]
    codex = tomllib.loads((tmp_path / ".codex/config.toml").read_text())
    assert codex["mcp_servers"]["plane"]["args"] == [".plane/plane.sh", "mcp"]
    for rel in (".claude/settings.json", ".codex/hooks.json"):
        hooks = json.loads((tmp_path / rel).read_text())["hooks"]["SessionStart"]
        assert "plane.sh" in hooks[0]["hooks"][0]["command"]
    assert "authoritative" in (tmp_path / "AGENTS.md").read_text()
    assert (tmp_path / "CLAUDE.md").read_text() == "@AGENTS.md\n"
    assert os.access(tmp_path / ".plane/plane.sh", os.X_OK)
    local = tmp_path / ".plane/local.env"
    assert local.stat().st_mode & 0o777 == 0o600
    assert f"PLANE_TOOLCHAIN={ROOT}" in local.read_text()
    assert "PLANE_WORKSPACE=demo" in local.read_text()
    assert "local.env" in (tmp_path / ".plane/.gitignore").read_text()

    revert(tmp_path)
    # Ignored local values stay, with the .gitignore that keeps them out of commits.
    assert sorted(p.name for p in (tmp_path / ".plane").iterdir()) == [".gitignore", "local.env"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [".plane"]
    local.unlink()
    apply(tmp_path)
    local.unlink()
    revert(tmp_path)
    assert not list(tmp_path.iterdir())


def test_existing_project_files_preserved_and_restored(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".codex").mkdir()
    files = {
        "AGENTS.md": "# Rules\n\nKeep tests green.\n",
        "CLAUDE.md": "@AGENTS.md\n",
        ".mcp.json": json.dumps({"mcpServers": {"other": {"command": "x"}}}, indent=2) + "\n",
        ".claude/settings.json": json.dumps(
            {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "y"}]}]}},
            indent=2,
        )
        + "\n",
        ".codex/config.toml": 'model = "m"\n',
    }
    for rel, text in files.items():
        (tmp_path / rel).write_text(text)
    apply(tmp_path)
    assert (tmp_path / "AGENTS.md").read_text().startswith(files["AGENTS.md"].rstrip() + "\n\n")
    assert (tmp_path / "CLAUDE.md").read_text() == "@AGENTS.md\n"
    assert set(json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]) == {
        "other",
        "plane",
    }
    assert tomllib.loads((tmp_path / ".codex/config.toml").read_text())["model"] == "m"
    (tmp_path / ".plane/local.env").unlink()
    revert(tmp_path)
    assert snapshot(tmp_path) == files


def test_claude_md_without_include_gets_block(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# Claude\n")
    apply(tmp_path)
    assert "plane-toolchain:begin" in (tmp_path / "CLAUDE.md").read_text()
    (tmp_path / ".plane/local.env").unlink()
    revert(tmp_path)
    assert snapshot(tmp_path) == {"CLAUDE.md": "# Claude\n"}


@pytest.mark.parametrize(
    ("rel", "text"),
    [
        (".mcp.json", '{"mcpServers": {"plane": {"command": "mine"}}}'),
        (".codex/config.toml", '[mcp_servers.plane]\ncommand = "mine"\n'),
        ("AGENTS.md", "<!-- plane-toolchain:begin -->\n"),
        (".plane/plane.sh", "mine"),
        (".mcp.json", "[]"),
    ],
)
def test_conflicts_refused_without_writing(tmp_path, rel, text):
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text(text)
    before = snapshot(tmp_path)
    with pytest.raises(ValueError, match=rel.replace(".", r"\.")):
        apply(tmp_path)
    assert snapshot(tmp_path) == before


def test_invalid_seed_value_refused_before_writing(tmp_path):
    with pytest.raises(ValueError, match="PLANE_WORKSPACE"):
        apply(tmp_path, {"PLANE_WORKSPACE": "demo $(x)"})
    assert not list(tmp_path.iterdir())


def test_edited_installation_never_reverted_or_reapplied(tmp_path):
    apply(tmp_path)
    agents = tmp_path / "AGENTS.md"
    agents.write_text(agents.read_text().replace("authoritative", "preferred"))
    before = snapshot(tmp_path)
    with pytest.raises(ValueError, match="AGENTS.md"):
        revert(tmp_path)
    with pytest.raises(ValueError, match="AGENTS.md"):
        apply(tmp_path)
    assert snapshot(tmp_path) == before


def test_symlinked_paths_refused(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    (target / ".claude").symlink_to(elsewhere)
    with pytest.raises(ValueError, match="symlink"):
        apply(target)
    assert not list(elsewhere.iterdir())


def test_installed_project_reads_only_its_plane_directory(tmp_path):
    (tmp_path / ".env").write_text("PLANE_ENABLED=true\nPLANE_WORKSPACE=wrong\n")
    apply(tmp_path, {"PLANE_WORKSPACE": "demo"})
    assert plane_session.switch(tmp_path, {}) == "disabled"
    assert plane_session.connection(tmp_path, {}) == {"PLANE_WORKSPACE": "demo"}
    local = tmp_path / ".plane/local.env"
    set_enabled(local, True, backup=tmp_path / ".plane/enable-backup.json")
    assert plane_session.switch(tmp_path, {}) == "enabled"
    assert (tmp_path / ".plane/enable-backup.json").exists()
    assert not (tmp_path / ".agent-state").exists()
    assert "PLANE_TOOLCHAIN=" in local.read_text()


def test_shim_reports_status_and_tolerates_missing_toolchain(tmp_path):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash unavailable")
    apply(tmp_path)
    shim = str(tmp_path / ".plane/plane.sh")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PLANE_")}

    def run(*args, **extra):
        return subprocess.run(  # noqa: S603 - fixed local shim
            [bash, shim, *args], env={**env, **extra}, capture_output=True, text=True, check=False
        )

    assert run("status").stdout.strip() == "disabled"
    assert "Plane: disabled" in json.loads(run("session").stdout)["systemMessage"]
    missing = run("session", PLANE_TOOLCHAIN=str(tmp_path / "absent"))
    assert missing.returncode == 0
    assert "invalid" in json.loads(missing.stdout)["systemMessage"]
    assert run("mcp", PLANE_TOOLCHAIN=str(tmp_path / "absent")).returncode == 1
    assert run("bogus").returncode == 2


def test_disabled_hint_names_installed_shim():
    with pytest.raises(ValueError, match=r"\.plane/plane\.sh enable"):
        Settings.from_env({"PLANE_CONFIG_ROOT": "/project"})
    with pytest.raises(ValueError, match="mise run plane:enable"):
        Settings.from_env({})


PROJECT = "11111111-1111-4111-8111-111111111111"
LABEL = installer["LABEL"]
ENV = {"PLANE_API_KEY": "fake-key", "PLANE_API_URL": "https://plane.example.test"}


class FakePlane:
    """Stands in for the Plane REST API; records calls and never touches the network."""

    def __init__(self, labels=()):
        self.labels, self.calls, self.keys = list(labels), [], []

    def __call__(self, base, key):
        self.keys.append(key)

        def request(method, path, body=None):
            self.calls.append((method, path, body))
            if method == "POST":
                self.labels.append(dict(body, id="label-1"))
            return {"results": list(self.labels), "next_page_results": False}

        return request


def labelled(tmp_path, enabled="true", **values):
    values = {"PLANE_WORKSPACE": "demo", "PLANE_PROJECT": PROJECT, **values}
    apply(tmp_path, {k: v for k, v in values.items() if v is not None})
    local = tmp_path / ".plane/local.env"
    local.write_text(local.read_text().replace("PLANE_ENABLED=false", f"PLANE_ENABLED={enabled}"))
    return tmp_path


def receipt(path):
    return json.loads((path / ".plane/install.json").read_text())


def test_rule_in_fresh_agents_block(tmp_path):
    apply(tmp_path)
    text = (tmp_path / "AGENTS.md").read_text()
    assert "`awaiting-decision`" in text
    assert "Remove the label when answered and resume." in text


def test_label_created_once_verified_and_recorded(tmp_path, capsys):
    labelled(tmp_path)
    plane = FakePlane()
    installer["label"](tmp_path, ENV, plane)
    posts = [c for c in plane.calls if c[0] == "POST"]
    assert posts == [("POST", f"/api/v1/workspaces/demo/projects/{PROJECT}/labels/", LABEL)]
    assert plane.calls[-1][0] == "GET"  # reread after the write
    assert plane.keys == ["fake-key"]
    assert receipt(tmp_path)["label"] == {"name": "awaiting-decision", "id": "label-1"}
    assert "fake-key" not in capsys.readouterr().out
    assert "fake-key" not in (tmp_path / ".plane/install.json").read_text()

    installer["label"](tmp_path, ENV, plane)
    assert len([c for c in plane.calls if c[0] == "POST"]) == 1
    assert "already exists" in capsys.readouterr().out


@pytest.mark.parametrize("field", ["color", "description"])
def test_label_with_different_definition_reported_and_unchanged(tmp_path, capsys, field):
    labelled(tmp_path)
    plane = FakePlane([dict(LABEL, id="other", **{field: "different"})])
    installer["label"](tmp_path, ENV, plane)
    assert [c for c in plane.calls if c[0] != "GET"] == []
    assert plane.labels[0][field] == "different"
    assert "different colour or description" in capsys.readouterr().out
    assert "label" not in receipt(tmp_path)


@pytest.mark.parametrize(
    ("setup", "env", "message"),
    [
        ({"enabled": "false"}, ENV, "disabled"),
        ({"PLANE_PROJECT": None}, ENV, "PLANE_PROJECT"),
        ({}, {"PLANE_API_KEY": "fake-key"}, "PLANE_API_URL"),
        ({}, {"PLANE_API_URL": ENV["PLANE_API_URL"]}, "PLANE_API_KEY"),
        ({}, {**ENV, "PLANE_API_URL": "http://plane.example.test"}, "PLANE_API_URL"),
    ],
)
def test_label_refuses_before_any_network_call(tmp_path, setup, env, message):
    labelled(tmp_path, **setup)
    plane = FakePlane()
    with pytest.raises(ValueError, match=message):
        installer["label"](tmp_path, env, plane)
    assert plane.calls == [] and plane.keys == []


def test_label_refuses_without_installation(tmp_path):
    plane = FakePlane()
    with pytest.raises(ValueError, match="receipt"):
        installer["label"](tmp_path, ENV, plane)
    assert plane.calls == []


def test_label_not_verified_is_an_error(tmp_path):
    labelled(tmp_path)
    plane = FakePlane()

    def lossy(base, key):
        request = plane(base, key)
        return lambda method, path, body=None: (
            {"results": []} if method == "GET" else request(method, path, body)
        )

    with pytest.raises(ValueError, match="verified"):
        installer["label"](tmp_path, ENV, lossy)


def test_revert_keeps_label_and_drops_receipt(tmp_path):
    labelled(tmp_path)
    plane = FakePlane()
    installer["label"](tmp_path, ENV, plane)
    (tmp_path / ".plane/local.env").unlink()
    revert(tmp_path)
    assert not list(tmp_path.iterdir())
    assert [c for c in plane.calls if c[0] == "DELETE"] == []


def legacy_install(path, files=None):
    """Install, then swap in the PTC-48 block, as an earlier installer would have written."""
    for rel, text in (files or {}).items():
        (path / rel).write_text(text)
    apply(path)
    agents = path / "AGENTS.md"
    assert installer["BLOCK"] in agents.read_text()
    agents.write_text(agents.read_text().replace(installer["BLOCK"], installer["LEGACY_BLOCK"], 1))


def test_legacy_block_lacks_rule_and_current_has_it():
    assert "awaiting-decision" not in installer["LEGACY_BLOCK"]
    assert "awaiting-decision" in installer["BLOCK"]


def test_revert_restores_legacy_install_exactly(tmp_path):
    legacy_install(tmp_path, {"AGENTS.md": "# Rules\n\nKeep tests green.\n"})
    (tmp_path / ".plane/local.env").unlink()
    revert(tmp_path)
    assert snapshot(tmp_path) == {"AGENTS.md": "# Rules\n\nKeep tests green.\n"}


def test_repeat_apply_on_legacy_install_reports_outdated_without_rewriting(tmp_path, capsys):
    legacy_install(tmp_path)
    before = snapshot(tmp_path)
    apply(tmp_path)
    assert snapshot(tmp_path) == before
    out = capsys.readouterr().out
    assert "already installed" in out and "predates the awaiting-decision rule" in out


def test_label_reports_outdated_block(tmp_path, capsys):
    labelled(tmp_path)
    agents = tmp_path / "AGENTS.md"
    agents.write_text(agents.read_text().replace(installer["BLOCK"], installer["LEGACY_BLOCK"]))
    installer["label"](tmp_path, ENV, FakePlane())
    assert "predates the awaiting-decision rule" in capsys.readouterr().out


def test_current_install_is_not_reported_outdated(tmp_path, capsys):
    apply(tmp_path)
    apply(tmp_path)
    assert "predates" not in capsys.readouterr().out


def test_edited_legacy_block_still_refused(tmp_path):
    legacy_install(tmp_path)
    agents = tmp_path / "AGENTS.md"
    agents.write_text(agents.read_text().replace("authoritative", "advisory"))
    (tmp_path / ".plane/local.env").unlink()
    with pytest.raises(ValueError, match="changed"):
        revert(tmp_path)
    with pytest.raises(ValueError, match="changed"):
        apply(tmp_path)
