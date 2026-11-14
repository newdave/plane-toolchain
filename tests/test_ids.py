"""ids.py helpers, and the guard that ce_tools.py's standalone copies stay identical."""

import uuid

import pytest

from plane_toolchain import ce_tools, ids

UPPER = "0123ABCD-0123-4567-89AB-0123456789AB"
LOWER = UPPER.lower()


@pytest.mark.parametrize(
    "value",
    [
        UPPER,
        LOWER,
        "{" + LOWER + "}",
        "urn:uuid:" + LOWER,
        LOWER.replace("-", ""),
    ],
)
def test_uuid_forms_canonicalise(value):
    assert ids.is_uuid(value)
    assert ids.canonical(value) == LOWER
    assert ce_tools.canonical_uuid(value) == LOWER


@pytest.mark.parametrize("value", ["", "PTC-12", "not-a-uuid", LOWER[:-1], LOWER + "0"])
def test_non_uuid_refused(value):
    assert not ids.is_uuid(value)
    with pytest.raises(ValueError):
        ids.canonical(value)
    with pytest.raises(ValueError):
        ce_tools.canonical_uuid(value)


@pytest.mark.parametrize("value", [None, 5, uuid.UUID(LOWER)])
def test_ce_tools_refuses_non_strings(value):
    with pytest.raises(ValueError):
        ce_tools.canonical_uuid(value)


@pytest.mark.parametrize("value", ["PTC-12", "a1-9", "Ab2-100"])
def test_identifier_accepts(value):
    assert ids.IDENTIFIER.fullmatch(value)


@pytest.mark.parametrize("value", ["PTC", "12-PTC", "PTC-", "PTC-1x", "PTC-12 ", "-1", LOWER])
def test_identifier_rejects(value):
    assert not ids.IDENTIFIER.fullmatch(value)


def test_relation_types_match_ce_tools():
    assert tuple(ids.RELATION_TYPES) == tuple(ce_tools.DEPENDENCIES)
    assert len(set(ids.RELATION_TYPES)) == 6
