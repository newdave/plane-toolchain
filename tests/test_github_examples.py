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
        {"GITHUB_REF": "refs/heads/feature"},
        {"PLANE_DEPLOYMENT_VERIFIED": "false"},
        {"PLANE_WORKITEM_ID": "task title"},
        {"GITHUB_RUN_ID": "$(attack)"},
    ],
)
def test_untrusted_or_unverified_write_rejected(change):
    with pytest.raises(ValueError):
        inputs(dict(ENV, **change))


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
        assert yaml.load(path.read_text(), Loader=yaml.BaseLoader)["on"]  # noqa: S506 - BaseLoader builds only str/list/dict
    assert not list(root.glob(".github/workflows/plane-*.yml"))
