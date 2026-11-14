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


def test_ci_checks_pr_commit_messages_safely():
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = ci["jobs"]["check"]["steps"]
    checkout = steps[0]
    assert checkout["with"]["persist-credentials"] is False
    assert checkout["with"]["fetch-depth"] == 0
    step = next(s for s in steps if s.get("name") == "Check commit messages")
    assert step["if"] == "github.event_name == 'pull_request'"
    assert "scripts/check-commit-msg.py" in step["run"]
    assert "git rev-list --no-merges" in step["run"]
    # Untrusted event fields reach the shell only through env.
    assert "${{" not in step["run"]
    assert {"BASE_SHA", "HEAD_SHA", "PR_TITLE", "PR_BODY"} <= set(step["env"])
    assert not any("secrets." in str(v) for v in step["env"].values())
    assert ci["permissions"] == {"contents": "read"}


def test_renovate_actions_digests_do_not_automerge():
    cfg = json.loads((ROOT / "renovate.json").read_text())
    rules = cfg["packageRules"]
    idx = next(i for i, r in enumerate(rules) if r.get("matchManagers") == ["github-actions"])
    assert rules[idx]["automerge"] is False
    # Later rules win in Renovate: the override must follow the generic automerge rule.
    generic = next(i for i, r in enumerate(rules) if "digest" in r.get("matchUpdateTypes", []))
    assert idx > generic


def test_lint_ci_covers_actions():
    import tomllib

    mise = tomllib.loads((ROOT / "mise.toml").read_text())
    runs = mise["tasks"]["lint:ci"]["run"]
    assert "zizmor --offline .github/workflows .github/actions examples/github/*.yml" in runs
