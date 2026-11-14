"""Core rules for GitHub delivery evidence in Plane.

Pure functions plus a Plane MCP publisher. Nothing here reads GitHub or the
environment: repository specifics arrive as parameters and the caller supplies
the MCP ``call``. Evidence is comments only; task state is never changed.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ReferenceRules:
    field: str = "Plane-Tasks"
    pattern: str = r"[A-Z][A-Z0-9]*-\d+[A-Z]?"
    minimum: int = 1
    maximum: int = 3


def visible_text(body: str) -> str:
    """Exclude comments and fenced examples, including blocks open through EOF."""
    visible = []
    fence = None
    comment = False
    for original in body.splitlines():
        if fence:
            if re.fullmatch(
                r" {0,3}" + re.escape(fence[0]) + "{" + str(fence[1]) + r",}[ \t]*", original
            ):
                fence = None
            continue
        line = original
        parts = []
        while line:
            if comment:
                end = line.find("-->")
                if end < 0:
                    line = ""
                    break
                line = line[end + 3 :]
                comment = False
            else:
                start = line.find("<!--")
                if start < 0:
                    parts.append(line)
                    break
                parts.append(line[:start] + " ")
                line = line[start + 4 :]
                comment = True
        text = "".join(parts)
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})", text)
        if opening:
            fence = (opening[1][0], len(opening[1]))
        else:
            visible.append(text)
    return "\n".join(visible)


def field_lines(body: str, name: str) -> list[str]:
    """Values of every visible standalone ``name:`` line."""
    pattern = r"^" + re.escape(name) + r":[ \t]*([^\n\r]*)\r?$"
    return re.findall(pattern, visible_text(body), re.M)


def references(body: str, rules: ReferenceRules) -> list[str]:
    """Read one visible standalone field; prose and examples grant no authority."""
    lines = field_lines(body, rules.field)
    if not lines:
        return []
    if len(lines) != 1:
        raise ValueError(f"Use one {rules.field} field")
    values = [value.strip() for value in lines[0].split(",")]
    if not rules.minimum <= len(values) <= rules.maximum or any(
        not re.fullmatch(rules.pattern, value) for value in values
    ):
        raise ValueError(f"{rules.field} requires one to three stable task IDs")
    if len(set(values)) != len(values):
        raise ValueError("Duplicate Plane task reference")
    return values
