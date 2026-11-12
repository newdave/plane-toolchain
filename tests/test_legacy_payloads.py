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
        "ce_tools.py": "5b48127fbe67780e6d43761cb4b2e57050fd6bdd23044759274f1b4bf624a68f",
        # PTC-19: digest changed (client messages from a fixed table; CodeQL stack-trace alerts).
        # PTC-44: digest changed (page update route, force_close via Redis); see docs/extensions.md.
        "pages_backend.py": "52167b99f187ad31743f94b5ecc9c52bebbe435e17cd4805bedba682ec882631",
    }
    for name, digest in expected.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
