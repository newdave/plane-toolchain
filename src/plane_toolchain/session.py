"""Report only the checkout's non-secret Plane switch, without evaluating shell text."""

import json
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Non-secret connection values only. PLANE_API_KEY is never read from checkout files.
CONNECTION = ("PLANE_MCP_URL", "PLANE_WORKSPACE", "PLANE_PROJECT")


def literal(root: Path, names: tuple[str, ...]) -> dict[str, str]:
    """Read KEY=value lines from checkout config files; later files win."""
    values = {}
    local = root / "config/local.env"
    legacy = root / ".deploy/env"
    pattern = re.compile(r"\s*(?:export\s+)?(" + "|".join(names) + r")=(.*?)\s*")
    for path in (root / "config/defaults.env", root / ".env", local if local.exists() else legacy):
        if path.is_file():
            for line in path.read_text().splitlines():
                match = pattern.fullmatch(line)
                if match:
                    values[match[1]] = match[2].split(" #", 1)[0].strip().strip("\"'")
    return values


def connection(root: Path, env: Mapping[str, str]) -> dict[str, str]:
    """Return file-provided connection values that the environment does not override."""
    return {k: v for k, v in literal(root, CONNECTION).items() if k not in env and v}


def switch(root: Path, env: Mapping[str, str]) -> str:
    value = literal(root, ("PLANE_ENABLED",)).get("PLANE_ENABLED", "false")
    if "PLANE_ENABLED" in env:
        value = env["PLANE_ENABLED"]
    return {"true": "enabled", "false": "disabled"}.get(value, "invalid")


def status(root: Path, env: Mapping[str, str]) -> str:
    return f"Plane: {switch(root, env)} (checkout switch; live connection not checked)."


def main() -> None:
    if sys.argv[1:] == ["status"]:
        print(switch(ROOT, os.environ))
        return
    if sys.argv[1:] == ["connection"]:
        # NUL-separated so the bridge can export values without evaluating them.
        for name, value in connection(ROOT, os.environ).items():
            sys.stdout.write(f"{name}={value}\0")
        return
    message = status(ROOT, os.environ)
    print(
        json.dumps(
            {
                "systemMessage": message,
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": message,
                },
            }
        )
    )


if __name__ == "__main__":
    main()
