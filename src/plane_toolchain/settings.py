"""Validate environment-only MCP connection settings without printing secrets."""

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from uuid import UUID


@dataclass(frozen=True)
class Settings:
    url: str
    workspace: str
    project: str
    key: str = field(repr=False)

    @classmethod
    def from_env(cls, env):
        if env.get("PLANE_ENABLED") != "true":
            hint = (
                ".plane/plane.sh enable"
                if env.get("PLANE_CONFIG_ROOT")
                else "mise run plane:enable"
            )
            raise ValueError(f"Plane integration disabled; use {hint}")
        url = env.get("PLANE_MCP_URL", "")
        try:
            parsed = urlsplit(url)
            parsed.port
        except ValueError:
            raise ValueError("Invalid PLANE_MCP_URL") from None
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.scheme not in {"https", "http"}
            or (
                parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            )
            or any(c.isspace() or c == "\\" for c in url)
            or any(c in {".", ".."} for c in parsed.path.split("/"))
        ):
            raise ValueError("PLANE_MCP_URL requires HTTPS without credentials, query or fragment")
        workspace = env.get("PLANE_WORKSPACE", "")
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", workspace):
            raise ValueError("Invalid PLANE_WORKSPACE")
        project = env.get("PLANE_PROJECT", "")
        try:
            UUID(project)
        except ValueError:
            raise ValueError("PLANE_PROJECT must be a UUID") from None
        key = env.get("PLANE_API_KEY", "")
        if not key or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise ValueError("Missing or invalid PLANE_API_KEY")
        return cls(url, workspace, project, key)


if __name__ == "__main__":
    import os
    import sys

    try:
        Settings.from_env(os.environ)
    except ValueError as error:
        sys.exit(str(error))
