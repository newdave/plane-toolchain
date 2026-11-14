"""GitHub evidence core: reference parsing, gate rules, events and Plane publishing."""

import pytest
from plane_toolchain import github_evidence as ge


def test_references_single_visible_field():
    assert ge.references("Plane-Tasks: ABC-1, ABC-2", ge.ReferenceRules()) == ["ABC-1", "ABC-2"]
    assert ge.references("ABC-1 mentioned in prose", ge.ReferenceRules()) == []


@pytest.mark.parametrize(
    "body",
    [
        "Plane-Tasks: ABC-1\nPlane-Tasks: ABC-2",
        "Plane-Tasks: ABC-1, ABC-2, ABC-3, ABC-4",
        "Plane-Tasks: abc-1",
        "Plane-Tasks: ABC-1, ABC-1",
        "Plane-Tasks:",
        "Plane-Tasks: ABC-1,",
    ],
)
def test_references_rejects(body):
    with pytest.raises(ValueError):
        ge.references(body, ge.ReferenceRules())


def test_reference_error_texts_name_the_field():
    with pytest.raises(ValueError, match="Use one Plane-Tasks field"):
        ge.references("Plane-Tasks: ABC-1\nPlane-Tasks: ABC-2", ge.ReferenceRules())
    with pytest.raises(ValueError, match="Duplicate Plane task reference"):
        ge.references("Plane-Tasks: ABC-1, ABC-1", ge.ReferenceRules())
    with pytest.raises(ValueError, match="Tasks requires one to three stable task IDs"):
        ge.references("Tasks: bad", ge.ReferenceRules(field="Tasks"))


@pytest.mark.parametrize(
    "body",
    [
        "<!-- Plane-Tasks: ABC-1 -->",
        "<!--\nPlane-Tasks: ABC-1\n-->",
        "```\nPlane-Tasks: ABC-1\n```",
        "~~~\nPlane-Tasks: ABC-1\n~~~",
        "```\nPlane-Tasks: ABC-1",
        "~~~\nPlane-Tasks: ABC-1",
        "<!--\nPlane-Tasks: ABC-1",
    ],
)
def test_hidden_and_fenced_fields_ignored(body):
    assert ge.references(body, ge.ReferenceRules()) == []


def test_field_after_closed_fence_is_read():
    body = "```\nPlane-Tasks: ABC-9\n```\nPlane-Tasks: ABC-1\n"
    assert ge.references(body, ge.ReferenceRules()) == ["ABC-1"]


def test_custom_field_and_limits():
    rules = ge.ReferenceRules(field="Tasks", maximum=1)
    assert ge.references("Tasks: ABC-1", rules) == ["ABC-1"]
    with pytest.raises(ValueError):
        ge.references("Tasks: ABC-1, ABC-2", rules)
    assert ge.field_lines("Tasks: ABC-1\r\nOther: x", "Tasks") == ["ABC-1"]
