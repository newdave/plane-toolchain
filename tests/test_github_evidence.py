"""GitHub evidence core: reference parsing, gate rules, events and Plane publishing."""

import asyncio
import hashlib

import pytest
from plane_toolchain import github_evidence as ge


def test_references_single_visible_field():
    assert ge.references("Plane-Tasks: ABC-1, ABC-2", ge.ReferenceRules()) == ["ABC-1", "ABC-2"]
    assert ge.references("ABC-1 mentioned in prose", ge.ReferenceRules()) == []


@pytest.mark.parametrize(
    "body",
    [
        "Plane-Tasks: ABC-1\nPlane-Tasks: ABC-2",
        "Plane-Tasks: ABC-1, ABC-2, ABC-3, ABC-4",
        "Plane-Tasks: abc-1",
        "Plane-Tasks: ABC-1, ABC-1",
        "Plane-Tasks:",
        "Plane-Tasks: ABC-1,",
    ],
)
def test_references_rejects(body):
    with pytest.raises(ValueError):
        ge.references(body, ge.ReferenceRules())


def test_reference_error_texts_name_the_field():
    with pytest.raises(ValueError, match="Use one Plane-Tasks field"):
        ge.references("Plane-Tasks: ABC-1\nPlane-Tasks: ABC-2", ge.ReferenceRules())
    with pytest.raises(ValueError, match="Duplicate Plane task reference"):
        ge.references("Plane-Tasks: ABC-1, ABC-1", ge.ReferenceRules())
    with pytest.raises(ValueError, match="Tasks requires one to three stable task IDs"):
        ge.references("Tasks: bad", ge.ReferenceRules(field="Tasks"))


@pytest.mark.parametrize(
    "body",
    [
        "<!-- Plane-Tasks: ABC-1 -->",
        "<!--\nPlane-Tasks: ABC-1\n-->",
        "```\nPlane-Tasks: ABC-1\n```",
        "~~~\nPlane-Tasks: ABC-1\n~~~",
        "```\nPlane-Tasks: ABC-1",
        "~~~\nPlane-Tasks: ABC-1",
        "<!--\nPlane-Tasks: ABC-1",
    ],
)
def test_hidden_and_fenced_fields_ignored(body):
    assert ge.references(body, ge.ReferenceRules()) == []


def test_field_after_closed_fence_is_read():
    body = "```\nPlane-Tasks: ABC-9\n```\nPlane-Tasks: ABC-1\n"
    assert ge.references(body, ge.ReferenceRules()) == ["ABC-1"]


def test_custom_field_and_limits():
    rules = ge.ReferenceRules(field="Tasks", maximum=1)
    assert ge.references("Tasks: ABC-1", rules) == ["ABC-1"]
    with pytest.raises(ValueError):
        ge.references("Tasks: ABC-1, ABC-2", rules)
    assert ge.field_lines("Tasks: ABC-1\r\nOther: x", "Tasks") == ["ABC-1"]


ALLOWED = (
    "package.json",
    "package-lock.json",
    "requirements.txt",
    "requirements-dev.txt",
    "Dockerfile",
    "mise.toml",
    ".pre-commit-config.yaml",
    "docker-compose.yml",
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
    ".github/actions/**/action.yml",
    ".github/actions/**/action.yaml",
)
MAINTENANCE = ge.MaintenanceRules(
    field="Plane-Maintenance",
    author="renovate[bot]",
    title_prefix="chore(deps)",
    branch_prefix="renovate/",
    base="main",
    allowed_files=ALLOWED,
)
REPO = "example/demo"
FILES = ["web/package.json", ".github/workflows/ci.yml", ".github/actions/setup/action.yml"]
EXEMPT = "Plane-Maintenance: dependency-update"


def renovate_pr(**changes):
    pr = {
        "body": EXEMPT,
        "state": "open",
        "draft": False,
        "user": {"login": "renovate[bot]"},
        "title": "chore(deps): bump x",
        "head": {"ref": "renovate/x", "repo": {"full_name": REPO}, "sha": "a" * 40},
        "base": {"ref": "main", "repo": {"full_name": REPO}},
    }
    pr.update(changes)
    return pr


