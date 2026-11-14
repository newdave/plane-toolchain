import asyncio
import contextlib
import io
import json

import httpx2
import pytest
from plane_toolchain import github_evidence as ge
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


REPO = "example/demo"
HEAD = "b" * 40
MERGE = "c" * 40


def pull_request(number=5, body="Plane-Tasks: ENG-1", head=HEAD, **changes):
    pr = {
        "number": number,
        "state": "open",
        "draft": False,
        "merged": False,
        "title": "feat: thing",
        "body": body,
        "html_url": f"https://example.test/{REPO}/pull/{number}",
        "merge_commit_sha": MERGE,
        "user": {"login": "someone"},
        "head": {"sha": head, "ref": "topic", "repo": {"full_name": REPO}},
        "base": {"ref": "main", "repo": {"full_name": REPO}},
    }
    pr.update(changes)
    return pr


class FakeGitHub:
    def __init__(self, pr=None, commit_prs=None, files=()):
        self.pr = pr or pull_request()
        self.commit_prs = [self.pr] if commit_prs is None else commit_prs
        self.file_names = list(files)
        self.statuses = []
        self.calls = []

    def pull(self, number):
        self.calls.append("pull")
        return self.pr

    def pulls_for_commit(self, sha):
        self.calls.append("pulls_for_commit")
        return self.commit_prs

    def files(self, number):
        self.calls.append("files")
        return self.file_names

    def post_status(self, sha, state, description):
        self.calls.append("post_status")
        self.statuses.append((state, description))

    @property
    def states(self):
        return [state for state, _ in self.statuses]


class Server:
    """Stateful fake of the Plane tools the entry point uses."""

    def __init__(self, tasks=("ENG-1",), error=None):
        self.rows = [
            {"id": f"w-{name}", "external_source": "demo", "external_id": name} for name in tasks
        ]
        self.comments = []
        self.creates = 0
        self.connects = 0
        self.error = error

    def connect(self):
        self.connects += 1
        if self.error:
            raise self.error
        return self._session()

    @contextlib.asynccontextmanager
    async def _session(self):
        yield self.call

    async def call(self, name, arguments):
        action = arguments.get("action")
        if name == "project":
            return {"id": arguments["project_id"]}
        if name == "workitem":
            return {"results": self.rows, "next_page_results": False}
        assert name == "workitem_comment"
        if action == "create":
            self.creates += 1
            self.comments.append(
                {k: v for k, v in arguments.items() if k.startswith(("ext", "co"))}
            )
            return {}
        return {"results": self.comments, "next_page_results": False}


def configure(tmp_path, event, mode, **changes):
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event))
    summary = tmp_path / "summary.md"
    values = env(
        GITHUB_EVENT_PATH=str(path),
        GITHUB_STEP_SUMMARY=str(summary),
        GITHUB_SERVER_URL="https://example.test",
        GITHUB_RUN_ID="42",
        GITHUB_RUN_ATTEMPT="1",
        GITHUB_SHA=HEAD,
        **changes,
    )
    return cli.inputs(values, mode), values, summary


def gate_event(action="opened", **extra):
    event = {
        "action": action,
        "number": 5,
        "pull_request": {"updated_at": "2026-01-01T00:00:00Z"},
    }
    event.update(extra)
    return event


def gate(tmp_path, event, github, server, **changes):
    config, values, summary = configure(
        tmp_path, event, "gate", GITHUB_EVENT_NAME="pull_request_target", **changes
    )
    code = run(cli.run_gate(config, github, server.connect, values))
    return code, summary


def test_gate_success_posts_pending_then_success_and_one_comment(tmp_path):
    api, server = FakeGitHub(), Server()
    code, summary = gate(tmp_path, gate_event(), api, server)
    assert (code, api.states, server.creates) == (0, ["pending", "success"], 1)
    assert api.statuses[1][1] == ge.description(api.pr)
    assert server.comments[0]["external_source"] == "demo-github"
    assert "passed" in summary.read_text()


def test_gate_missing_field_posts_failure_with_cause(tmp_path, capsys):
    api, server = FakeGitHub(pr=pull_request(body="no field")), Server()
    code, summary = gate(tmp_path, gate_event(), api, server)
    assert (code, api.states, server.connects) == (1, ["pending", "failure"], 0)
    out = capsys.readouterr().out
    assert "::error title=Plane task references::Missing standalone Plane-Tasks field" in out
    assert "Missing standalone Plane-Tasks field" in summary.read_text()


def test_gate_plane_error_fails_closed(tmp_path):
    api, server = FakeGitHub(), Server(error=RuntimeError("down"))
    code, _ = gate(tmp_path, gate_event(), api, server)
    assert (code, api.states) == (1, ["pending", "failure"])


