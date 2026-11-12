"""commit-msg hook: reject AI attribution trailers."""

import re
import sys

NAMES = r"(claude|codex|anthropic|openai|chatgpt|copilot|cursor|gemini|aider|devin)"
PATTERNS = (
    re.compile(rf"^\s*co-authored-by:.*\b{NAMES}\b", re.I),
    re.compile(rf"^.*\bgenerated\s+(with|by)\b.*\b{NAMES}\b", re.I),
)


def violations(message):
    lines = [ln for ln in message.splitlines() if not ln.startswith("#")]
    return [ln for ln in lines if any(p.search(ln) for p in PATTERNS)]


def main(argv):
    with open(argv[0], encoding="utf-8") as f:
        found = violations(f.read())
    for line in found:
        print(f"AI attribution not allowed: {line.strip()}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