def required(pr, files=FILES, maintenance=MAINTENANCE):
    return ge.required_references(pr, files, REPO, ge.ReferenceRules(), maintenance)


def test_maintenance_off_by_default():
    with pytest.raises(ValueError, match="Missing standalone Plane-Tasks field"):
        required(renovate_pr(), maintenance=ge.MaintenanceRules())
    assert required({"body": "Plane-Tasks: ABC-1"}, maintenance=ge.MaintenanceRules()) == ["ABC-1"]
    assert not ge.needs_files(EXEMPT, ge.MaintenanceRules())
    assert ge.needs_files(EXEMPT, MAINTENANCE)


def test_maintenance_requires_bot_title_branch_base_and_paths():
    assert required(renovate_pr()) == []
    fork = {"ref": "renovate/x", "repo": {"full_name": "someone/demo"}}
    mutations = [
        (dict(user={"login": "someone"}), FILES),
        (dict(title="fix: x"), FILES),
        (dict(head={"ref": "feature/x", "repo": {"full_name": REPO}}), FILES),
        (dict(head=fork), FILES),
        (dict(base={"ref": "dev", "repo": {"full_name": REPO}}), FILES),
        (dict(), ["api/app.py"]),
        (dict(), []),
        (dict(body="Plane-Maintenance: other"), FILES),
        (dict(body=EXEMPT + "\nPlane-Tasks: ABC-1"), FILES),
    ]
    for changes, files in mutations:
        with pytest.raises(ValueError, match="Maintenance exemption requires a bounded trusted"):
            required(renovate_pr(**changes), files)


def test_allowed_path_semantics():
    assert not ge.allowed_path(".github/actions/a/b/action.yml", ["github/actions/**/action.yml"])
    assert ge.allowed_path(".github/actions/a/b/action.yml", [".github/actions/**/action.yml"])
    assert ge.allowed_path(".github/actions/action.yml", [".github/actions/**/action.yml"])
    assert not ge.allowed_path(".github/workflows/sub/x.yml", [".github/workflows/*.yml"])
    assert ge.allowed_path(".github/workflows/x.yml", [".github/workflows/*.yml"])
    assert ge.allowed_path("deep/dir/Dockerfile", ["Dockerfile"])
    assert not ge.allowed_path("deep/dir/Dockerfile.dev", ["Dockerfile"])
    assert not ge.allowed_path("a/b", [])


def test_hidden_maintenance_marker_ignored():
    pr = renovate_pr(body="<!-- " + EXEMPT + " -->")
    with pytest.raises(ValueError, match="Missing standalone Plane-Tasks field"):
        required(pr)


def test_title_prefix_bang():
    assert ge.eligible(renovate_pr(title="chore(deps)!: x"), REPO, MAINTENANCE)
    assert not ge.eligible(renovate_pr(title="chore(deps) x"), REPO, MAINTENANCE)
    assert not ge.eligible(renovate_pr(title="chore(depsX): x"), REPO, MAINTENANCE)


def digest(head: str, body: str) -> str:
    return "Plane-Tasks sha256:" + hashlib.sha256((head + "\n" + body).encode()).hexdigest()


def test_description_binds_head_and_body_hash():
    pr = {"head": {"sha": "a" * 40}, "body": "Plane-Tasks: ABC-1"}
    assert ge.description(pr) == digest("a" * 40, "Plane-Tasks: ABC-1")
    assert ge.description(dict(pr, body=None)) == digest("a" * 40, "")
    assert ge.description({"head": {"sha": "a" * 40}}) == digest("a" * 40, "")