def test_gate_without_key_posts_failure(tmp_path, capsys):
    api, server = FakeGitHub(), Server()
    code, _ = gate(tmp_path, gate_event(), api, server, PLANE_API_KEY="")
    assert (code, api.states[-1], server.connects) == (1, "failure", 0)
    assert "PLANE_API_KEY unavailable" in capsys.readouterr().out


def test_gate_unknown_task_and_refusals(tmp_path):
    api, server = FakeGitHub(), Server(tasks=())
    code, _ = gate(tmp_path, gate_event(), api, server)
    assert (code, api.states[-1]) == (1, "failure")
    api = FakeGitHub()
    config, values, _ = configure(tmp_path, gate_event(), "gate", GITHUB_EVENT_NAME="pull_request")
    assert run(cli.run_gate(config, api, Server().connect, values)) == 1
    assert api.calls == []
    api = FakeGitHub()
    code, _ = gate(tmp_path, gate_event("labeled"), api, Server())
    assert (code, api.calls) == (0, [])


def test_gate_status_failure_is_named_too(tmp_path, capsys):
    class Broken(FakeGitHub):
        def post_status(self, sha, state, description):
            super().post_status(sha, state, description)
            if state == "success":
                raise cli.Failure("GitHub status failed (HTTP 403)")

    api = Broken()
    code, _ = gate(tmp_path, gate_event(), api, Server())
    assert (code, api.states) == (1, ["pending", "success", "failure"])
    assert "GitHub status failed (HTTP 403)" in capsys.readouterr().out


def test_gate_fetches_files_only_for_maintenance_marker(tmp_path):
    changes = {
        "PLANE_MAINTENANCE_FIELD": "Plane-Maintenance",
        "PLANE_MAINTENANCE_AUTHOR": "bot",
        "PLANE_MAINTENANCE_TITLE_PREFIX": "chore(deps)",
        "PLANE_MAINTENANCE_BRANCH_PREFIX": "dep/",
        "PLANE_MAINTENANCE_BASE": "main",
        "PLANE_MAINTENANCE_ALLOWED_FILES": "uv.lock",
    }
    api = FakeGitHub()
    gate(tmp_path, gate_event(), api, Server(), **changes)
    assert "files" not in api.calls
    pr = pull_request(
        body="Plane-Maintenance: dependency-update",
        title="chore(deps): bump",
        user={"login": "bot"},
    )
    pr["head"]["ref"] = "dep/x"
    api, server = FakeGitHub(pr=pr, files=["uv.lock"]), Server()
    code, _ = gate(tmp_path, gate_event(), api, server, **changes)
    assert (code, api.states, server.connects, "files" in api.calls) == (
        0,
        ["pending", "success"],
        0,
        True,
    )


def test_edited_title_only_exits_before_status(tmp_path):
    api, server = FakeGitHub(), Server()
    code, _ = gate(tmp_path, gate_event("edited", changes={"title": {"from": "old"}}), api, server)
    assert (code, api.statuses, server.connects) == (0, [], 0)


def test_edited_body_without_field_change_reposts_status_only(tmp_path):
    pr = pull_request(body="Plane-Tasks: ENG-1\n\nnew prose")
    api, server = FakeGitHub(pr=pr), Server()
    event = gate_event("edited", changes={"body": {"from": "Plane-Tasks: ENG-1\n\nold prose"}})
    code, _ = gate(tmp_path, event, api, server)
    assert (code, api.states, server.creates) == (0, ["pending", "success"], 0)
    assert api.statuses[1][1] == ge.description(pr)
    event = gate_event("edited", changes={"body": {"from": "Plane-Tasks: ENG-1"}})
    code, _ = gate(
        tmp_path, event, FakeGitHub(pr=pull_request(body="Plane-Tasks: ENG-1\nmore")), server
    )
    assert server.creates == 0
    event = gate_event("edited", changes={"body": {"from": "Plane-Tasks: ENG-2"}})
    code, _ = gate(tmp_path, event, FakeGitHub(), server)
    assert (code, server.creates) == (0, 1)


def evidence_event(tmp_path, name, **options):
    event = {"pull_request": {"head": {"sha": HEAD, "repo": {"full_name": REPO}}}}
    event["repository"] = {"default_branch": "main"}
    event.update(options)
    return event


def evidence(tmp_path, name, github, server, event=None, **changes):
    config, values, summary = configure(
        tmp_path,
        event or evidence_event(tmp_path, name),
        "evidence",
        GITHUB_EVENT_NAME=name,
        **changes,
    )
    return run(cli.run_evidence(config, github, server.connect, values)), summary


