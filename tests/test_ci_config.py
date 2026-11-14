"""Repository CI config the ruleset and release gate depend on."""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
GITHUB_ACTIONS_APP_ID = 15368


def test_required_checks_are_bound_to_github_actions():
    ruleset = json.loads((ROOT / ".github/rulesets/main.json").read_text())
    rule = next(r for r in ruleset["rules"] if r["type"] == "required_status_checks")
    checks = rule["parameters"]["required_status_checks"]
    assert checks
    for check in checks:
        assert check.get("integration_id") == GITHUB_ACTIONS_APP_ID, check


def test_ci_runs_mise_check_and_only_cancels_pull_requests():
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    # release.yml matches the check run by this exact job name.
    assert list(ci["jobs"]) == ["check"]
    steps = [s.get("run", "") for s in ci["jobs"]["check"]["steps"]]
    assert "mise run check" in steps
    assert not any(
        c.startswith(("uv run", "pytest", "ruff")) for s in steps for c in s.splitlines()
    )
    # A push to main must finish: release.yml needs a successful check on the tagged SHA.
    assert ci["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    release = (ROOT / ".github/workflows/release.yml").read_text()
    assert 'select(.name == "check"' in release
