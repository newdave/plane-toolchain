"""GitHub evidence core: reference parsing, gate rules, events and Plane publishing."""

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


def test_description_matches_terrain_link():
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
