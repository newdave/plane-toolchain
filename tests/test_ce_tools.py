"""CE relation adapter keeps official API-key authentication and direction."""

from types import SimpleNamespace

import pytest
from plane_toolchain.ce_tools import relation_result


def test_normalises_existing_ce_relation_response_without_losing_other_types():
    data = {
        "blocked_by": [{"issue_id": "parent", "project_id": "project"}],
        "blocking": [],
        "relates_to": [{"issue_id": "owner"}],
        "duplicate": [],
    }
    result = relation_result(SimpleNamespace(model_dump=lambda: data))
    assert result["dependencies"]["blocked_by"] == [{"id": "parent", "project_id": "project"}]
    assert result["custom"]["relates_to"] == [{"id": "owner", "project_id": None}]
    assert relation_result(data) == result


def test_relation_rows_from_other_projects_keep_their_own_project_id():
    # CE relations are workspace-wide: ids from other projects are listed, never filtered,
    # and each carries its project_id so the caller can tell (documented in docs/extensions.md).
    data = {"blocking": [{"issue_id": "far", "project_id": "other-project"}]}
    result = relation_result(data)
    assert result["dependencies"]["blocking"] == [{"id": "far", "project_id": "other-project"}]


def test_invalid_relation_identity_fails_closed():
    with pytest.raises(ValueError):
        relation_result(SimpleNamespace(model_dump=lambda: {"blocked_by": [{}]}))


def test_ce_pages_requires_explicit_project_and_known_action():
    from uuid import uuid4

    from plane_toolchain.ce_tools import page_request

    project, page = str(uuid4()), str(uuid4())
    assert page_request("list", project, "") == f"{project}/ce-pages"
    assert page_request("retrieve", project, page) == f"{project}/ce-pages/{page}"
    with pytest.raises(ValueError):
        page_request("delete", project, page)
    with pytest.raises(ValueError):
        page_request("list", "", "")


def test_ce_page_update_targets_canonical_page_path_with_precondition_body():
    from uuid import uuid4

    from plane_toolchain.ce_tools import page_request, page_update_body

    u = uuid4()
    assert page_request("update", str(u).upper(), "{" + str(u) + "}") == f"{u}/ce-pages/{u}"
    with pytest.raises(ValueError):
        page_request("update", str(u), "")
    assert page_update_body("<p>x</p>", "2026-11-13T01:02:03Z", True) == {
        "description_html": "<p>x</p>",
        "expected_updated_at": "2026-11-13T01:02:03Z",
        "force": True,
    }
    with pytest.raises(ValueError, match="expected_updated_at"):
        page_update_body("<p>x</p>", "", True)


def test_ce_page_description_explains_update_precondition_and_force():
    from plane_toolchain.ce_tools import CE_PAGE_DESCRIPTION

    text = CE_PAGE_DESCRIPTION.lower()
    assert "update" in text
    assert "expected_updated_at" in text
    assert "force" in text
    assert "disconnect" in text
    assert "unsupported" not in text


def test_ids_are_canonicalised_for_paths():
    from uuid import uuid4

    from plane_toolchain.ce_tools import canonical_uuid, page_request

    u = uuid4()
    for raw in (str(u).upper(), "{" + str(u) + "}", "urn:uuid:" + str(u), u.hex):
        assert canonical_uuid(raw) == str(u)
        assert page_request("retrieve", raw, raw) == f"{u}/ce-pages/{u}"
    with pytest.raises(ValueError):
        canonical_uuid("not-a-uuid")


def test_private_sdk_call_guard_fails_clearly_when_method_missing():
    from plane_toolchain.ce_tools import sdk_private

    class Resource:
        def _get(self, path):
            return path

    assert sdk_private(Resource(), "_get")("x") == "x"
    with pytest.raises(RuntimeError, match="_post"):
        sdk_private(Resource(), "_post")


def test_bind_host_default_env_and_validation():
    from plane_toolchain.ce_tools import bind_host

    assert bind_host({}) == "0.0.0.0"  # noqa: S104 - documented default
    assert bind_host({"FASTMCP_HOST": "127.0.0.1"}) == "127.0.0.1"
    assert bind_host({"FASTMCP_HOST": "mcp-host.local"}) == "mcp-host.local"
    for empty in ("", "  "):
        assert bind_host({"FASTMCP_HOST": empty}) == "0.0.0.0"  # noqa: S104 - documented default
    for bad in ("http://x", "a b", "x/y"):
        with pytest.raises(ValueError):
            bind_host({"FASTMCP_HOST": bad})


