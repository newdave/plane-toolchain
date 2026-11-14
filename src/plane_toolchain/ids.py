"""UUID, task identifier and relation-type helpers shared across the CLI modules.

ce_tools.py ships alone as the MCP server entry point and cannot import this module. It keeps
a local copy of RELATION_TYPES and canonical_uuid; tests/test_ids.py pins the two together.
"""

import re
from uuid import UUID

IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9]*-\d+")
RELATION_TYPES = (
    "blocking",
    "blocked_by",
    "start_before",
    "start_after",
    "finish_before",
    "finish_after",
)


def is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def canonical(value: str) -> str:
    """Return the canonical lowercase hyphenated form; raises ValueError if not a UUID."""
    return str(UUID(value))
