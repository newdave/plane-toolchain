"""Entry point for the GitHub PR gate and delivery evidence actions.

Thin layer over ``plane_toolchain.github_evidence``: a GitHub REST client, the
Plane preflight, error classification, annotations and the job summary. Every
failure prints a fixed, secret-free cause; no URL, header or body is echoed.
"""

import asyncio
import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

import httpx2
from plane_toolchain import github_evidence as ge
from plane_toolchain.connection import ToolError, connection


class Failure(Exception):
    """A failure whose message is fixed, secret-free text."""


class GitHub:
    """Minimal GitHub REST reads and the one status write the gate needs."""

    def __init__(
        self,
        token: str,
        repository: str,
        api_url: str = "https://api.github.com",
        client: httpx2.Client | None = None,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Invalid repository")
        self._token = token
        self._repository = repository
        self._api_url = api_url.rstrip("/")
        self._client = client or httpx2.Client(timeout=30)

    def _request(self, operation: str, method: str, path: str, **options):
        headers = {
            "Authorization": "Bearer " + self._token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        url = f"{self._api_url}/repos/{self._repository}{path}"
        try:
            response = self._client.request(method, url, headers=headers, **options)
        except httpx2.HTTPError:
            raise Failure(f"GitHub {operation} failed (network error)") from None
        if not 200 <= response.status_code < 300:
            raise Failure(f"GitHub {operation} failed (HTTP {response.status_code})")
        try:
            return response.json()
        except ValueError:
            raise Failure(f"GitHub {operation} failed (invalid response)") from None

    def pull(self, number: int) -> dict:
        return self._request("pull", "GET", f"/pulls/{int(number)}")

    def pulls_for_commit(self, sha: str) -> list[dict]:
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise Failure("Invalid commit SHA")
        return self._request(
            "commit pulls", "GET", f"/commits/{sha}/pulls", params={"per_page": 100}
        )

    def files(self, number: int) -> list[str]:
        names: list[str] = []
        for page in range(1, 31):
            rows = self._request(
                "files",
                "GET",
                f"/pulls/{int(number)}/files",
                params={"per_page": 100, "page": page},
            )
            names.extend(row["filename"] for row in rows)
            if len(rows) < 100:
                return names
        raise Failure("PR file listing incomplete")

    def post_status(self, sha: str, state: str, description: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise Failure("Invalid PR SHA")
        self._request(
            "status",
            "POST",
            f"/statuses/{sha}",
            json={"state": state, "context": "Plane task references", "description": description},
        )


@dataclass(frozen=True)
class Inputs:
    """Parsed, validated environment contract."""

    key: str = field(repr=False)
    token: str = field(repr=False)
    project: str
    task_source: str
    comment_source: str
    refs: ge.ReferenceRules
    maintenance: ge.MaintenanceRules
    repository: str
    event_name: str
    event_path: str
    ref: str
    sha: str
    api_url: str
    server_url: str
    label: str = ""
    conclusion: str = ""
    run_id: int = 0
    attempt: int = 0


def _integer(env: Mapping[str, str], name: str, default: int) -> int:
    text = env.get(name, "").strip() or str(default)
    try:
        return int(text)
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None


def _conclusion(env: Mapping[str, str]) -> str:
    direct = env.get("PLANE_EVIDENCE_CONCLUSION", "").strip()
    needs = env.get("PLANE_EVIDENCE_NEEDS", "").strip()
    if direct and needs:
        raise ValueError("Set only one of PLANE_EVIDENCE_CONCLUSION and PLANE_EVIDENCE_NEEDS")
    if direct:
        if direct not in {"success", "failure", "cancelled"}:
            raise ValueError("PLANE_EVIDENCE_CONCLUSION must be success, failure or cancelled")
        return direct
    if not needs:
        raise ValueError("Set PLANE_EVIDENCE_CONCLUSION or PLANE_EVIDENCE_NEEDS")
    try:
        data = json.loads(needs)
        results = {name: value["result"] for name, value in data.items()}
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError("PLANE_EVIDENCE_NEEDS must be the JSON of needs") from None
    if not results:
        raise ValueError("PLANE_EVIDENCE_NEEDS is empty")
    return ge.aggregate(results)


def inputs(env: Mapping[str, str], mode: Literal["gate", "evidence"]) -> Inputs:
    """Validate the contract the actions set from their inputs."""
    minimum = _integer(env, "PLANE_REFERENCES_MIN", 1)
    maximum = _integer(env, "PLANE_REFERENCES_MAX", 3)
    if minimum < 1 or maximum < 1 or minimum > maximum:
        raise ValueError("PLANE_REFERENCES_MIN and PLANE_REFERENCES_MAX need 1 <= min <= max")
    pattern = env.get("PLANE_REFERENCES_PATTERN", "") or ge.ReferenceRules.pattern
    try:
        re.compile(pattern)
    except re.error:
        raise ValueError("PLANE_REFERENCES_PATTERN is not a valid regular expression") from None
    sources = {}
    for name in ("PLANE_TASK_SOURCE", "PLANE_COMMENT_SOURCE"):
        sources[name] = env.get(name, "").strip()
        if not sources[name]:
            raise ValueError(f"{name} must not be empty")
    refs = ge.ReferenceRules(
        field=env.get("PLANE_REFERENCES_FIELD", "") or ge.ReferenceRules.field,
        pattern=pattern,
        minimum=minimum,
        maximum=maximum,
    )
    files = tuple(
        line.strip()
        for line in env.get("PLANE_MAINTENANCE_ALLOWED_FILES", "").splitlines()
        if line.strip()
    )
    maintenance = ge.MaintenanceRules(
        field=env.get("PLANE_MAINTENANCE_FIELD", ""),
        value=env.get("PLANE_MAINTENANCE_VALUE", "") or ge.MaintenanceRules.value,
        author=env.get("PLANE_MAINTENANCE_AUTHOR", ""),
        title_prefix=env.get("PLANE_MAINTENANCE_TITLE_PREFIX", ""),
        branch_prefix=env.get("PLANE_MAINTENANCE_BRANCH_PREFIX", ""),
        base=env.get("PLANE_MAINTENANCE_BASE", ""),
        allowed_files=files,
    )
    extra = {}
    if mode == "evidence":
        label = env.get("PLANE_EVIDENCE_LABEL", "").strip()
        if not label:
            raise ValueError("PLANE_EVIDENCE_LABEL must not be empty")
        extra = {
            "label": label,
            "conclusion": _conclusion(env),
            "run_id": _integer(env, "GITHUB_RUN_ID", 0),
            "attempt": _integer(env, "GITHUB_RUN_ATTEMPT", 1),
        }
    return Inputs(
        key=env.get("PLANE_API_KEY", ""),
        token=env.get("GH_TOKEN", ""),
        project=env.get("PLANE_PROJECT", ""),
        task_source=sources["PLANE_TASK_SOURCE"],
        comment_source=sources["PLANE_COMMENT_SOURCE"],
        refs=refs,
        maintenance=maintenance,
        repository=env.get("GITHUB_REPOSITORY", ""),
        event_name=env.get("GITHUB_EVENT_NAME", ""),
        event_path=env.get("GITHUB_EVENT_PATH", ""),
        ref=env.get("GITHUB_REF", ""),
        sha=env.get("GITHUB_SHA", ""),
        api_url=env.get("GITHUB_API_URL", "") or "https://api.github.com",
        server_url=env.get("GITHUB_SERVER_URL", "") or "https://github.com",
        **extra,
    )


def _leaf(error: BaseException) -> BaseException:
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    return error


def _status(error: BaseException) -> int | None:
    """HTTP status carried by an error, from its response or its text."""
    error = _leaf(error)
    code = getattr(getattr(error, "response", None), "status_code", None)
    if isinstance(code, int) and 400 <= code <= 599:
        return code
    found = re.search(r"(?<!\d)([45]\d\d)(?!\d)", str(error))
    return int(found[1]) if found else None


def _retry_after(error: BaseException) -> float | None:
    error = _leaf(error)
    value = getattr(getattr(error, "response", None), "headers", {}).get("Retry-After")
    if value is None:
        found = re.search(r"retry-after\W+(\d+(?:\.\d+)?)", str(error), re.I)
        value = found[1] if found else None
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def retrying(call, *, sleep=asyncio.sleep, attempts: int = 3):
    """Wrap an MCP call: reads (list, retrieve) retry 429 and 5xx; creates run once.

    Waits are 2, 4 and 8 s, or Retry-After when present, capped at 30 s. Failures
    carry the tool and action so ``classify`` can name them.
    """

    async def wrapped(name, arguments):
        action = arguments.get("action", "")
        retries = 0
        while True:
            try:
                return await call(name, arguments)
            except Exception as error:
                try:
                    _leaf(error).plane_operation = f"{name.replace('_', ' ')} {action}".strip()
                except AttributeError:
                    pass
                code = _status(error)
                transient = code == 429 or (code is not None and code >= 500)
                if action not in {"list", "retrieve"} or not transient or retries >= attempts:
                    raise
                wait = _retry_after(error)
                await sleep(min(wait if wait is not None else 2 * 2**retries, 30.0))
                retries += 1

    return wrapped


async def preflight(call, project: str) -> None:
    """Name a 401, 403 or empty project before resolution starts."""

    async def step(what: str, name: str, arguments: dict):
        try:
            return await call(name, arguments)
        except Exception as error:
            code = _status(error)
            raise Failure(
                f"Plane bot cannot {what}" + (f" (HTTP {code})" if code else "")
            ) from None

    await step("read the project", "project", {"action": "retrieve", "project_id": project})
    data = await step(
        "list tasks",
        "workitem",
        {"action": "list", "project_id": project, "per_page": 1},
    )
    rows = data if isinstance(data, list) else (data or {}).get("results", [])
    if not rows:
        raise Failure("Plane bot sees no tasks in the project")
    await step(
        "list comments",
        "workitem_comment",
        {"action": "list", "project_id": project, "workitem_id": rows[0]["id"], "per_page": 1},
    )


def classify(error: BaseException) -> str:
    """Fixed, secret-free cause text; never a URL, header or response body."""
    error = _leaf(error)
    if type(error) in {Failure, ValueError, ToolError}:
        return str(error)
    code = _status(error)
    operation = getattr(error, "plane_operation", None)
    if code and operation:
        return f"Plane {operation} failed (HTTP {code})"
    return "Unexpected " + type(error).__name__


GATE_TITLE = "Plane task references"
EVIDENCE_TITLE = "Plane evidence"


def annotate(
    level: Literal["notice", "warning", "error"],
    message: str,
    out=None,
    *,
    title: str = EVIDENCE_TITLE,
) -> None:
    """Write a workflow command; the message is escaped so it cannot inject another."""
    text = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::{level} title={title}::{text}", file=out or sys.stdout, flush=True)


def summarise(line: str, env: Mapping[str, str]) -> None:
    """Append one line to the job summary when the runner provides the file."""
    path = env.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as summary:
            summary.write(line.replace("\r", " ").replace("\n", " ") + "\n")


def _event(config: Inputs) -> dict:
    with open(config.event_path, encoding="utf-8") as handle:
        return json.load(handle)


async def _resolve(config: Inputs, call, selected: list[tuple[ge.Update, list[str]]]):
    """Preflight, then resolve every reference before anything is written."""
    await preflight(call, config.project)
    rows = await ge.paginated(call, "workitem", {"action": "list", "project_id": config.project})
    tasks = ge.directory_entries(rows, config.task_source, config.refs.pattern)
    return ge.resolved_updates(selected, tasks)


async def _publish(config: Inputs, call, resolved) -> int:
    created = 0
    for item, key, content in resolved:
        created += await ge.publish(
            call, config.project, item, key, content, source=config.comment_source
        )
    return created


async def run_gate(
    config: Inputs, github: GitHub, connect, env: Mapping[str, str] | None = None
) -> int:
    """The pull_request_target gate; it fails closed on any error."""
    env = os.environ if env is None else env
    pr = None
    try:
        if config.event_name != "pull_request_target":
            raise Failure("Plane gate runs only on pull_request_target")
        event = _event(config)
        action = event["action"]
        if action not in ge.PR_ACTIONS:
            annotate(
                "notice", f"Plane gate skipped: action {action} is not gated", title=GATE_TITLE
            )
            return 0
        pr = github.pull(int(event["number"]))
        before = ((event.get("changes") or {}).get("body") or {}).get("from")
        if action == "edited" and before is None:
            annotate("notice", "Plane gate skipped: the edit left the body alone", title=GATE_TITLE)
            return 0
        sha = pr["head"]["sha"]
        text = ge.description(pr)
        github.post_status(sha, "pending", text)
        update = ge.pr_update(event, pr, config.repository)
        body = pr.get("body") or ""
        files = github.files(pr["number"]) if ge.needs_files(body, config.maintenance) else []
        ids = ge.required_references(pr, files, config.repository, config.refs, config.maintenance)
        if ids:
            if not config.key:
                raise Failure("PLANE_API_KEY unavailable")
            async with connect() as raw:
                call = retrying(raw)
                resolved = await _resolve(config, call, [(update, ids)])
                # An edit that kept the gate fields already has its comment.
                unchanged = action == "edited" and not ge.gate_fields_changed(
                    before, body, config.refs, config.maintenance
                )
                if not unchanged:
                    await _publish(config, call, resolved)
        github.post_status(sha, "success", text)
        summarise(f"Plane task references: passed for PR #{pr['number']}", env)
        return 0
    except Exception as error:
        message = classify(error)
        if pr is not None:
            try:
                github.post_status(pr["head"]["sha"], "failure", ge.description(pr))
            except Exception as status_error:
                message += "; " + classify(status_error)
        annotate("error", message, title=GATE_TITLE)
        summarise(f"Plane task references: failed. {message}", env)
        return 1


async def run_evidence(
    config: Inputs, github: GitHub, connect, env: Mapping[str, str] | None = None
) -> int:
    """Record one run's checks on the Plane tasks its PRs name; never the gate."""
    env = os.environ if env is None else env
    if not config.key:
        annotate("notice", "Plane evidence skipped: no PLANE_API_KEY")
        return 0
    try:
        name = config.event_name
        if name == "pull_request":
            head = _event(config)["pull_request"]["head"]
            if (head.get("repo") or {}).get("full_name") != config.repository:
                raise Failure("Plane evidence refuses pull requests from forks")
            sha = head["sha"]
        elif name == "push":
            default = _event(config)["repository"]["default_branch"]
            if config.ref != f"refs/heads/{default}":
                raise Failure("Plane evidence on push runs only for the default branch")
            sha = config.sha
        elif name == "pull_request_target":
            raise Failure("Plane evidence refuses pull_request_target")
        else:
            raise Failure("Plane evidence supports only pull_request and push events")
        prs = ge.matching_prs(github.pulls_for_commit(sha), config.repository, sha)
        wanted = []
        ids_by_number = {}
        for pr in prs:
            body = pr.get("body") or ""
            files = github.files(pr["number"]) if ge.needs_files(body, config.maintenance) else []
            ids = ge.optional_references(
                pr, files, config.repository, config.refs, config.maintenance
            )
            if ids:
                wanted.append(pr)
                ids_by_number[pr["number"]] = ids
            else:
                annotate("notice", f"Plane evidence skipped PR #{pr['number']}: no task reference")
        if not wanted:
            annotate("notice", "Plane evidence skipped: no pull request names a Plane task")
            return 0
        updates = ge.run_updates(
            wanted,
            config.repository,
            sha,
            label=config.label,
            conclusion=config.conclusion,
            run_id=config.run_id,
            attempt=config.attempt,
            run_url=f"{config.server_url}/{config.repository}/actions/runs/{config.run_id}",
        )
        selected = [(update, ids_by_number[update.pr["number"]]) for update in updates]
        async with connect() as raw:
            call = retrying(raw)
            resolved = await _resolve(config, call, selected)
            created = await _publish(config, call, resolved)
        line = f"{config.label}: recorded {created} of {len(resolved)} Plane comments"
        annotate("notice", line)
        summarise("Plane evidence: " + line, env)
        return 0
    except Exception as error:
        message = classify(error)
        annotate("warning", message)
        summarise(f"Plane evidence: not recorded. {message}", env)
        return 1


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or argv[0] not in {"gate", "evidence"}:
        print("usage: github_evidence_cli (gate|evidence)", file=sys.stderr)
        return 2
    mode = argv[0]
    try:
        config = inputs(os.environ, mode)
        github = GitHub(config.token, config.repository, config.api_url)
    except ValueError as error:
        message = classify(error)
        if mode == "gate":
            annotate("error", message, title=GATE_TITLE)
        else:
            annotate("warning", message)
        summarise(f"Plane {mode}: not run. {message}", os.environ)
        return 1
    run = run_gate if mode == "gate" else run_evidence
    return asyncio.run(run(config, github, lambda: connection(os.environ)))


if __name__ == "__main__":
    sys.exit(main())
