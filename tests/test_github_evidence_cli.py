import asyncio
import json

import httpx2
import pytest
from plane_toolchain import github_evidence_cli as cli

SHA = "a" * 40


def client(handler, seen=None):
    def wrapped(request):
        if seen is not None:
            seen.append(request)
        return handler(request)

    return httpx2.Client(transport=httpx2.MockTransport(wrapped))


def github(handler, seen=None):
    return cli.GitHub("t", "example/demo", client=client(handler, seen))


def test_github_client_paths_and_auth():
    seen = []

    def handler(request):
        return httpx2.Response(200, json={"number": 7})

    api = github(handler, seen)
    assert api.pull(7) == {"number": 7}
    request = seen[0]
    assert (request.method, request.url.path) == ("GET", "/repos/example/demo/pulls/7")
    assert request.headers["Authorization"] == "Bearer t"
    assert request.headers["Accept"] == "application/vnd.github+json"
    api.post_status(SHA, "pending", "Plane-Tasks sha256:x")
    post = seen[1]
    assert (post.method, post.url.path) == ("POST", f"/repos/example/demo/statuses/{SHA}")
    assert json.loads(post.content) == {
        "state": "pending",
        "context": "Plane task references",
        "description": "Plane-Tasks sha256:x",
    }
    before = len(seen)
    with pytest.raises(cli.Failure, match="Invalid PR SHA"):
        api.post_status("abc", "pending", "x")
    assert len(seen) == before


def test_pulls_for_commit_path():
    seen = []
    api = github(lambda request: httpx2.Response(200, json=[{"number": 1}]), seen)
    assert api.pulls_for_commit(SHA) == [{"number": 1}]
    assert seen[0].url.path == f"/repos/example/demo/commits/{SHA}/pulls"
    with pytest.raises(cli.Failure, match="Invalid commit SHA"):
        api.pulls_for_commit("nope")


def test_files_paginates_and_bounds():
    def handler(request):
        page = int(request.url.params["page"])
        rows = 100 if page == 1 else 3
        return httpx2.Response(200, json=[{"filename": f"f{page}-{i}"} for i in range(rows)])

    assert len(github(handler).files(1)) == 103

    def full(request):
        return httpx2.Response(200, json=[{"filename": "x"}] * 100)

    with pytest.raises(cli.Failure, match="PR file listing incomplete"):
        github(full).files(1)


def test_github_error_named():
    api = github(lambda request: httpx2.Response(404, text="secret body"))
    with pytest.raises(cli.Failure) as caught:
        api.pull(1)
    assert cli.classify(caught.value) == "GitHub pull failed (HTTP 404)"


KEY = "pk_live_SECRETKEY123"
UUID = "00000000-0000-4000-8000-000000000001"


def env(**changes):
    base = {
        "PLANE_API_KEY": KEY,
        "PLANE_PROJECT": UUID,
        "PLANE_TASK_SOURCE": "demo",
        "PLANE_COMMENT_SOURCE": "demo-github",
        "GITHUB_REPOSITORY": "example/demo",
        "PLANE_EVIDENCE_LABEL": "ci",
        "PLANE_EVIDENCE_CONCLUSION": "success",
    }
    base.update(changes)
    return base


def run(coroutine):
    return asyncio.run(coroutine)


def test_inputs_defaults():
    config = cli.inputs(env(), "evidence")
    assert (config.refs.field, config.refs.minimum, config.refs.maximum) == ("Plane-Tasks", 1, 3)
    assert config.maintenance.field == ""
    assert (config.label, config.conclusion) == ("ci", "success")
    assert KEY not in repr(config)
    config = cli.inputs(
        env(PLANE_MAINTENANCE_ALLOWED_FILES="a\n\n b/** \n", PLANE_EVIDENCE_CONCLUSION=""), "gate"
    )
    assert config.maintenance.allowed_files == ("a", "b/**")


def test_inputs_needs_aggregates():
    needs = '{"ci":{"result":"skipped"},"build":{"result":"failure"}}'
    config = cli.inputs(env(PLANE_EVIDENCE_CONCLUSION="", PLANE_EVIDENCE_NEEDS=needs), "evidence")
    assert config.conclusion == "failure"


