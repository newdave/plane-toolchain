"""Legacy deployment receipts depend on these payloads staying byte-identical.

Review deployment migration support before changing a frozen payload digest.
"""

import hashlib
from pathlib import Path


def test_server_payloads_match_existing_deployment_receipts():
    root = Path(__file__).parents[1] / "src/plane_toolchain"
    expected = {
        # PTC-13: digest changed (canonical UUIDs, SDK guard, FASTMCP_HOST); see docs/extensions.md.
        "ce_tools.py": "1d3866ab40b70c0070dbdd6ff01efd6e6c009a35b433b0cc271420df610ccda5",
        "pages_backend.py": "2ccbdc6f3b33e9227f97346d3d34c106acd93ab3f72fb0e0b3cf3f3ba190c4b3",
    }
    for name, digest in expected.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
