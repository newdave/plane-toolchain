import tomllib
from pathlib import Path

from plane_toolchain import workspace_rename

ROOT = Path(__file__).resolve().parent.parent
RUNBOOK = (ROOT / "docs" / "workspace-rename.md").read_text()
REQUIRED = (
    "PLANE_RENAME_FROM",
    "PLANE_RENAME_TO",
    "PLANE_MCP_SSH",
    "PLANE_API_CONTAINER",
    "PLANE_LIVE_URL",
)
OPTIONAL = (
    "PLANE_RENAME_RECEIPT_DIR",
    "PLANE_API_URL",
    "PLANE_RENAME_ALLOW_UNDRAINED",
    "PLANE_RENAME_SNIPPET_IDLE_SECONDS",
)


def test_runbook_mentions_every_env_var_and_owner_task():
    tasks = tomllib.loads((ROOT / "mise.toml").read_text())["tasks"]
    owner_tasks = [name for name in tasks if name.startswith("owner:plane-workspace-rename:")]
    assert len(owner_tasks) == 3
    variables = (*REQUIRED, *OPTIONAL, *workspace_rename.SERVICE_VARIABLES)
    missing = [name for name in (*variables, *owner_tasks) if name not in RUNBOOK]
    assert missing == []


def test_runbook_is_linked_from_readme_and_extensions():
    for page in ("README.md", "docs/extensions.md"):
        assert "workspace-rename.md" in (ROOT / page).read_text()
