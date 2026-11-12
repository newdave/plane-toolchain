"""Report only the checkout's non-secret Plane switch, without evaluating shell text."""

import json
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def switch(root: Path, env: Mapping[str, str]) -> str:
    value = "false"
    local = root / "config/local.env"
    legacy = root / ".deploy/env"
    for path in (root / "config/defaults.env", root / ".env", local if local.exists() else legacy):
        if path.is_file():
            for line in path.read_text().splitlines():
                match = re.fullmatch(r"\s*(?:export\s+)?PLANE_ENABLED=(.*?)\s*", line)
                if match:
                    value = match[1].split(" #", 1)[0].strip().strip("\"'")
    if "PLANE_ENABLED" in env:
        value = env["PLANE_ENABLED"]
    return {"true": "enabled", "false": "disabled"}.get(value, "invalid")


def status(root: Path, env: Mapping[str, str]) -> str:
    return f"Plane: {switch(root, env)} (checkout switch; live connection not checked)."


def main() -> None:
    if sys.argv[1:] == ["status"]:
        print(switch(ROOT, os.environ))
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
