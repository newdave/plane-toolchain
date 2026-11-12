"""Offline Plane status shared by Codex and Claude startup hooks."""

import json
import os
from pathlib import Path

import pytest
from plane_toolchain import session as plane_session


@pytest.mark.parametrize("enabled", ["true", "false"])
def test_local_switch_is_reported_without_executing_shell(tmp_path, enabled):
    local = tmp_path / "config/local.env"
    local.parent.mkdir()
    local.write_text(f"OTHER=$(touch should-not-exist)\nexport PLANE_ENABLED={enabled}\n")
    message = plane_session.status(tmp_path, {})
    assert ("enabled" if enabled == "true" else "disabled") in message
    assert "live connection not checked" in message
    assert not (tmp_path / "should-not-exist").exists()


def test_environment_override_and_invalid_value(tmp_path):
    assert "enabled" in plane_session.status(tmp_path, {"PLANE_ENABLED": "true"})
    assert "invalid" in plane_session.status(tmp_path, {"PLANE_ENABLED": "$(private)"})
    assert "private" not in plane_session.status(tmp_path, {"PLANE_ENABLED": "$(private)"})
    assert "disabled" in plane_session.status(tmp_path, {})


def test_legacy_env_precedence(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/defaults.env").write_text("PLANE_ENABLED=false\n")
    (tmp_path / ".env").write_text("PLANE_ENABLED=true\n")
    assert plane_session.switch(tmp_path, {}) == "enabled"
    (tmp_path / "config/local.env").write_text("PLANE_ENABLED=false\n")
    assert plane_session.switch(tmp_path, {}) == "disabled"


def test_connection_values_read_literally_with_environment_precedence(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/local.env").write_text(
        "PLANE_MCP_URL=https://mcp.example.org/http/api-key/mcp # endpoint\n"
        "export PLANE_WORKSPACE='demo'\n"
        "PLANE_PROJECT=$(touch should-not-exist)\n"
        "PLANE_API_KEY=never-read-from-files\n"
    )
    values = plane_session.connection(tmp_path, {"PLANE_PROJECT": "from-env"})
    assert values == {
        "PLANE_MCP_URL": "https://mcp.example.org/http/api-key/mcp",
        "PLANE_WORKSPACE": "demo",
    }
    assert not (tmp_path / "should-not-exist").exists()


def test_both_clients_register_same_startup_hook():
    root = Path(__file__).resolve().parents[1]
    for path in (root / ".codex/hooks.json", root / ".claude/settings.json"):
        hooks = json.loads(path.read_text())["hooks"]["SessionStart"]
        assert any("session.py" in entry["command"] for group in hooks for entry in group["hooks"])


def test_zsh_prompt_updates_and_restores_without_global_changes(tmp_path):
    import os
    import shutil
    import subprocess

    zsh = shutil.which("zsh")
    if not zsh:
        pytest.skip("zsh unavailable")
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "src/plane_toolchain").mkdir(parents=True)
    shutil.copyfile(
        root / "src/plane_toolchain/session.py", tmp_path / "src/plane_toolchain/session.py"
    )
    (tmp_path / "config").mkdir()
    env = {k: v for k, v in os.environ.items() if k != "PLANE_ENABLED"}
    env.update(
        MISE_PROJECT_ROOT=str(tmp_path), PROMPT_SCRIPT=str(root / "scripts/plane-prompt.zsh")
    )
    result = subprocess.run(  # noqa: S603 - fixed local shell fixture
        [
            zsh,
            "-f",
            "-c",
            """
PS1='original> '
source "$PROMPT_SCRIPT" "$MISE_PROJECT_ROOT"
_plane_toolchain_prompt
[[ $PS1 == '[Plane:disabled] original> ' ]] || exit 1
print 'PLANE_ENABLED=true' > "$MISE_PROJECT_ROOT/config/local.env"
_plane_toolchain_prompt
[[ $PS1 == '[Plane:enabled] original> ' ]] || exit 2
source "$PROMPT_SCRIPT" "$MISE_PROJECT_ROOT"
_plane_toolchain_prompt
[[ $PS1 == '[Plane:enabled] original> ' ]] || exit 3
_plane_toolchain_prompt_remove
[[ $PS1 == 'original> ' ]] || exit 4
[[ ${precmd_functions[(Ie)_plane_toolchain_prompt]} == 0 ]] || exit 5
""",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_unreadable_file_reports_invalid_not_traceback(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/local.env").write_bytes(b"PLANE_ENABLED=\xff\xfe\n")
    assert plane_session.switch(tmp_path, {}) == "invalid"
    assert "invalid" in plane_session.status(tmp_path, {})
    assert plane_session.connection(tmp_path, {}) == {}


def test_unreadable_file_by_permission_reports_invalid(tmp_path):
    (tmp_path / "config").mkdir()
    local = tmp_path / "config/local.env"
    local.write_text("PLANE_ENABLED=true\n")
    local.chmod(0)
    try:
        if os.access(local, os.R_OK):
            pytest.skip("chmod 0 has no effect (running as root)")
        assert plane_session.switch(tmp_path, {}) == "invalid"
        assert plane_session.connection(tmp_path, {}) == {}
    finally:
        local.chmod(0o600)
