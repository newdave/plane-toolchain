"""Legacy tripwire names, plus the neutral names the deploy helpers write.

The Pages and MCP helpers no longer read legacy-named host state (PTC-30). They only detect
these four persisted names and refuse before any change; see docs/decisions/0001-neutral-names.md.
A guard test (tests/test_legacy_names.py) rejects new references elsewhere.
"""

# Persisted legacy names: Pages backup container suffix and source label; MCP backup and receipt
# (the receipt's temp file adds `.tmp`). Detected only; never read or written.
PAGES_BACKUP_SUFFIX = "-before-terrainlink-pages"
PAGES_LABEL = "terrainlink.pages.source"
MCP_BACKUP_NAME = "server.py.before-terrainlink-ce"
MCP_RECEIPT_NAME = "terrainlink-ce-receipt.json"

# Neutral names: the only ones the helpers write.
NEUTRAL_PAGES_BACKUP_SUFFIX = "-before-plane-toolchain-pages"
NEUTRAL_PAGES_LABEL = "plane-toolchain.pages.source"
NEUTRAL_PAGES_IMAGE_PREFIX = "plane-toolchain-pages:"
NEUTRAL_PAGES_BACKEND_FILE = "/code/plane/api/plane_toolchain_pages.py"
NEUTRAL_PAGES_BACKEND_MODULE = "plane.api.plane_toolchain_pages"
NEUTRAL_MCP_BACKUP_NAME = "server.py.before-plane-toolchain-ce"
NEUTRAL_MCP_RECEIPT_NAME = "plane-toolchain-ce-receipt.json"

# Constants shipped into each stdlib-only remote program (the host never imports this package).
# The remotes know the legacy names only to refuse; they write only neutral names.
PAGES = {
    "PAGES_BACKUP_SUFFIX": PAGES_BACKUP_SUFFIX,
    "PAGES_LABEL": PAGES_LABEL,
    "NEUTRAL_PAGES_BACKUP_SUFFIX": NEUTRAL_PAGES_BACKUP_SUFFIX,
    "NEUTRAL_PAGES_LABEL": NEUTRAL_PAGES_LABEL,
    "NEUTRAL_PAGES_IMAGE_PREFIX": NEUTRAL_PAGES_IMAGE_PREFIX,
    "NEUTRAL_PAGES_BACKEND_FILE": NEUTRAL_PAGES_BACKEND_FILE,
    "NEUTRAL_PAGES_BACKEND_MODULE": NEUTRAL_PAGES_BACKEND_MODULE,
}
MCP = {
    "MCP_BACKUP_NAME": MCP_BACKUP_NAME,
    "MCP_RECEIPT_NAME": MCP_RECEIPT_NAME,
    "NEUTRAL_MCP_BACKUP_NAME": NEUTRAL_MCP_BACKUP_NAME,
    "NEUTRAL_MCP_RECEIPT_NAME": NEUTRAL_MCP_RECEIPT_NAME,
}


def refusal(found):
    """Operator message for legacy-named host state; found is a list of names, never content."""
    return (
        "Legacy-named state found: " + ", ".join(sorted(found)) + ". "
        "This release no longer reads it. Migrate first with plane-toolchain at commit 1cb6153: "
        "Pages revert (apply only if check passes there), MCP apply. Or reconcile manually."
    )