def test_gate_fields_changed():
    refs = ge.ReferenceRules()
    off = ge.MaintenanceRules()

    def changed(before, after, maintenance=off):
        return ge.gate_fields_changed(before, after, refs, maintenance)

    assert not changed("Plane-Tasks: ABC-1\nold prose", "Plane-Tasks: ABC-1\nnew prose")
    assert changed("Plane-Tasks: ABC-1, ABC-2", "Plane-Tasks: ABC-2, ABC-1")
    assert changed("Plane-Tasks: ABC-1", "<!-- Plane-Tasks: ABC-1 -->")
    assert changed("Plane-Tasks: ABC-1", "Plane-Tasks: ABC-1\n" + EXEMPT, MAINTENANCE)
    assert not changed("Plane-Tasks: ABC-1", "Plane-Tasks: ABC-1\n" + EXEMPT)
    assert not changed(None, "")
    assert changed(None, "Plane-Tasks: ABC-1")


def test_pr_update_keys_and_html():
    event = {
        "action": "closed",
        "number": 7,
        "pull_request": {"updated_at": "2026-01-01T00:00:00Z"},
    }
    pr = {
        "number": 7,
        "merged": True,
        "head": {"sha": "b" * 40},
        "html_url": "https://github.com/example/demo/pull/7",
        "base": {"repo": {"full_name": REPO}},
    }
    update = ge.pr_update(event, pr, REPO)
    assert update.pr is pr
    assert update.key == "example/demo:pr:7:merged:2026-01-01T00:00:00Z"
    assert update.content == (
        f"<p>PR #7: merged. Head: {'b' * 40}. "
        '<a href="https://github.com/example/demo/pull/7">Pull request</a></p>'
    )
    unmerged = ge.pr_update(dict(event, action="edited"), dict(pr, merged=False), REPO)
    assert unmerged.key == "example/demo:pr:7:edited:2026-01-01T00:00:00Z"
    assert ge.pr_update(dict(event, action="closed"), dict(pr, merged=False), REPO).key.endswith(
        ":closed:2026-01-01T00:00:00Z"
    )
    assert ge.pr_update(dict(event, action="labeled"), pr, REPO) is None
    other = dict(pr, base={"repo": {"full_name": "other/demo"}})
    with pytest.raises(ValueError, match="Unexpected PR base repository"):
        ge.pr_update(event, other, REPO)


def test_matching_prs_head_or_merge_commit():
    sha = "c" * 40

    def candidate(number, head, merge, repo=REPO):
        return {
            "number": number,
            "head": {"sha": head},
            "merge_commit_sha": merge,
            "base": {"repo": {"full_name": repo}},
        }

    candidates = [
        candidate(1, sha, "d" * 40),
        candidate(2, "e" * 40, sha),
        candidate(3, sha, sha, repo="other/demo"),
        candidate(4, "e" * 40, None),
    ]
    assert [pr["number"] for pr in ge.matching_prs(candidates, REPO, sha)] == [1, 2]


def test_run_updates_text():
    sha = "c" * 40
    pr = {"number": 7, "head": {"sha": sha}}
    (update,) = ge.run_updates(
        [pr],
        REPO,
        sha,
        label="build",
        conclusion="success",
        run_id=42,
        attempt=2,
        run_url="https://github.com/example/demo/actions/runs/42",
    )
    assert update.pr is pr
    assert update.key == "example/demo:run:42:2:pr:7"
    assert update.content == (
        f"<p>build: success. Revision: {sha}. "
        "This records checks, not deployed health or task acceptance. "
        '<a href="https://github.com/example/demo/actions/runs/42">Workflow run</a></p>'
    )
    with pytest.raises(ValueError, match="Invalid workflow SHA"):
        ge.run_updates(
            [pr],
            REPO,
            "XYZ",
            label="build",
            conclusion="success",
            run_id=42,
            attempt=1,
            run_url="u",
        )


def test_aggregate():
    assert ge.aggregate({"a": "success", "b": "skipped"}) == "success"
    assert ge.aggregate({"a": "success", "b": "skipped", "c": "cancelled"}) == "cancelled"
    assert ge.aggregate({"a": "success", "c": "cancelled", "d": "failure"}) == "failure"
    assert ge.aggregate({}) == "success"


