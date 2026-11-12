"""Change only the host-local Plane enable switch; never touch encrypted settings."""

import json
import os
import re
import sys
import tempfile
from pathlib import Path

from plane_toolchain.session import assignments


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
        previous = [value for _, value in assignments(text, ("PLANE_ENABLED",))]
        if any(value not in {"true", "false"} for value in previous):
            raise ValueError("PLANE_ENABLED must be a literal true or false before activation")
        backup.parent.mkdir(parents=True, exist_ok=True)
        # Temp file, fsync, then atomic replace: an interrupted write never leaves a bad backup.
        fd, tmp = tempfile.mkstemp(dir=backup.parent, prefix=".backup-")
        try:
            with os.fdopen(fd, "w") as output:
                json.dump({"enabled": bool(previous) and previous[-1] == "true"}, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(tmp, backup)
        finally:
            Path(tmp).unlink(missing_ok=True)
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