def test_evidence_pull_request_records_comment(tmp_path, capsys):
    api, server = FakeGitHub(), Server()
    code, summary = evidence(tmp_path, "pull_request", api, server)
    assert (code, server.creates) == (0, 1)
    comment = server.comments[0]
    assert comment["external_id"] == f"{REPO}:run:42:1:pr:5:task:w-ENG-1"
    assert "ci: success. Revision: " + HEAD in comment["comment_html"]
    assert "https://example.test/example/demo/actions/runs/42" in comment["comment_html"]
    assert "recorded 1 of 1" in summary.read_text()
    assert "::notice title=Plane evidence::" in capsys.readouterr().out


def test_evidence_skips_pr_without_reference_field(tmp_path, capsys):
    api, server = FakeGitHub(pr=pull_request(body="plain")), Server()
    code, _ = evidence(tmp_path, "pull_request", api, server)
    assert (code, server.connects) == (0, 0)
    assert "::notice" in capsys.readouterr().out
    api = FakeGitHub(commit_prs=[])
    assert evidence(tmp_path, "pull_request", api, server)[0] == 0


def test_evidence_malformed_field_is_a_warning(tmp_path, capsys):
    api = FakeGitHub(pr=pull_request(body="Plane-Tasks: nope"))
    code, summary = evidence(tmp_path, "pull_request", api, Server())
    assert code == 1
    assert "::warning title=Plane evidence::Plane-Tasks requires" in capsys.readouterr().out
    assert "not recorded" in summary.read_text()


@pytest.mark.parametrize(
    "name, event, ref",
    [
        ("pull_request_target", None, ""),
        (
            "pull_request",
            {"pull_request": {"head": {"sha": HEAD, "repo": {"full_name": "other/fork"}}}},
            "",
        ),
        ("pull_request", {"pull_request": {"head": {"sha": HEAD, "repo": None}}}, ""),
        ("push", {"repository": {"default_branch": "main"}}, "refs/heads/feature"),
        ("workflow_dispatch", None, ""),
    ],
)
def test_evidence_refusals(tmp_path, capsys, name, event, ref):
    api, server = FakeGitHub(), Server()
    code, _ = evidence(tmp_path, name, api, server, event=event or {}, GITHUB_REF=ref)
    assert (code, server.connects, api.calls) == (1, 0, [])
    assert "::warning" in capsys.readouterr().out


def test_evidence_without_key_skips_quietly(tmp_path, capsys):
    api, server = FakeGitHub(), Server()
    code, _ = evidence(tmp_path, "pull_request", api, server, PLANE_API_KEY="")
    assert (code, api.calls, server.connects) == (0, [], 0)
    out = capsys.readouterr().out
    assert "::notice title=Plane evidence::Plane evidence skipped: no PLANE_API_KEY" in out


def test_evidence_push_uses_commit_mapping_and_needs(tmp_path):
    other = pull_request(number=6, head="d" * 40, merge_commit_sha="e" * 40)
    merged = pull_request(number=5, head="f" * 40, merge_commit_sha=HEAD)
    api, server = FakeGitHub(commit_prs=[other, merged]), Server()
    needs = '{"ci":{"result":"skipped"},"build":{"result":"success"}}'
    code, _ = evidence(
        tmp_path,
        "push",
        api,
        server,
        event={"repository": {"default_branch": "main"}},
        GITHUB_REF="refs/heads/main",
        PLANE_EVIDENCE_CONCLUSION="",
        PLANE_EVIDENCE_NEEDS=needs,
        PLANE_EVIDENCE_LABEL="build",
    )
    assert (code, server.creates) == (0, 1)
    assert "build: success." in server.comments[0]["comment_html"]
    assert ":pr:5:" in server.comments[0]["external_id"]


def test_evidence_differing_content_is_refused_as_warning(tmp_path, capsys):
    api, server = FakeGitHub(), Server()
    assert evidence(tmp_path, "pull_request", api, server)[0] == 0
    code, _ = evidence(tmp_path, "pull_request", api, server, PLANE_EVIDENCE_CONCLUSION="failure")
    assert (code, server.creates) == (1, 1)
    assert "Existing evidence content differs" in capsys.readouterr().out


def test_evidence_preflight_names_forbidden(tmp_path, capsys):
    class Forbidden(Server):
        async def call(self, name, arguments):
            if name == "project":
                raise RuntimeError("403 forbidden")
            return await super().call(name, arguments)

    code, _ = evidence(tmp_path, "pull_request", FakeGitHub(), Forbidden())
    assert code == 1
    assert "Plane bot cannot read the project (HTTP 403)" in capsys.readouterr().out


def test_annotate_escapes_and_summarise(tmp_path, capsys):
    cli.annotate("warning", "a%b\r\nc")
    assert capsys.readouterr().out == "::warning title=Plane evidence::a%25b%0D%0Ac\n"
    buffer = io.StringIO()
    cli.annotate("error", "x", buffer, title="Plane task references")
    assert buffer.getvalue() == "::error title=Plane task references::x\n"
    summary = tmp_path / "s.md"
    cli.summarise("one\ntwo", {"GITHUB_STEP_SUMMARY": str(summary)})
    cli.summarise("three", {})
    assert summary.read_text() == "one two\n"


