"""PreToolUse hook: point code searches at the graphify graph when one exists."""

import json
import re
import sys
from pathlib import Path

SEARCH = re.compile(r"(?:^|[\s|;&(])(?:grep|rg|find|fd|ack|ag)(?:\s|$)")
HINT = (
    "graphify: knowledge graph at graphify-out/. For focused questions, run "
    '`uv run --group graph graphify query "<question>"` (scoped subgraph) before grepping '
    "raw files. Read graphify-out/GRAPH_REPORT.md only for broad architecture context."
)


def wants_hint(event):
    tool = event.get("tool_name", "")
    if tool in ("Grep", "Glob"):
        return True
    return tool == "Bash" and bool(SEARCH.search(event.get("tool_input", {}).get("command", "")))


def main(stdin=sys.stdin, root=Path.cwd()):
    try:
        event = json.load(stdin)
    except ValueError:
        return 0
    if (
        isinstance(event, dict)
        and wants_hint(event)
        and (root / "graphify-out/graph.json").is_file()
    ):
        out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": HINT}}
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
