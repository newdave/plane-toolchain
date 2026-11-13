"""Example automation must fail closed before remote writes."""

import asyncio
import hashlib
import json
import re
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
    return path.read_text(), yaml.load(path.read_text(), Loader=yaml.BaseLoader)  # noqa: S506 - BaseLoader builds strings only


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
        assert yaml.load(path.read_text(), Loader=yaml.BaseLoader)["on"]  # noqa: S506 - BaseLoader builds only str/list/dict
    assert not list(root.glob(".github/workflows/plane-*.yml"))


ALL_FILES = ["plane-check.yml", *WRITE_FILES]


@pytest.mark.parametrize("name", ALL_FILES)
def test_secret_holding_jobs_skip_dev_dependencies(name):
    text, _ = load(name)
    assert "uv sync --locked --no-dev" in text
    assert "uv run --locked --no-dev" in text
    assert "uv sync --locked\n" not in text
    assert "uv run --locked python" not in text


@pytest.mark.parametrize("name", WRITE_FILES)
def test_case_pattern_and_python_allow_list_match(name):
    text, doc = load(name)
    script = "\n".join(s.get("run", "") for s in doc["jobs"]["preconditions"]["steps"])
    pattern = re.search(r"^\s*(\S+\|\S+)\)\s*;;", script, re.M).group(1)
    assert set(pattern.split("|")) == set(ALLOWED) == module["TRUSTED_EVENTS"]


@pytest.mark.parametrize("name", ALL_FILES)
def test_no_status_override_and_secrets_scoped(name):
    _, doc = load(name)
    for job_name, job in doc["jobs"].items():
        assert not re.search(r"always\(|cancelled\(|failure\(", str(job.get("if", "")))
        if job_name == "preconditions":
            assert "secrets." not in yaml.dump(job)
    assert "secrets." in yaml.dump(doc["jobs"])


installer = runpy.run_path(str(Path(__file__).parents[1] / "scripts/github_examples.py"))
install = installer["install"]
EXAMPLES = Path(__file__).parents[1] / "examples/github"


def test_install_apply_then_revert_round_trip(tmp_path, capsys):
    install("apply", tmp_path)
    assert sorted(p.name for p in (tmp_path / ".github/workflows").iterdir()) == sorted(
        p.name for p in EXAMPLES.glob("*.yml")
    )
    install("apply", tmp_path)
    assert "already installed" in capsys.readouterr().out
    install("revert", tmp_path)
    assert not list((tmp_path / ".github/workflows").iterdir())


@pytest.mark.parametrize("parent", [".github", ".agent-state"])
def test_symlinked_parent_is_refused_and_nothing_is_written(tmp_path, parent):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    (target / parent).symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        install("apply", target)
    assert not list(outside.iterdir())


def test_outdated_install_is_detected(tmp_path):
    install("apply", tmp_path)
    receipt = tmp_path / ".agent-state/plane-toolchain/workflows.json"
    saved = json.loads(receipt.read_text())
    name = sorted(saved)[0]
    stale = b"name: older example\n"
    (tmp_path / ".github/workflows" / name).write_bytes(stale)
    saved[name] = hashlib.sha256(stale).hexdigest()
    receipt.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="outdated"):
        install("apply", tmp_path)
    install("revert", tmp_path)
    install("apply", tmp_path)


def test_interrupted_apply_is_completed_and_can_be_reverted(tmp_path):
    install("apply", tmp_path)
    workflows = tmp_path / ".github/workflows"
    victim = sorted(workflows.iterdir())[0]
    victim.unlink()
    install("apply", tmp_path)
    assert victim.is_file()
    victim.unlink()
    install("revert", tmp_path)
    assert not list(workflows.iterdir())


def test_retried_completion_does_not_repeat_comment_or_state_update():
    state = {"state": "other"}
    comments = []
    calls = []

    async def call(name, args):
        calls.append((name, args["action"]))
        if name == "state":
            return {"group": "completed"}
        if name == "workitem_comment":
            if args["action"] == "create":
                comments.append({"id": "c1", "comment_html": args["comment_html"]})
                return {"id": "c1"}
            if args["action"] == "list":
                return {"results": list(comments)}
            return comments[0]
        if args["action"] == "update":
            state["state"] = args["state"]
            return {}
        return dict(state)

    values = inputs(ENV)
    asyncio.run(publish(call, values))
    asyncio.run(publish(call, values))
    assert len(comments) == 1
    assert calls.count(("workitem", "update")) == 1


def test_comment_for_a_longer_run_id_does_not_count_as_this_run():
    values = inputs(dict(ENV, GITHUB_RUN_ID="12"))
    other = '<a href="https://github.com/example/demo/actions/runs/123">x</a>'
    created = []

    async def call(name, args):
        if name == "state":
            return {"group": "completed"}
        if name == "workitem_comment" and args["action"] == "list":
            return {"results": [{"comment_html": other}]}
        if name == "workitem_comment" and args["action"] == "create":
            created.append(args)
            return {"id": "c"}
        if name == "workitem_comment":
            return {"comment_html": values[4]}
        return {"state": values[3]}

    asyncio.run(publish(call, values))
    assert len(created) == 1
