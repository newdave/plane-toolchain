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


def test_invalid_relation_identity_fails_closed():
    with pytest.raises(ValueError):
        relation_result(SimpleNamespace(model_dump=lambda: {"blocked_by": [{}]}))


def test_ce_pages_refuses_document_edits_and_requires_explicit_project():
    from uuid import uuid4

    from plane_toolchain.ce_tools import page_request

    project, page = str(uuid4()), str(uuid4())
    assert page_request("list", project, "") == f"{project}/ce-pages"
    assert page_request("retrieve", project, page) == f"{project}/ce-pages/{page}"
    with pytest.raises(ValueError):
        page_request("update", project, page)
    with pytest.raises(ValueError):
        page_request("list", "", "")


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
