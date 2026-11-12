"""Example automation must fail closed before remote writes."""

import asyncio
import runpy
from pathlib import Path

import pytest
import yaml

module = runpy.run_path(str(Path(__file__).parents[1] / "examples/github/plane_event.py"))
inputs, publish = module["inputs"], module["publish"]
ENV = {
    "PLANE_EXAMPLE_ACTION": "complete",
    "PLANE_PROJECT": "00000000-0000-4000-8000-000000000001",
    "PLANE_WORKITEM_ID": "00000000-0000-4000-8000-000000000002",
    "PLANE_DONE_STATE_ID": "00000000-0000-4000-8000-000000000003",
    "PLANE_DEFAULT_BRANCH": "main",
    "PLANE_DEPLOYMENT_VERIFIED": "true",
    "GITHUB_EVENT_NAME": "push",
    "GITHUB_REF": "refs/heads/main",
    "GITHUB_REPOSITORY": "example/demo",
    "GITHUB_RUN_ID": "123",
}


@pytest.mark.parametrize(
    "change",
    [
        {"GITHUB_EVENT_NAME": "pull_request"},
        {"GITHUB_EVENT_NAME": "pull_request_target"},
        {"GITHUB_EVENT_NAME": "issue_comment"},
        {"GITHUB_EVENT_NAME": "workflow_run"},
        {"GITHUB_EVENT_NAME": "pull_request_review"},
        {"GITHUB_EVENT_NAME": ""},
        {"GITHUB_REF": "refs/heads/feature"},
        {"PLANE_DEPLOYMENT_VERIFIED": "false"},
        {"PLANE_WORKITEM_ID": "task title"},
        {"GITHUB_RUN_ID": "$(attack)"},
    ],
)
def test_untrusted_or_unverified_write_rejected(change):
    with pytest.raises(ValueError):
        inputs(dict(ENV, **change))


@pytest.mark.parametrize("event", ["workflow_dispatch", "push", "release", "schedule"])
def test_allow_listed_events_accepted(event):
    assert inputs(dict(ENV, GITHUB_EVENT_NAME=event))[0] == "complete"


def test_missing_event_rejected():
    env = {k: v for k, v in ENV.items() if k != "GITHUB_EVENT_NAME"}
    with pytest.raises(ValueError):
        inputs(env)


ALLOWED = ("workflow_dispatch", "push", "release", "schedule")
WRITE_FILES = ["plane-complete.yml", "plane-comment.yml"]


def load(name):
    path = Path(__file__).parents[1] / "examples/github" / name
    return path.read_text(), yaml.load(path.read_text(), Loader=yaml.BaseLoader)


@pytest.mark.parametrize("name", WRITE_FILES)
def test_workflow_allow_lists_events_and_gates_write_job(name):
    text, doc = load(name)
    pre = doc["jobs"]["preconditions"]
    assert "secrets." not in yaml.dump(pre)
    script = "\n".join(s.get("run", "") for s in pre["steps"])
    assert "::error::" in script
    for event in ALLOWED:
        assert event in script
    for bad in ("issue_comment", "workflow_run", "pull_request"):
        assert bad not in script
    write = next(j for k, j in doc["jobs"].items() if k != "preconditions")
    assert write["needs"] == "preconditions"
    assert "uv sync --locked --no-dev" in text
    assert "uv sync --locked\n" not in text


def test_complete_preconditions_check_verification():
    _, doc = load("plane-complete.yml")
    steps = doc["jobs"]["preconditions"]["steps"]
    assert "DEPLOYMENT_VERIFIED" in yaml.dump(steps)


def test_readback_failure_prevents_state_update_and_write_is_not_retried():
    calls = []

    async def call(name, args):
        calls.append((name, args))
        if name == "state":
            return {"group": "completed"}
        if name == "workitem_comment" and args["action"] == "create":
            return {"id": "comment"}
        return {}

    with pytest.raises(ValueError, match="readback"):
        asyncio.run(publish(call, inputs(ENV)))
    assert sum(a["action"] == "create" for _, a in calls) == 1
    assert not any(a["action"] == "update" for _, a in calls)


def test_workflows_parse_and_do_not_activate_examples():
    root = Path(__file__).parents[1]
    for path in [*root.glob("examples/github/*.yml"), *root.glob(".github/workflows/*.yml")]:
        assert yaml.load(path.read_text(), Loader=yaml.BaseLoader)["on"]
    assert not list(root.glob(".github/workflows/plane-*.yml"))
