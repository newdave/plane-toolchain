"""Legacy deployment receipts depend on these payloads staying byte-identical.

Review deployment migration support before changing a frozen payload digest.
"""

import hashlib
from pathlib import Path


def test_server_payloads_match_existing_deployment_receipts():
    root = Path(__file__).parents[1] / "src/plane_toolchain"
    expected = {
        # PTC-13: digest changed (canonical UUIDs, SDK guard, FASTMCP_HOST); see docs/extensions.md.
        # PTC-44: digest changed (ce_page update action); see docs/extensions.md.
        # PTC-29: digest changed (comment wording only; provenance line removed).
        # PTC-70: digest changed (update tool description mentions late_store_check).
        # PTC-71: digest changed (relation_result docstring: cross-project ids).
        # PTC-70: digest changed again (async ce_page update confirms the late store itself).
        # ce_intake tool added (decide via the routed detail PATCH); see docs/extensions.md.
        # PTC-92: digest changed (update refuses an empty body; access refuses bool).
        # PTC-97: digest changed (ce_page runs every SDK call in a thread).
        # PTC-111: digest changed (tool bodies are module-level functions; one SDK thread helper).
        "ce_tools.py": "a360fc1f6964f396700cfbfdddff09ae6c705f00cc439673b666bc420a78853d",
        # PTC-19: digest changed (client messages from a fixed table; CodeQL stack-trace alerts).
        # PTC-44: digest changed (page update route, force_close via Redis); see docs/extensions.md.
        # PTC-29: digest changed (comment and docstring wording only).
        # PTC-69: digest changed (non-object request bodies and Live replies are 400/502, not 500).
        # PTC-70: digest changed (update returns at once with late_store_check; no post-commit polling).
        # PTC-70: digest changed again (late_store_check carries html_sha256).
        # PTC-92: digest changed (update refuses an empty body; access refuses bool).
        "pages_backend.py": "d9738acc55f8b8166ab935e16831cfd7b7ac38775682fda7f437904553f4254e",
    }
    for name, digest in expected.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