def test_evidence_secret_never_printed(tmp_path, capsys):
    leaking = RuntimeError("503 " + KEY)
    cases = [
        lambda: gate(tmp_path, gate_event(), FakeGitHub(pr=pull_request(body="x")), Server()),
        lambda: gate(tmp_path, gate_event(), FakeGitHub(), Server(error=leaking)),
        lambda: evidence(tmp_path, "pull_request", FakeGitHub(), Server(error=leaking)),
        lambda: evidence(tmp_path, "pull_request_target", FakeGitHub(), Server(), event={}),
        lambda: evidence(
            tmp_path, "pull_request", FakeGitHub(pr=pull_request(body="Plane-Tasks: no")), Server()
        ),
    ]
    for case in cases:
        code, summary = case()
        assert code == 1
        text = capsys.readouterr()
        assert KEY not in text.out + text.err + summary.read_text()
        assert summary.read_text()
        summary.unlink()
    assert KEY not in repr(cli.inputs(env(), "gate"))


def test_main_dispatch(monkeypatch, tmp_path, capsys):
    assert cli.main(["bogus"]) == 2
    assert cli.main([]) == 2
    summary = tmp_path / "s.md"
    monkeypatch.setenv("PLANE_COMMENT_SOURCE", "")
    monkeypatch.setenv("PLANE_TASK_SOURCE", "demo")
    monkeypatch.setenv("PLANE_API_KEY", KEY)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert cli.main(["gate"]) == 1
    assert cli.main(["evidence"]) == 1
    out = capsys.readouterr().out
    assert "::error title=Plane task references::PLANE_COMMENT_SOURCE must not be empty" in out
    assert "::warning title=Plane evidence::" in out
    monkeypatch.setenv("GITHUB_REPOSITORY", "not a repo")
    monkeypatch.setenv("PLANE_COMMENT_SOURCE", "x")
    assert cli.main(["gate"]) == 1


def test_main_runs_evidence_skip(monkeypatch, tmp_path):
    for name, value in env(PLANE_API_KEY="").items():
        monkeypatch.setenv(name, value)
    assert cli.main(["evidence"]) == 0


def test_github_unexpected_request_error_named():
    def boom(request):
        raise RuntimeError("Bearer t https://host/secret")

    with pytest.raises(cli.Failure) as caught:
        github(boom).pull(1)
    assert str(caught.value) == "GitHub pull failed (request error)"


def test_classify_never_passes_secret_text():
    cli.inputs(env(GH_TOKEN="ghs_TOKENVALUE"), "gate")  # noqa: S106
    for error in (
        ValueError("bad value " + KEY),
        cli.ToolError("failed " + KEY),
        ValueError("bad ghs_TOKENVALUE"),
    ):
        result = cli.classify(error)
        assert result in {"Unexpected ValueError", "Unexpected ToolError"}
    assert cli.classify(ValueError("fine text")) == "fine text"


def test_connect_failure_is_named(tmp_path, capsys):
    api, server = FakeGitHub(), Server(error=RuntimeError("HTTP 401 " + KEY))
    code, _ = gate(tmp_path, gate_event(), api, server)
    assert (code, api.states[-1]) == (1, "failure")
    out = capsys.readouterr().out
    assert "Plane connection failed (HTTP 401)" in out
    assert KEY not in out
    code, _ = evidence(tmp_path, "pull_request", FakeGitHub(), Server(error=RuntimeError("429")))
    assert code == 1
    assert "::warning title=Plane evidence::Plane connection failed (HTTP 429)" in (
        capsys.readouterr().out
    )


def test_pulls_for_commit_follows_pages_and_bounds():
    def handler(request):
        page = int(request.url.params["page"])
        rows = 100 if page == 1 else 2
        return httpx2.Response(200, json=[{"number": page * 1000 + i} for i in range(rows)])

    assert len(github(handler).pulls_for_commit(SHA)) == 102

    def full(request):
        return httpx2.Response(200, json=[{"number": 1}] * 100)

    with pytest.raises(cli.Failure, match="Commit PR listing incomplete"):
        github(full).pulls_for_commit(SHA)


def test_main_evidence_without_key_skips_before_validation(monkeypatch, capsys):
    for name, value in env(PLANE_API_KEY="", PLANE_EVIDENCE_CONCLUSION="passed").items():
        monkeypatch.setenv(name, value)
    assert cli.main(["evidence"]) == 0
    assert "Plane evidence skipped: no PLANE_API_KEY" in capsys.readouterr().out
