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
        "ce_tools.py": "3251a424debf574af740455e95abc4861c8edc7d2b4953fc2f29aa33700dc855",
        # PTC-19: digest changed (client messages from a fixed table; CodeQL stack-trace alerts).
        # PTC-44: digest changed (page update route, force_close via Redis); see docs/extensions.md.
        # PTC-29: digest changed (comment and docstring wording only).
        # PTC-69: digest changed (non-object request bodies and Live replies are 400/502, not 500).
        "pages_backend.py": "566f0e63991752f60f215ff9b356a27c11c6ce1edc843bc537aaacdc9ece34f9",
    }
    for name, digest in expected.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
