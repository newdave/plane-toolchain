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
