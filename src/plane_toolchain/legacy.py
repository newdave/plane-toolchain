"""The only module allowed to spell legacy identifiers; neutral replacements sit beside them.

Deploy code reads the legacy names on existing hosts; the Pages and MCP helpers write only the
neutral ones (PTC-27, PTC-28). See docs/decisions/0001-neutral-names.md for the mapping and
migration plan. A guard test (tests/test_legacy_names.py) rejects new references elsewhere.
"""

# Pages: backup container suffix, source label, derived image prefix, backend module.
PAGES_BACKUP_SUFFIX = "-before-terrainlink-pages"
PAGES_LABEL = "terrainlink.pages.source"
PAGES_IMAGE_PREFIX = "terrainlink-plane-pages:"
PAGES_BACKEND_FILE = "/code/plane/api/terrainlink_pages.py"
PAGES_BACKEND_MODULE = "plane.api.terrainlink_pages"

# MCP: upstream-original backup and install receipt (the receipt's temp file adds `.tmp`).
MCP_BACKUP_NAME = "server.py.before-terrainlink-ce"
MCP_RECEIPT_NAME = "terrainlink-ce-receipt.json"

# Neutral replacements (PTC-27/28 migrate to these).
NEUTRAL_PAGES_BACKUP_SUFFIX = "-before-plane-toolchain-pages"
NEUTRAL_PAGES_LABEL = "plane-toolchain.pages.source"
NEUTRAL_PAGES_IMAGE_PREFIX = "plane-toolchain-pages:"
NEUTRAL_PAGES_BACKEND_FILE = "/code/plane/api/plane_toolchain_pages.py"
NEUTRAL_PAGES_BACKEND_MODULE = "plane.api.plane_toolchain_pages"
NEUTRAL_MCP_BACKUP_NAME = "server.py.before-plane-toolchain-ce"
NEUTRAL_MCP_RECEIPT_NAME = "plane-toolchain-ce-receipt.json"

# Constants shipped into each stdlib-only remote program (the host never imports this package).
# The Pages remote reads the legacy backup suffix and label; it writes only neutral names.
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