def test_run_evidence_skips_pr_without_reference_field():
    refs = ge.ReferenceRules()
    assert ge.optional_references({"body": "no field here"}, [], REPO, refs, MAINTENANCE) == []
    assert ge.optional_references({"body": None}, [], REPO, refs, MAINTENANCE) == []
    with pytest.raises(ValueError):
        ge.optional_references({"body": "Plane-Tasks: bad"}, [], REPO, refs, MAINTENANCE)
    assert ge.optional_references({"body": "Plane-Tasks: ABC-1"}, [], REPO, refs, MAINTENANCE) == [
        "ABC-1"
    ]
    assert ge.optional_references(renovate_pr(), FILES, REPO, refs, MAINTENANCE) == []


SOURCE = "demo-github"


def update(key="event", content="<p>Evidence</p>"):
    return ge.Update({}, key, content)


def test_all_references_resolve_before_any_evidence_write():
    selected = [(update(), ["ABC-1", "ABC-2"])]
    with pytest.raises(ValueError, match="Referenced Plane task missing: ABC-2"):
        ge.resolved_updates(selected, {"ABC-1": {"id": "first"}})
    assert ge.resolved_updates(selected, {"ABC-1": {"id": "first"}, "ABC-2": {"id": "second"}}) == [
        ("first", "event", "<p>Evidence</p>"),
        ("second", "event", "<p>Evidence</p>"),
    ]


class Comments:
    def __init__(self):
        self.rows = []
        self.creates = 0
        self.uncertain = False

    async def call(self, name, arguments):
        assert name == "workitem_comment"
        assert "state" not in arguments
        if arguments["action"] == "list":
            return {"results": self.rows, "next_page_results": False}
        assert arguments["action"] == "create"
        self.creates += 1
        self.rows.append(dict(arguments, id="comment"))
        if self.uncertain:
            self.uncertain = False
            raise RuntimeError("Connection lost after create")
        return self.rows[-1]


def publish(comments, content="<p>Evidence</p>", key="event", item="task"):
    return asyncio.run(ge.publish(comments.call, "project", item, key, content, source=SOURCE))


def test_duplicate_events_create_one_comment():
    comments = Comments()
    assert publish(comments, key="repo:pr:1:merged:v") is True
    assert publish(comments, key="repo:pr:1:merged:v") is False
    assert comments.creates == 1
    assert comments.rows[0]["external_source"] == SOURCE
    assert comments.rows[0]["external_id"] == "repo:pr:1:merged:v:task:task"


def test_retry_after_uncertain_write_rereads_before_creating():
    comments = Comments()
    comments.uncertain = True
    with pytest.raises(RuntimeError):
        publish(comments)
    assert publish(comments) is False
    assert comments.creates == 1


def test_conflicting_duplicate_event_content_is_reported():
    comments = Comments()
    publish(comments, "<p>One</p>")
    with pytest.raises(ValueError, match="content"):
        publish(comments, "<p>Different</p>")


def test_publish_refuses_differing_content():
    comments = Comments()
    comments.rows.append(
        dict(
            external_source=SOURCE,
            external_id="event:task:task",
            comment_html="<p>Existing</p>",
        )
    )
    with pytest.raises(ValueError, match="Existing evidence content differs; inspect before retry"):
        publish(comments, "<p>Other</p>")
    assert comments.creates == 0


def test_publish_returns_false_when_present():
    comments = Comments()
    comments.rows.append(
        dict(
            external_source=SOURCE,
            external_id="event:task:task",
            comment_html="<p>Evidence</p>",
        )
    )
    assert publish(comments) is False
    assert comments.creates == 0


def test_publish_refuses_duplicates_and_unverified_writes():
    comments = Comments()
    row = dict(external_source=SOURCE, external_id="event:task:task", comment_html="<p>E</p>")
    comments.rows.extend([row, dict(row)])
    with pytest.raises(ValueError, match="Duplicate Plane evidence identity"):
        publish(comments)

    async def forgetful(name, arguments):
        if arguments["action"] == "list":
            return []
        return {}

    with pytest.raises(ValueError, match="Plane evidence write not verified"):
        asyncio.run(ge.publish(forgetful, "project", "task", "event", "<p>E</p>", source=SOURCE))


