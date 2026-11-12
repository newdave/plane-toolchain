"""commit-msg hook: reject AI attribution trailers."""

import re
import sys

TOOLS = r"(?:claude|codex|chatgpt|openai|anthropic|copilot|cursor|gemini|aider|devin)"
# Optional suffix words (product/model names) after the tool name, e.g. "Claude Opus 4".
SUFFIX = r"(?:\s+(?:code|cli|agent|ai|bot|opus|sonnet|haiku|pro|flash|ultra|[\d.]+|\([^)]*\)))*"
AI_NAME = re.compile(rf"^(?:github\s+)?{TOOLS}{SUFFIX}$", re.I)
AI_EMAIL = re.compile(
    r"@(?:[\w-]+\.)*(?:anthropic\.com|openai\.com|cursor\.com|cursoragent\.com)$"
    r"|^(?:\d+\+)?(?:copilot|cursor|claude|devin-ai-integration)[\w\[\]-]*@users\.noreply\.github\.com$",
    re.I,
)
TRAILER = re.compile(r"^\s*co-authored-by:\s*(?P<name>.*?)\s*(?:<(?P<email>[^>]*)>)?\s*$", re.I)
GENERATED = re.compile(rf"^[^\w]*generated\s+(?:with|by)\s+[^\w]*(?:github\s+)?{TOOLS}\b", re.I)


def is_ai_trailer(line):
    m = TRAILER.match(line)
    if not m:
        return False
    return bool(AI_NAME.match(m["name"]) or AI_EMAIL.search(m["email"] or ""))


def violations(message):
    lines = [ln for ln in message.splitlines() if not ln.startswith("#")]
    return [ln for ln in lines if is_ai_trailer(ln) or GENERATED.match(ln)]


def main(argv):
    with open(argv[0], encoding="utf-8") as f:
        found = violations(f.read())
    for line in found:
        print(f"AI attribution not allowed: {line.strip()}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
