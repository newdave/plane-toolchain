"""Legacy deployment receipts depend on these payloads staying byte-identical.

Review deployment migration support before changing a frozen payload digest.
"""

import hashlib
from pathlib import Path


def test_server_payloads_match_existing_deployment_receipts():
    root = Path(__file__).parents[1] / "src/plane_toolchain"
    expected = {
        "ce_tools.py": "9838a26a0ee51aa8ce95dd1d4fb85d23df2b99f83e1c07e6c6146457ac45ec55",
        "pages_backend.py": "0b971bc41a59d6273fd7b62c6601ca5a69991cb0d51ff65489720443fd2154bd",
    }
    for name, digest in expected.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
