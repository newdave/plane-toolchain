"""Core rules for GitHub delivery evidence in Plane.

Pure functions plus a Plane MCP publisher. Nothing here reads GitHub or the
environment: repository specifics arrive as parameters and the caller supplies
the MCP ``call``. Evidence is comments only; task state is never changed.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import PurePosixPath


@dataclass(frozen=True)
class ReferenceRules:
    field: str = "Plane-Tasks"
    pattern: str = r"[A-Z][A-Z0-9]*-\d+[A-Z]?"
    minimum: int = 1
    maximum: int = 3


@dataclass(frozen=True)
class MaintenanceRules:
    """Opt-in exemption for bounded dependency PRs; an empty ``field`` turns it off."""

    field: str = ""
    value: str = "dependency-update"
    author: str = ""
    title_prefix: str = ""
    branch_prefix: str = ""
    base: str = ""
    allowed_files: tuple[str, ...] = ()


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


def _segments(pattern: list[str], path: list[str]) -> bool:
    if not pattern:
        return not path
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        return any(_segments(rest, path[skip:]) for skip in range(len(path) + 1))
    return bool(path) and fnmatchcase(path[0], head) and _segments(rest, path[1:])


def allowed_path(path: str, patterns: Sequence[str]) -> bool:
    """A pattern without ``/`` names a basename; otherwise it matches the whole path.

    ``*`` stays inside one segment and ``**/`` spans zero or more directories.
    """
    for pattern in patterns:
        if "/" not in pattern:
            if PurePosixPath(path).name == pattern:
                return True
        elif _segments(pattern.split("/"), path.split("/")):
            return True
    return False


def eligible(pr: dict, repo: str, rules: MaintenanceRules) -> bool:
    """Require bot identity, dependency title, local branch and the configured base."""
    return (
        pr["state"] == "open"
        and not pr["draft"]
        and pr["user"]["login"] == rules.author
        and re.match("^" + re.escape(rules.title_prefix) + r"!?: ", pr["title"]) is not None
        and pr["head"]["ref"].startswith(rules.branch_prefix)
        and (pr["head"]["repo"] or {}).get("full_name") == repo
        and pr["base"]["repo"]["full_name"] == repo
        and pr["base"]["ref"] == rules.base
    )


def needs_files(body: str, rules: MaintenanceRules) -> bool:
    """Only list changed files when the PR visibly asks for the exemption."""
    return bool(rules.field) and bool(field_lines(body, rules.field))


def required_references(
    pr: dict,
    files: list[str],
    repo: str,
    refs: ReferenceRules,
    maintenance: MaintenanceRules,
) -> list[str]:
    """Only explicit, bounded dependency maintenance may omit tasks."""
    body = pr.get("body") or ""
    ids = references(body, refs)
    exemptions = field_lines(body, maintenance.field) if maintenance.field else []
    if exemptions:
        if (
            ids
            or exemptions != [maintenance.value]
            or not files
            or not all(allowed_path(path, maintenance.allowed_files) for path in files)
            or not eligible(dict(pr, state="open", draft=False), repo, maintenance)
        ):
            raise ValueError(
                "Maintenance exemption requires a bounded trusted Renovate dependency PR"
            )
        return []
    if not ids:
        raise ValueError(f"Missing standalone {refs.field} field")
    return ids
