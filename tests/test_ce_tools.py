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
