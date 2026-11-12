"""Pages creation must validate every document format before storing anything."""

import pytest
from plane_toolchain.pages_backend import creation_payload


def test_creation_sanitises_html_then_converts_consistent_document_formats():
    calls = []
    result = creation_payload(
        {"name": "Planning", "description_html": "<p>Safe</p>", "access": 0},
        lambda value: value.replace("Safe", "Clean"),
        lambda value: (
            calls.append(value)
            or {"description_json": {"type": "doc"}, "description_binary": "AQ=="}
        ),
    )
    assert calls == ["<p>Clean</p>"]
    assert result["description_html"] == calls[0]
    assert result["description_binary"] == "AQ=="
    assert result["description_json"] == {"type": "doc"}


@pytest.mark.parametrize(
    "data",
    [
        {"name": "X", "description_html": "text", "owned_by": "other"},
        {"name": "", "description_html": "text"},
        {"name": "X", "description_html": "text", "access": 2},
    ],
)
def test_creation_rejects_unadvertised_or_invalid_fields_before_conversion(data):
    with pytest.raises(ValueError):
        creation_payload(data, lambda value: value, lambda value: pytest.fail("Must not convert"))


def test_conversion_failure_does_not_produce_a_partial_document():
    with pytest.raises(ValueError):
        creation_payload(
            {"name": "X", "description_html": "text"},
            lambda value: value,
            lambda value: {"description_json": {}},
        )


def test_replacement_preserves_opaque_config_and_does_not_mutate_source():
    from plane_toolchain.pages_deploy import replacement

    original = {
        "Config": {
            "Image": "base",
            "Env": ["OPAQUE=value"],
            "Labels": {"owner": "kept"},
            "Cmd": ["start"],
            "Hostname": "old",
        },
        "HostConfig": {"Binds": ["/data:/data"], "NetworkMode": "shared"},
        "NetworkSettings": {"Networks": {"shared": {"Aliases": ["service"], "IPAMConfig": None}}},
    }
    body = replacement(original, "derived", "http://live:3000", "digest")
    assert body["Env"] == ["OPAQUE=value", "CE_PLANE_LIVE_URL=http://live:3000"]
    assert body["Labels"] == {"owner": "kept", "terrainlink.pages.source": "digest"}
    assert body["HostConfig"] == original["HostConfig"]
    assert body["NetworkingConfig"]["EndpointsConfig"]["shared"]["Aliases"] == ["service"]
    assert original["Config"]["Image"] == "base"
    assert original["Config"]["Labels"] == {"owner": "kept"}


def test_replacement_rejects_auto_removal_before_stopping_original():
    from plane_toolchain.pages_deploy import replacement

    with pytest.raises(ValueError, match="AutoRemove"):
        replacement({"HostConfig": {"AutoRemove": True}}, "derived", "http://live:3000", "digest")
