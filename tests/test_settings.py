"""Configuration fails closed without disclosing authentication values."""

from dataclasses import replace

import pytest

from plane_toolchain.settings import Settings

ENV = {
    "PLANE_ENABLED": "true",
    "PLANE_MCP_URL": "https://mcp.example.org/http/api-key/mcp",
    "PLANE_WORKSPACE": "demo",
    "PLANE_PROJECT": "00000000-0000-4000-8000-000000000001",
    "PLANE_API_KEY": "fixture-only",
}


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.example.org",
        "https://user:pass@example.org",
        "https://example.org/?key=x",
        "https://example.org/#x",
        "https://example.org/a/../b",
        "https://example.org:bad",
        "",
    ],
)
def test_unsafe_urls_rejected(url):
    with pytest.raises(ValueError):
        Settings.from_env(dict(ENV, PLANE_MCP_URL=url))


def test_disabled_and_invalid_project_rejected_and_key_not_in_repr():
    assert "fixture-only" not in repr(Settings.from_env(ENV))
    assert "other-secret" not in repr(replace(Settings.from_env(ENV), key="other-secret"))
    for key, value in [
        ("PLANE_ENABLED", "false"),
        ("PLANE_PROJECT", "not-uuid"),
        ("PLANE_API_KEY", "bad\nkey"),
        ("PLANE_WORKSPACE", "a/b"),
    ]:
        with pytest.raises(ValueError):
            Settings.from_env(dict(ENV, **{key: value}))
