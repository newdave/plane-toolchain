"""Change only the host-local Plane enable switch; never touch encrypted settings."""

import json
import os
import re
import sys
import tempfile
from pathlib import Path


def set_enabled(path: Path, enabled: bool, *, restore: bool = True):
    if path.is_symlink():
        raise ValueError("Refusing a symlinked local configuration")
    if not path.exists() and (path.parent.parent / ".deploy/env").exists():
        raise ValueError("Refusing to shadow legacy .deploy/env; migrate to config/local.env first")
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text() if path.exists() else ""
    # Keep the first activation baseline: repeated apply/revert must remain idempotent.
    backup = path.parent.parent / ".agent-state/plane/enable-backup.json"
    if backup.is_symlink() or backup.parent.is_symlink():
        raise ValueError("Refusing symlinked Plane backup")
    if enabled and not backup.exists():
        previous = re.findall(r"(?m)^\s*(?:export\s+)?PLANE_ENABLED=(.*)$", text)
        if any(value.strip() not in {"true", "false"} for value in previous):
            raise ValueError("PLANE_ENABLED must be a literal true or false before activation")
        backup.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump({"enabled": previous[-1].strip() == "true" if previous else False}, output)
    if not enabled and restore and backup.exists():
        saved = json.loads(backup.read_text())
        if not isinstance(saved.get("enabled"), bool):
            raise ValueError("Invalid Plane activation backup")
        enabled = saved["enabled"]
    text = re.sub(r"(?m)^\s*(?:export\s+)?PLANE_ENABLED=.*(?:\n|$)", "", text)
    text = text.rstrip("\n") + "\nPLANE_ENABLED=" + str(enabled).lower() + "\n"
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".plane-")
    try:
        with os.fdopen(fd, "w") as output:
            output.write(text)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"apply", "revert", "enable", "disable"}:
        sys.exit("Expected apply, revert, enable or disable")
    set_enabled(
        Path("config/local.env"),
        sys.argv[1] in {"apply", "enable"},
        restore=sys.argv[1] != "disable",
    )