def test_one_event_can_publish_to_multiple_tasks_with_global_comment_identity_uniqueness():
    rows = {}
    identities = set()

    async def call(name, arguments):
        assert name == "workitem_comment"
        task_rows = rows.setdefault(arguments["workitem_id"], [])
        if arguments["action"] == "list":
            return {"results": task_rows, "next_page_results": False}
        identity = (arguments["external_source"], arguments["external_id"])
        if identity in identities:
            raise ValueError("HTTP 409: comment external identity already exists")
        identities.add(identity)
        task_rows.append(dict(arguments))
        return task_rows[-1]

    async def run():
        for _ in range(2):
            for task in ("first", "second"):
                await ge.publish(call, "project", task, "shared", "<p>E</p>", source=SOURCE)

    asyncio.run(run())
    assert len(identities) == 2
    assert all(len(task_rows) == 1 for task_rows in rows.values())


def test_legacy_event_identity_is_verified_without_recreating_comment():
    comments = Comments()
    comments.rows.append(
        dict(external_source=SOURCE, external_id="event", comment_html="<p>Old</p>")
    )
    assert publish(comments, "<p>Old</p>") is False
    assert comments.creates == 0
    with pytest.raises(ValueError, match="content"):
        publish(comments, "<p>Changed</p>")


def listing(rows, size=100):
    """A fake workitem listing that ignores any external_id filter, in cursor pages."""
    calls = []

    async def call(name, arguments):
        calls.append(arguments)
        assert name == "workitem"
        start = int(arguments["cursor"] or 0)
        page = rows[start : start + size]
        more = start + size < len(rows)
        return {
            "results": page,
            "next_page_results": more,
            "next_cursor": str(start + size) if more else None,
        }

    return call, calls


def test_directory_ignores_server_filter_and_rejects_duplicates():
    rows = [
        {"id": f"id{n}", "external_source": SOURCE, "external_id": f"XYZ-{n}"}
        for n in range(1, 249)
    ]
    rows += [
        {"id": "other", "external_source": "elsewhere", "external_id": "not a task id"},
        {"id": "seven", "external_source": SOURCE, "external_id": "ABC-7"},
    ]
    call, calls = listing(rows)
    found = asyncio.run(ge.paginated(call, "workitem", {"action": "list", "external_id": "ABC-7"}))
    assert len(found) == 250 and len(calls) == 3
    entries = ge.directory_entries(found, SOURCE, ge.ReferenceRules().pattern)
    assert entries["ABC-7"]["id"] == "seven"
    assert "not a task id" not in entries
    with pytest.raises(ValueError, match="Duplicate task identity: ABC-7"):
        ge.directory_entries(
            found + [{"id": "again", "external_source": SOURCE, "external_id": "ABC-7"}],
            SOURCE,
            ge.ReferenceRules().pattern,
        )
    with pytest.raises(ValueError, match="Invalid task identity"):
        ge.directory_entries(
            [{"external_source": SOURCE, "external_id": "bad id"}],
            SOURCE,
            ge.ReferenceRules().pattern,
        )


def test_paginated_rejects_repeated_cursor():
    async def call(name, arguments):
        return {"results": [{}], "next_page_results": True, "next_cursor": "same"}

    with pytest.raises(ValueError, match="Invalid MCP pagination"):
        asyncio.run(ge.paginated(call, "workitem", {}))

    async def missing(name, arguments):
        return {"results": [], "next_page_results": True}

    with pytest.raises(ValueError, match="Invalid MCP pagination"):
        asyncio.run(ge.paginated(missing, "workitem", {}))


def test_paginated_accepts_plain_list_and_caps_pages():
    async def plain(name, arguments):
        return [{"id": 1}]

    assert asyncio.run(ge.paginated(plain, "workitem", {})) == [{"id": 1}]
    counter = iter(range(1000))

    async def endless(name, arguments):
        return {"results": [], "next_page_results": True, "next_cursor": str(next(counter))}

    with pytest.raises(ValueError, match="MCP pagination limit reached"):
        asyncio.run(ge.paginated(endless, "workitem", {}))
