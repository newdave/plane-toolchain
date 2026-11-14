"""Core rules for GitHub delivery evidence in Plane.

Pure functions plus a Plane MCP publisher. Nothing here reads GitHub or the
environment: repository specifics arrive as parameters and the caller supplies
the MCP ``call``. Evidence is comments only; task state is never changed.
"""

import hashlib
import html
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import PurePosixPath

from plane_toolchain.connection import paginated


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


def description(pr: dict) -> str:
    """Status text binding the gate to this exact head and body.

    The literal prefix stays ``Plane-Tasks`` whatever the field name is.
    """
    digest = hashlib.sha256((pr["head"]["sha"] + "\n" + (pr.get("body") or "")).encode())
    return "Plane-Tasks sha256:" + digest.hexdigest()


def gate_fields_changed(
    before: str, after: str, refs: ReferenceRules, maintenance: MaintenanceRules
) -> bool:
    """Whether an edit touched a visible gate field (references or maintenance marker)."""

    def fields(body: str) -> tuple[list[str], list[str]]:
        body = body or ""
        return (
            field_lines(body, refs.field),
            field_lines(body, maintenance.field) if maintenance.field else [],
        )

    return fields(before) != fields(after)


def optional_references(
    pr: dict,
    files: list[str],
    repo: str,
    refs: ReferenceRules,
    maintenance: MaintenanceRules,
) -> list[str]:
    """Evidence only: a PR with no reference field has nothing to record, so skip it.

    A malformed field or a failed exemption still raises.
    """
    body = pr.get("body") or ""
    if not references(body, refs) and not needs_files(body, maintenance):
        return []
    return required_references(pr, files, repo, refs, maintenance)


STATUS_CONTEXT = "Plane task references"
PR_ACTIONS = frozenset({"opened", "edited", "synchronize", "reopened", "closed"})


@dataclass(frozen=True)
class Update:
    """One PR and the evidence comment to record for it."""

    pr: dict
    key: str
    content: str


def _paragraph(text: str, url: str, label: str) -> str:
    return f'<p>{html.escape(text)} <a href="{html.escape(url, quote=True)}">{label}</a></p>'


def pr_update(event: dict, pr: dict, repo: str) -> Update | None:
    """Evidence for a pull_request_target event; the PR itself comes from the API."""
    if pr["base"]["repo"]["full_name"] != repo:
        raise ValueError("Unexpected PR base repository")
    action = event["action"]
    if action not in PR_ACTIONS:
        return None
    number = int(event["number"])
    outcome = "merged" if action == "closed" and pr["merged"] else action
    version = str(event["pull_request"]["updated_at"])
    text = f"PR #{number}: {outcome}. Head: {pr['head']['sha']}."
    return Update(
        pr,
        f"{repo}:pr:{number}:{outcome}:{version}",
        _paragraph(text, pr["html_url"], "Pull request"),
    )


def matching_prs(candidates: list[dict], repo: str, sha: str) -> list[dict]:
    """Keep PRs of this repository whose head or merge commit is the run's commit."""
    return [
        pr
        for pr in candidates
        if pr["base"]["repo"]["full_name"] == repo
        and sha in {pr["head"]["sha"], pr.get("merge_commit_sha")}
    ]


def run_updates(
    prs: list[dict],
    repo: str,
    sha: str,
    *,
    label: str,
    conclusion: str,
    run_id: int,
    attempt: int,
    run_url: str,
) -> list[Update]:
    """Evidence for a finished run; a rerun attempt gets its own key."""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Invalid workflow SHA")
    text = (
        f"{label}: {conclusion}. Revision: {sha}. "
        "This records checks, not deployed health or task acceptance."
    )
    content = _paragraph(text, run_url, "Workflow run")
    if any(pr["base"]["repo"]["full_name"] != repo for pr in prs):
        raise ValueError("Unexpected PR base repository")
    return [Update(pr, f"{repo}:run:{run_id}:{attempt}:pr:{pr['number']}", content) for pr in prs]


def aggregate(results: Mapping[str, str]) -> str:
    """Single conclusion from job results; skipped is neutral."""
    values = set(results.values())
    for value in sorted(values - {"success", "skipped", "cancelled", "failure"}):
        raise ValueError("Unknown job result: " + value)
    if "failure" in values:
        return "failure"
    if "cancelled" in values:
        return "cancelled"
    return "success"


def directory_entries(rows: list[dict], source: str, pattern: str) -> dict[str, dict]:
    """Index unique external task identities of one source.

    The caller lists every row: the server's external_id filter is not trusted.
    """
    result = {}
    for item in rows:
        if item.get("external_source") != source:
            continue
        identity = item.get("external_id", "")
        if not re.fullmatch(pattern, identity):
            raise ValueError("Invalid task identity")
        if identity in result:
            raise ValueError("Duplicate task identity: " + identity)
        result[identity] = item
    return result


def resolved_updates(
    selected: list[tuple[Update, list[str]]], tasks: dict[str, dict]
) -> list[tuple[str, str, str]]:
    """Resolve the entire event before publishing any evidence."""
    updates = []
    for update, identities in selected:
        for identity in identities:
            if identity not in tasks:
                raise ValueError("Referenced Plane task missing: " + identity)
            updates.append((tasks[identity]["id"], update.key, update.content))
    return updates


async def publish(
    call, project: str, item: str, event_key: str, content: str, *, source: str
) -> bool:
    """Read before create and verify after create, including retries after uncertain writes.

    Returns True when a comment was created, False when it was already present.
    """
    arguments = {"project_id": project, "workitem_id": item}
    # Plane enforces external comment identities across tasks. Keep the legacy
    # key matching so replaying an event never duplicates saved evidence.
    identity = f"{event_key}:task:{item}"
    expected = dict(external_source=source, external_id=identity, comment_html=content)
    rows = await paginated(call, "workitem_comment", dict(arguments, action="list"))
    matches = [
        row
        for row in rows
        if row.get("external_source") == source and row.get("external_id") in {event_key, identity}
    ]
    if len(matches) > 1:
        raise ValueError("Duplicate Plane evidence identity")
    if matches:
        if matches[0].get("comment_html") != content:
            raise ValueError("Existing evidence content differs; inspect before retry")
        return False
    await call("workitem_comment", dict(arguments, action="create", **expected))
    rows = await paginated(call, "workitem_comment", dict(arguments, action="list"))
    matches = [row for row in rows if all(row.get(key) == value for key, value in expected.items())]
    if len(matches) != 1:
        raise ValueError("Plane evidence write not verified")
    return True