def test_canonical_uuid_rejects_non_string_with_value_error():
    from plane_toolchain.ce_tools import canonical_uuid

    for bad in (None, 5, b"x", []):
        with pytest.raises(ValueError):
            canonical_uuid(bad)


def _confirm(result, pages, sleeps):
    import asyncio

    from plane_toolchain.ce_tools import confirm_late_store

    async def retrieve():
        return pages.pop(0)

    async def sleep(seconds):
        sleeps.append(seconds)

    return asyncio.run(confirm_late_store(result, retrieve, sleep))


def _result(html="<p>new</p>"):
    import hashlib

    return {
        "id": "page-1",
        "late_store_check": {
            "seconds": 12,
            "html_sha256": hashlib.sha256(html.encode()).hexdigest(),
        },
    }


def test_late_store_unchanged_after_waiting_the_hinted_seconds():
    sleeps = []
    out = _confirm(_result(), [{"description_html": "<p>new</p>"}], sleeps)
    assert sleeps == [12]
    assert out["late_store"] == "unchanged"
    assert out["id"] == "page-1"


def test_late_store_overwrite_fails_naming_only_the_page_id():
    sleeps = []
    with pytest.raises(RuntimeError) as caught:
        _confirm(_result(), [{"description_html": "<p>live</p>"}], sleeps)
    assert sleeps == [12]
    assert "page-1" in str(caught.value)
    assert "live" not in str(caught.value)


def test_late_store_hint_missing_from_older_backend_passes_through():
    sleeps = []
    assert _confirm({"id": "p"}, [], sleeps) == {"id": "p"}
    assert sleeps == []


def test_late_store_wait_is_capped_and_invalid_hint_rejected():
    sleeps = []
    result = _result()
    result["late_store_check"]["seconds"] = 3600
    _confirm(result, [{"description_html": "<p>new</p>"}], sleeps)
    assert sleeps == [60]
    result["late_store_check"]["seconds"] = "12"
    with pytest.raises(ValueError):
        _confirm(result, [], [])


class FakeIntake:
    def __init__(self, status=-2, apply=True):
        self.calls, self.status, self.apply = [], status, apply

    def _get(self, path):
        self.calls.append(("GET", path))
        return {"status": self.status, "issue": "x"}

    def _patch(self, path, body):
        self.calls.append(("PATCH", path, body))
        if self.apply:
            self.status = body["status"]


def test_intake_decide_body_validates_status_and_companions():
    from uuid import uuid4

    from plane_toolchain.ce_tools import intake_decide_body

    dup = uuid4()
    assert intake_decide_body(1, "", "") == {"status": 1}
    assert intake_decide_body(-1, "", "") == {"status": -1}
    assert intake_decide_body(0, "2026-12-01T00:00:00Z", "") == {
        "status": 0,
        "snoozed_till": "2026-12-01T00:00:00Z",
    }
    assert intake_decide_body(2, "", str(dup).upper()) == {"status": 2, "duplicate_to": str(dup)}
    for args in (
        (3, "", ""),
        (True, "", ""),
        (0, "", ""),
        (2, "", ""),
        (1, "2026-12-01", ""),
        (1, "", str(dup)),
        (2, "", "nope"),
    ):
        with pytest.raises(ValueError):
            intake_decide_body(*args)


def test_intake_decide_patches_detail_path_without_status_suffix_and_rereads():
    from uuid import uuid4

    from plane_toolchain.ce_tools import decide_intake

    project, item = uuid4(), uuid4()
    fake = FakeIntake()
    record = decide_intake(fake, "ws", str(project).upper(), str(item), 1, "", "")
    path = f"ws/projects/{project}/intake-issues/{item}"
    assert fake.calls == [("GET", path), ("PATCH", path, {"status": 1}), ("GET", path)]
    assert not path.endswith("/status")
    assert record["status"] == 1


def test_intake_decide_fails_when_backend_ignores_the_status():
    from uuid import uuid4

    from plane_toolchain.ce_tools import decide_intake

    with pytest.raises(RuntimeError, match="not changed"):
        decide_intake(FakeIntake(apply=False), "ws", str(uuid4()), str(uuid4()), -1, "", "")


def test_intake_decide_invalid_input_makes_no_request():
    from plane_toolchain.ce_tools import decide_intake

    fake = FakeIntake()
    with pytest.raises(ValueError):
        decide_intake(fake, "ws", "bad", "bad", 1, "", "")
    with pytest.raises(ValueError):
        decide_intake(fake, "ws", "bad", "bad", 0, "", "")
    assert fake.calls == []
