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


def test_github_transport_error_named():
    def boom(request):
        raise httpx2.ConnectError("https://host/secret")

    with pytest.raises(cli.Failure) as caught:
        github(boom).pull(1)
    assert str(caught.value) == "GitHub pull failed (network error)"
