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
        "ce_tools.py": "6534960fec76509317c6574b1bb0000288f187c18a0acc600a7fba60a0b38359",
        # PTC-19: digest changed (client messages from a fixed table; CodeQL stack-trace alerts).
        # PTC-44: digest changed (page update route, force_close via Redis); see docs/extensions.md.
        # PTC-29: digest changed (comment and docstring wording only).
        # PTC-69: digest changed (non-object request bodies and Live replies are 400/502, not 500).
        # PTC-70: digest changed (update returns at once with late_store_check; no post-commit polling).
        "pages_backend.py": "9ebbac188b6fda856a0842b34de7f4fb93e97b894ea46b8d8e2631a52af0f65c",
    }
    for name, digest in expected.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
