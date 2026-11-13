"""Report only the checkout's non-secret Plane switch, without evaluating shell text."""

import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

# Installed projects point at their own root; this checkout is the default.
ROOT = Path(os.environ.get("PLANE_CONFIG_ROOT") or Path(__file__).resolve().parents[2])
# Non-secret connection values only. PLANE_API_KEY is never read from checkout files.
CONNECTION = ("PLANE_MCP_URL", "PLANE_WORKSPACE", "PLANE_PROJECT")


def assignments(text: str, names: tuple[str, ...]) -> list[tuple[str, str]]:
    """Return (name, value) for each literal KEY=value line, in file order."""
    pattern = re.compile(r"\s*(?:export\s+)?(" + "|".join(names) + r")=(.*?)\s*")
    found = []
    for line in text.splitlines():
        match = pattern.fullmatch(line)
        if match:
            found.append((match[1], match[2].split(" #", 1)[0].strip().strip("\"'")))
    return found


def main_checkout(root: Path) -> Path | None:
    """Return the main checkout when root is a linked git worktree, else None."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv
            [  # noqa: S607
                "git",
                "rev-parse",
                "--absolute-git-dir",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    git_dir, _, common = result.stdout.strip().partition("\n")
    # Equal dirs: a main checkout, or a copy nested in an unrelated repo. Neither falls back.
    if git_dir == common or Path(common).name != ".git":
        return None
    return Path(common).parent


def shared_file(main: Path | None, rel: str) -> list[Path]:
    """Return the main checkout's file as a layer only when it is a safe regular file."""
    if main is None:
        return []
    path = main / rel
    if path.is_symlink() or not path.is_file():
        return []
    return [path] if path.resolve().is_relative_to(main.resolve()) else []


def local_layers(root: Path) -> list[Path]:
    """Return local settings files, lowest precedence first.

    A worktree's own config/local.env overrides the main checkout's key by key. The main
    checkout is consulted only when the worktree has neither config/local.env nor .deploy/env.
    """
    local = root / "config/local.env"
    legacy = root / ".deploy/env"
    if local.exists() or not legacy.exists():
        main = main_checkout(root)
        return [*shared_file(main, "config/local.env"), local]
    return [legacy]


def literal(root: Path, names: tuple[str, ...]) -> dict[str, str]:
    """Read KEY=value lines from checkout config files; later files win.

    Raises OSError or UnicodeDecodeError when a present file cannot be read.
    """
    values = {}
    paths = (root / "config/defaults.env", root / ".env", *local_layers(root))
    # A project set up by scripts/install_project.py keeps its values only under .plane/.
    if (root / ".plane/plane.sh").is_file():
        paths = (*shared_file(main_checkout(root), ".plane/local.env"), root / ".plane/local.env")
    for path in paths:
        if path.is_file():
            values.update(assignments(path.read_text(), names))
    return values


def connection(root: Path, env: Mapping[str, str]) -> dict[str, str]:
    """Return file-provided connection values that the environment does not override."""
    try:
        values = literal(root, CONNECTION)
    except (OSError, UnicodeDecodeError):
        return {}
    return {k: v for k, v in values.items() if k not in env and v}


def switch(root: Path, env: Mapping[str, str]) -> str:
    if "PLANE_ENABLED" in env:
        value = env["PLANE_ENABLED"]
    else:
        try:
            value = literal(root, ("PLANE_ENABLED",)).get("PLANE_ENABLED", "false")
        except (OSError, UnicodeDecodeError):
            return "invalid"
    return {"true": "enabled", "false": "disabled"}.get(value, "invalid")


def status(root: Path, env: Mapping[str, str]) -> str:
    return f"Plane: {switch(root, env)} (checkout switch; live connection not checked)."


def main() -> None:
    if sys.argv[1:] == ["status"]:
        print(switch(ROOT, os.environ))
        return
    if sys.argv[1:] == ["connection"]:
        # NUL-separated so the bridge can export values without evaluating them.
        for name, value in connection(ROOT, os.environ).items():
            sys.stdout.write(f"{name}={value}\0")
        return
    if len(sys.argv) == 3 and sys.argv[1] == "main":
        # Prints the main checkout path only, for the bridge; exits 1 when there is none.
        found = main_checkout(Path(sys.argv[2]))
        if found is None:
            sys.exit(1)
        print(found)
        return
    message = status(ROOT, os.environ)
    print(
        json.dumps(
            {
                "systemMessage": message,
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": message,
                },
            }
        )
    )


if __name__ == "__main__":
    main()
