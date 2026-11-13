"""Test configuration: shared support modules get pytest's assertion introspection."""

import pytest

for name in (
    "support_pages_backend",
    "support_pages_deploy",
    "support_workspace_rename_backend",
    "support_workspace_rename_deploy",
    "support_workspace_rename_hardening",
):
    pytest.register_assert_rewrite(name)
