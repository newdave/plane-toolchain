"""Structure checks for the composite actions (actionlint does not lint them)."""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
ACTIONS = ["plane-pr-gate", "plane-evidence"]


def load(name):
    path = ROOT / ".github/actions" / name / "action.yml"
    text = path.read_text()
    return text, yaml.load(text, Loader=yaml.BaseLoader)  # noqa: S506 - BaseLoader builds strings only


def steps(name):
    return load(name)[1]["runs"]["steps"]


@pytest.mark.parametrize("name", ACTIONS)
def test_action_is_composite(name):
    assert load(name)[1]["runs"]["using"] == "composite"


@pytest.mark.parametrize("name", ACTIONS)
def test_every_run_step_declares_shell(name):
    run_steps = [s for s in steps(name) if "run" in s]
    assert run_steps
    assert all(s.get("shell") == "bash" for s in run_steps)


@pytest.mark.parametrize("name", ACTIONS)
def test_inputs_never_interpolated_in_run(name):
    for step in steps(name):
        assert not re.search(r"\$\{\{\s*(inputs|github\.event)\.", step.get("run", ""))


@pytest.mark.parametrize("name", ACTIONS)
def test_uses_pinned_full_sha(name):
    text, _ = load(name)
    uses = [s["uses"] for s in steps(name) if "uses" in s]
    assert uses
    for value in uses:
        assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", value)
        assert re.search(rf"uses: {re.escape(value)}\s+# v\d", text)


@pytest.mark.parametrize("name", ACTIONS)
def test_locked_no_dev(name):
    text, _ = load(name)
    assert "uv sync --locked --no-dev" in text
    assert not re.search(r"uv sync --locked\n", text)


@pytest.mark.parametrize("name", ACTIONS)
def test_no_checkout_and_no_secrets_context(name):
    text, _ = load(name)
    assert "actions/checkout" not in text
    assert "secrets." not in text


@pytest.mark.parametrize("name", ACTIONS)
def test_env_maps_every_input(name):
    _, action = load(name)
    cli = next(s for s in action["runs"]["steps"] if "github_evidence_cli" in s.get("run", ""))
    env = cli["env"]
    mapped = set(re.findall(r"inputs\.([\w-]+)", " ".join(env.values())))
    assert mapped == set(action["inputs"])
    assert "inputs.github-token" in env["GH_TOKEN"]


@pytest.mark.parametrize("name", ACTIONS)
def test_action_enables_plane(name):
    # Settings.from_env refuses to connect unless PLANE_ENABLED is exactly "true".
    _, action = load(name)
    cli = next(s for s in action["runs"]["steps"] if "github_evidence_cli" in s.get("run", ""))
    assert cli["env"]["PLANE_ENABLED"] == "true"


def test_gate_has_no_continue_on_error():
    assert "continue-on-error" not in load("plane-pr-gate")[0]


@pytest.mark.parametrize("name", ACTIONS)
def test_no_private_identity(name):
    text, _ = load(name)
    assert "terrain" not in text.lower()
    assert "newdave.com" not in text
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", text)


def test_docs_link_adr_0002():
    adr = "docs/decisions/0002-github-pr-gate-and-evidence-events.md"
    assert adr in (ROOT / "docs/github-actions.md").read_text()
    assert "pull_request_target" in (ROOT / adr).read_text()
