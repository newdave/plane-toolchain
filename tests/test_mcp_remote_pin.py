"""The mcp-remote bridge runs from a vendored lockfile, never an unpinned npx install."""

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "mcp-remote"
EXACT = re.compile(r"^\d+\.\d+\.\d+$")


def test_package_json_pins_mcp_remote_exactly():
    pkg = json.loads((TOOL / "package.json").read_text())
    assert pkg["dependencies"] == {"mcp-remote": "0.16.6"}
    assert pkg["private"] is True


def test_lockfile_has_integrity_for_every_package():
    lock = json.loads((TOOL / "package-lock.json").read_text())
    assert lock["lockfileVersion"] == 3
    packages = {k: v for k, v in lock["packages"].items() if k}
    assert "integrity" in packages["node_modules/mcp-remote"]
    assert all("integrity" in v for v in packages.values() if not v.get("link"))


def test_node_pin_is_exact_and_matches_engines():
    mise = tomllib.loads((ROOT / "mise.toml").read_text())
    node = mise["tools"]["node"]
    assert EXACT.match(node)
    pkg = json.loads((TOOL / "package.json").read_text())
    assert pkg["engines"]["node"] == node


def test_bridge_uses_local_bin_without_npx():
    script = (ROOT / "scripts" / "plane-mcp.sh").read_text()
    assert "npx" not in script
    assert "tools/mcp-remote/node_modules/.bin/mcp-remote" in script
    assert "mise run setup" in script


def test_setup_installs_from_lockfile():
    mise = tomllib.loads((ROOT / "mise.toml").read_text())
    run = mise["tasks"]["setup"]["run"]
    cmds = [run] if isinstance(run, str) else run
    assert any("npm ci --ignore-scripts" in c and "tools/mcp-remote" in c for c in cmds)
