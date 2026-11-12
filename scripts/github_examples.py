"""Install/remove example workflows without overwriting another project's work."""

import hashlib
import json
import sys
from pathlib import Path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install(operation, target):
    target = target.resolve(strict=True)
    receipt = target / ".agent-state/plane-toolchain/workflows.json"
    destination = target / ".github/workflows"
    source = Path(__file__).resolve().parents[1] / "examples/github"
    files = sorted(source.glob("*.yml"))
    if receipt.is_symlink() or destination.is_symlink() or receipt.parent.is_symlink():
        raise ValueError("Refusing symlinked installation paths")
    if operation == "apply":
        if receipt.exists():
            saved = json.loads(receipt.read_text())
            if all(
                (destination / name).is_file() and digest(destination / name) == value
                for name, value in saved.items()
            ):
                print("Workflow examples already installed")
                return
            raise ValueError("Installed files changed; preserve and reconcile them")
        if any(
            (destination / path.name).exists() or (destination / path.name).is_symlink()
            for path in files
        ):
            raise ValueError("Workflow file already exists; refusing overwrite")
        destination.mkdir(parents=True, exist_ok=True)
        receipt.parent.mkdir(parents=True, exist_ok=True)
        saved = {}
        for path in files:
            dest = destination / path.name
            with dest.open("xb") as output:
                output.write(path.read_bytes())
            saved[path.name] = digest(dest)
        receipt.write_text(json.dumps(saved, indent=2) + "\n")
        print("Installed three workflow examples; review before committing")
    elif operation == "revert":
        if not receipt.exists():
            print("No installation receipt; nothing removed")
            return
        saved = json.loads(receipt.read_text())
        allowed = {path.name for path in files}
        if not saved or set(saved) - allowed:
            raise ValueError("Invalid installation receipt")
        for name, value in saved.items():
            path = destination / name
            if path.is_symlink() or not path.is_file() or digest(path) != value:
                raise ValueError("Installed file changed; refusing removal")
        for name in saved:
            (destination / name).unlink()
        receipt.unlink()
        print("Removed unchanged workflow examples")
    else:
        raise ValueError("Expected apply or revert")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("Expected apply/revert and target checkout path")
    install(sys.argv[1], Path(sys.argv[2]))
