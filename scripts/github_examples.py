"""Install/remove example workflows without overwriting another project's work."""

import hashlib
import json
import os
import sys
from pathlib import Path

RECEIPT = Path(".agent-state/plane-toolchain/workflows.json")
WORKFLOWS = Path(".github/workflows")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def contained(target, relative):
    """Return target/relative after refusing a symlink in any component below target."""
    current = target
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Refusing symlinked installation paths")
    if current.exists() and not current.resolve().is_relative_to(target):
        raise ValueError("Refusing installation paths outside the target")
    return current


def write_atomic(path, data):
    """Write data to path through a temporary sibling so a crash never leaves a partial file."""
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def place(destination, name, data):
    """Create destination/name from data, never replacing an existing file."""
    temporary = destination / f".{name}.tmp"
    final = destination / name
    try:
        with temporary.open("wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, final)
    finally:
        temporary.unlink(missing_ok=True)


def install(operation, target):
    target = target.resolve(strict=True)
    receipt = contained(target, RECEIPT)
    destination = contained(target, WORKFLOWS)
    source = Path(__file__).resolve().parents[1] / "examples/github"
    files = {path.name: path for path in sorted(source.glob("*.yml"))}
    current = {name: digest(path) for name, path in files.items()}
    if operation == "apply":
        if receipt.exists():
            saved = json.loads(receipt.read_text())
            if saved != current:
                raise ValueError(
                    "Installed examples are outdated or differ from this checkout; "
                    "revert, then apply again"
                )
            missing = [name for name in saved if not (destination / name).exists()]
            for name in saved:
                path = destination / name
                if path.exists() and (path.is_symlink() or digest(path) != saved[name]):
                    raise ValueError("Installed files changed; preserve and reconcile them")
            if not missing:
                print("Workflow examples already installed")
                return
            for name in missing:
                place(destination, name, files[name].read_bytes())
            print("Completed an interrupted installation; review before committing")
            return
        if any(
            (destination / name).exists() or (destination / name).is_symlink() for name in files
        ):
            raise ValueError("Workflow file already exists; refusing overwrite")
        destination.mkdir(parents=True, exist_ok=True)
        receipt.parent.mkdir(parents=True, exist_ok=True)
        # The receipt lists every file before any is placed, so an interrupted apply is
        # visible and can be completed or reverted instead of leaving unrecorded files.
        write_atomic(receipt, (json.dumps(current, indent=2) + "\n").encode())
        for name, path in files.items():
            place(destination, name, path.read_bytes())
        print("Installed three workflow examples; review before committing")
    elif operation == "revert":
        if not receipt.exists():
            print("No installation receipt; nothing removed")
            return
        saved = json.loads(receipt.read_text())
        if not saved or set(saved) - set(files):
            raise ValueError("Invalid installation receipt")
        for name, value in saved.items():
            path = destination / name
            if not path.exists() and not path.is_symlink():
                continue  # an interrupted apply never placed this file
            if path.is_symlink() or not path.is_file() or digest(path) != value:
                raise ValueError("Installed file changed; refusing removal")
        for name in saved:
            (destination / name).unlink(missing_ok=True)
        receipt.unlink()
        print("Removed unchanged workflow examples")
    else:
        raise ValueError("Expected apply or revert")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("Expected apply/revert and target checkout path")
    install(sys.argv[1], Path(sys.argv[2]))
