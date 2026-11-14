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
        "ce_tools.py": "c0093a9f090704486570c0b3116decd524c3a13288ac47a2ba3adc7686b813d6",
        # PTC-19: digest changed (client messages from a fixed table; CodeQL stack-trace alerts).
        # PTC-44: digest changed (page update route, force_close via Redis); see docs/extensions.md.
        # PTC-29: digest changed (comment and docstring wording only).
        # PTC-69: digest changed (non-object request bodies and Live replies are 400/502, not 500).
        # PTC-70: digest changed (update returns at once with late_store_check; no post-commit polling).
        # PTC-70: digest changed again (late_store_check carries html_sha256).
        "pages_backend.py": "03ae665105e1fb74ec10e5b42ed27207ca016386b3495decbb406b8d020f71eb",
    }
    for name, digest in expected.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
