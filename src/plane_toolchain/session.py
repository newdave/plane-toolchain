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
URL = "PLANE_MCP_URL"
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
    .deploy/env is a legacy layer kept for older checkouts: used only when config/local.env is absent.
    """
    local = root / "config/local.env"
    legacy = root / ".deploy/env"
    if local.exists() or not legacy.exists():
        main = main_checkout(root)
        return [*shared_file(main, "config/local.env"), local]
    return [legacy]


def installed_layers(root: Path) -> list[Path]:
    """Return an installed project's local settings files, lowest precedence first.

    The ignored config/local.env is the home. A project not yet migrated keeps its values in
    .plane/local.env, which is read only while no config/local.env exists, never merged.
    """
    main = main_checkout(root)
    current = [*shared_file(main, "config/local.env"), root / "config/local.env"]
    if any(path.is_file() for path in current):
        return current
    return [*shared_file(main, ".plane/local.env"), root / ".plane/local.env"]


def legacy_layout(root: Path) -> bool:
    """True for an installed project that still keeps its values under .plane/."""
    return (root / ".plane/plane.sh").is_file() and installed_layers(root)[
        -1
    ].parent.name == ".plane"


def in_checkout(path: Path) -> bool:
    """True when the (resolved) file sits in a git checkout.

    A checkout is a .git entry, a directory or the file of a linked worktree, in the file's
    directory or an ancestor.
    """
    return any((d / ".git").exists() for d in (path.parent, *path.parent.parents))


def tracked(path: Path) -> bool:
    """Return True unless the file is known to be uncommitted; the URL guard fails closed.

    A symlink is tracked: the link name may be untracked while its target is committed. The path
    is resolved first, so a relative or symlinked directory still finds its checkout. The name
    is matched case-insensitively (icase), so on a case-insensitive filesystem a committed
    `.Env` still counts for `.env`.

    Outside a git checkout every outcome is False, because the file cannot be committed. Inside
    one, False only when git exits 1 (not tracked); exit 0, any other exit (such as 128 for
    dubious ownership or a corrupt repo) and a missing git (OSError) are tracked.
    """
    if path.is_symlink():
        return True
    path = path.resolve()
    spec = f":(icase,literal){path.name}"
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv
            ["git", "ls-files", "--error-unmatch", "--", spec],  # noqa: S607
            cwd=path.parent,
            capture_output=True,
        )
    except OSError:
        return in_checkout(path)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return in_checkout(path)


def literal(root: Path, names: tuple[str, ...]) -> dict[str, str]:
    """Read KEY=value lines from checkout config files; later files win.

    Raises OSError or UnicodeDecodeError when a present file cannot be read.
    """
    values = {}
    paths = (root / "config/defaults.env", root / ".env", *local_layers(root))
    # A project set up by scripts/install_project.py reads only its own local.env, not the
    # project's other config files.
    if (root / ".plane/plane.sh").is_file():
        paths = tuple(installed_layers(root))
    for path in paths:
        if path.is_file():
            found = assignments(path.read_text(), names)
            # The URL receives the API key, so a file a commit can change never supplies it.
            # config/defaults.env is tracked by definition; other layers are asked of git.
            if any(name == URL for name, _ in found) and (
                path == root / "config/defaults.env" or tracked(path)
            ):
                found = [(name, value) for name, value in found if name != URL]
            values.update(found)
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
        if legacy_layout(ROOT):
            print(
                "plane: .plane/local.env is deprecated; move it to config/local.env",
                file=sys.stderr,
            )
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
