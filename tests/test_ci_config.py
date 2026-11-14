"""Repository CI config the ruleset and release gate depend on."""

import json
from pathlib import Path

ROOT = Path(__file__).parents[1]
GITHUB_ACTIONS_APP_ID = 15368


def test_required_checks_are_bound_to_github_actions():
    ruleset = json.loads((ROOT / ".github/rulesets/main.json").read_text())
    rule = next(r for r in ruleset["rules"] if r["type"] == "required_status_checks")
    checks = rule["parameters"]["required_status_checks"]
    assert checks
    for check in checks:
        assert check.get("integration_id") == GITHUB_ACTIONS_APP_ID, check