@pytest.mark.parametrize(
    "changes",
    [
        {"PLANE_REFERENCES_MAX": "0"},
        {"PLANE_REFERENCES_MIN": "3", "PLANE_REFERENCES_MAX": "2"},
        {"PLANE_REFERENCES_PATTERN": "(["},
        {"PLANE_COMMENT_SOURCE": ""},
        {"PLANE_EVIDENCE_NEEDS": '{"a":{"result":"success"}}'},
        {"PLANE_EVIDENCE_CONCLUSION": "passed"},
        {"PLANE_EVIDENCE_CONCLUSION": "", "PLANE_EVIDENCE_NEEDS": '{"a":{"result":"weird"}}'},
        {"PLANE_EVIDENCE_CONCLUSION": "", "PLANE_EVIDENCE_NEEDS": "[]x"},
        {"PLANE_EVIDENCE_CONCLUSION": "", "PLANE_EVIDENCE_NEEDS": "{}"},
        {"PLANE_EVIDENCE_CONCLUSION": ""},
        {"PLANE_EVIDENCE_LABEL": ""},
        {"GITHUB_RUN_ID": "x"},
    ],
)
def test_inputs_validation(changes):
    with pytest.raises(ValueError):
        cli.inputs(env(**changes), "evidence")


class Plane:
    """Fake MCP caller; `fail` maps (tool, action) to an exception."""

    def __init__(self, fail=None, tasks=1):
        self.fail = fail or {}
        self.tasks = tasks
        self.calls = []

    async def __call__(self, name, arguments):
        self.calls.append((name, arguments.get("action")))
        error = self.fail.get((name, arguments.get("action")))
        if error:
            raise error
        if name == "workitem":
            return {"results": [{"id": "w"}] * self.tasks}
        return {}


def test_preflight_names_forbidden():
    plane = Plane({("project", "retrieve"): RuntimeError("HTTP 403 forbidden " + KEY)})
    with pytest.raises(cli.Failure) as caught:
        run(cli.preflight(plane, UUID))
    assert str(caught.value) == "Plane bot cannot read the project (HTTP 403)"
    assert KEY not in str(caught.value)
    with pytest.raises(cli.Failure, match=r"Plane bot sees no tasks in the project"):
        run(cli.preflight(Plane(tasks=0), UUID))
    plane = Plane({("workitem", "list"): RuntimeError("401")})
    with pytest.raises(cli.Failure, match=r"Plane bot cannot list tasks \(HTTP 401\)"):
        run(cli.preflight(plane, UUID))
    plane = Plane({("workitem_comment", "list"): RuntimeError("500")})
    with pytest.raises(cli.Failure, match=r"Plane bot cannot list comments \(HTTP 500\)"):
        run(cli.preflight(plane, UUID))
    ok = Plane()
    run(cli.preflight(ok, UUID))
    assert ok.calls == [("project", "retrieve"), ("workitem", "list"), ("workitem_comment", "list")]


def test_classify_names_status_without_secret():
    plane = Plane({("workitem", "list"): RuntimeError(f"429 too many requests key={KEY}")})
    call = cli.retrying(plane, attempts=0)
    with pytest.raises(RuntimeError) as caught:
        run(call("workitem", {"action": "list"}))
    result = cli.classify(caught.value)
    assert result == "Plane workitem list failed (HTTP 429)"
    assert KEY not in result
    assert cli.classify(ValueError("Referenced Plane task missing: A-1")) == (
        "Referenced Plane task missing: A-1"
    )
    assert cli.classify(KeyError(KEY)) == "Unexpected KeyError"
    assert cli.classify(ExceptionGroup("g", [cli.Failure("fixed text")])) == "fixed text"


def test_retrying_reads_only():
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)

    results = [RuntimeError("429"), RuntimeError("429"), {"ok": True}]

    async def flaky(name, arguments):
        item = results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    assert run(cli.retrying(flaky, sleep=sleep)("workitem", {"action": "list"})) == {"ok": True}
    assert sleeps == [2, 4]

    calls = []

    async def create(name, arguments):
        calls.append(name)
        raise RuntimeError("429")

    with pytest.raises(RuntimeError):
        run(cli.retrying(create, sleep=sleep)("workitem_comment", {"action": "create"}))
    assert calls == ["workitem_comment"]
    assert sleeps == [2, 4]


def test_retrying_backoff_retry_after_and_limits():
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)

    async def always(name, arguments):
        raise RuntimeError("503")

    with pytest.raises(RuntimeError):
        run(cli.retrying(always, sleep=sleep)("project", {"action": "retrieve"}))
    assert sleeps == [2, 4, 8]

    sleeps.clear()
    waits = ["Retry-After: 7", "retry-after=999"]

    async def throttled(name, arguments):
        if waits:
            raise RuntimeError(waits.pop(0) + " 429")

    run(cli.retrying(throttled, sleep=sleep)("workitem", {"action": "list"}))
    assert sleeps == [7, 30]

    async def missing(name, arguments):
        raise RuntimeError("404")

    sleeps.clear()
    with pytest.raises(RuntimeError):
        run(cli.retrying(missing, sleep=sleep)("workitem", {"action": "list"}))
    assert sleeps == []


def test_github_transport_error_named():
    def boom(request):
        raise httpx2.ConnectError("https://host/secret")

    with pytest.raises(cli.Failure) as caught:
        github(boom).pull(1)
    assert str(caught.value) == "GitHub pull failed (network error)"
