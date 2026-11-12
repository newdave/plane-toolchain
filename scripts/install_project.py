"""Install/remove Plane MCP and task-management integration in another project.

Stdlib only, so an external checkout can run it with any python3 >= 3.11:

    python3 /path/to/plane-toolchain/scripts/install_project.py apply .
    python3 /path/to/plane-toolchain/scripts/install_project.py revert .

Every change is recorded in .plane/install.json. Apply never overwrites existing
Plane configuration; revert removes only what is still exactly as installed.
"""

import argparse
import json
import os
import re
import sys
import tempfile
import tomllib
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
TEMPLATES = SOURCE / "templates/project"
RECEIPT = ".plane/install.json"
MARKER = "<!-- plane-toolchain:begin"
BLOCK = (TEMPLATES / "AGENTS.md").read_text()
SHIM = (TEMPLATES / "plane.sh").read_text()
GITIGNORE = (
    "# Local Plane values, secrets and switch state; never commit them.\n"
    "local.env\n*.enc.env\nenable-backup.json\n.plane-*\n.backup-*\n"
)
MCP_ENTRY = {"command": "bash", "args": [".plane/plane.sh", "mcp"]}
HOOK_GROUP = {
    "hooks": [
        {
            "type": "command",
            "command": 'f="$(git rev-parse --show-toplevel)/.plane/plane.sh"; '
            '[ ! -f "$f" ] || bash "$f" session',
            "timeout": 5,
        }
    ]
}
CODEX_BLOCK = (
    "# plane-toolchain:begin\n"
    "[mcp_servers.plane]\n"
    'command = "bash"\n'
    'args = [".plane/plane.sh", "mcp"]\n'
    "# plane-toolchain:end\n"
)
# Directories the installer may create and, once empty, remove again.
DIRECTORIES = (".plane", ".claude", ".codex")


class Changed(ValueError):
    """An installed artefact no longer matches what the installer wrote."""


def insert_file(content, *, reuse=False):
    def insert(old):
        # Revert keeps an identical .gitignore while ignored local files remain.
        if old is not None and not (reuse and old == content):
            raise ValueError("already exists; refusing overwrite")
        return content

    def remove(text, created):
        if text != content:
            raise Changed("changed since installation")
        return None

    return insert, remove


def insert_block(old):
    if old is not None and MARKER in old:
        raise ValueError("already contains a plane-toolchain block")
    return separate(old) + BLOCK


def separate(old):
    """Return existing text followed by one blank line, keeping its own line endings."""
    if not old or not old.strip():
        return ""
    return (old if old.endswith("\n") else old + "\n") + "\n"


def strip(text, block, created):
    """Remove an appended block and its separator; None when nothing else remains."""
    separated = "\n" + block
    text = text.replace(separated, "", 1) if separated in text else text.replace(block, "", 1)
    return None if created and not text.strip() else text


def remove_block(text, created):
    if BLOCK not in text:
        raise Changed("Plane block changed since installation")
    return strip(text, BLOCK, created)


def load(old):
    data = json.loads(old) if old is not None else {}
    if not isinstance(data, dict):
        raise ValueError("is not a JSON object")
    return data


def dump(data):
    return json.dumps(data, indent=2) + "\n"


def insert_server(old):
    data = load(old)
    servers = data.setdefault("mcpServers", {})
    if "plane" in servers:
        raise ValueError("already defines a plane MCP server")
    servers["plane"] = MCP_ENTRY
    return dump(data)


def remove_server(text, created):
    data = load(text)
    servers = data.get("mcpServers", {})
    if servers.get("plane") != MCP_ENTRY:
        raise Changed("plane MCP server changed since installation")
    del servers["plane"]
    if not servers:
        del data["mcpServers"]
    return None if created and not data else dump(data)


def insert_hook(old):
    data = load(old)
    groups = data.setdefault("hooks", {}).setdefault("SessionStart", [])
    if HOOK_GROUP in groups:
        raise ValueError("already has the Plane SessionStart hook")
    groups.append(HOOK_GROUP)
    return dump(data)


def remove_hook(text, created):
    data = load(text)
    groups = data.get("hooks", {}).get("SessionStart", [])
    if HOOK_GROUP not in groups:
        raise Changed("Plane SessionStart hook changed since installation")
    groups.remove(HOOK_GROUP)
    if not groups:
        del data["hooks"]["SessionStart"]
    if not data["hooks"]:
        del data["hooks"]
    return None if created and not data else dump(data)


def insert_codex(old):
    if "plane" in tomllib.loads(old or "").get("mcp_servers", {}):
        raise ValueError("already defines a plane MCP server")
    text = separate(old) + CODEX_BLOCK
    tomllib.loads(text)
    return text


def remove_codex(text, created):
    if CODEX_BLOCK not in text:
        raise Changed("plane MCP server changed since installation")
    return strip(text, CODEX_BLOCK, created)


KINDS = {
    "shim": insert_file(SHIM),
    "gitignore": insert_file(GITIGNORE, reuse=True),
    "include": insert_file("@AGENTS.md\n"),
    "block": (insert_block, remove_block),
    "mcp-json": (insert_server, remove_server),
    "hooks-json": (insert_hook, remove_hook),
    "codex-toml": (insert_codex, remove_codex),
}


def read(path):
    return path.read_text() if path.exists() else None


def write(path, text, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".install-")
    try:
        with os.fdopen(fd, "w") as output:
            output.write(text)
        if path.exists():
            mode = path.stat().st_mode & 0o777
        os.chmod(tmp, mode or (0o755 if text.startswith("#!") else 0o644))
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def guard(target, rel):
    """Refuse symlinks anywhere between the target root and the file."""
    path = target / rel
    for part in (path, *path.parents):
        if part == target:
            return path
        if part.is_symlink():
            raise ValueError(f"{rel}: refusing symlinked installation path")
    raise ValueError(f"{rel}: outside target")


def plan(target):
    """Return (relative path, kind) pairs for this target's current layout."""
    claude = read(guard(target, "CLAUDE.md"))
    steps = [
        (".plane/plane.sh", "shim"),
        (".plane/.gitignore", "gitignore"),
        ("AGENTS.md", "block"),
    ]
    if claude is None:
        steps.append(("CLAUDE.md", "include"))
    elif not re.search(r"(?m)^@AGENTS\.md\s*$", claude):
        steps.append(("CLAUDE.md", "block"))
    return steps + [
        (".mcp.json", "mcp-json"),
        (".claude/settings.json", "hooks-json"),
        (".codex/config.toml", "codex-toml"),
        (".codex/hooks.json", "hooks-json"),
    ]


def reverted(target, receipt):
    """Return {path: new text or None}; raise Changed when anything was edited."""
    entries = receipt.get("entries")
    if receipt.get("version") != 1 or not isinstance(entries, dict) or not entries:
        raise ValueError("Invalid installation receipt")
    result = {}
    for rel, entry in entries.items():
        if entry.get("kind") not in KINDS:
            raise ValueError("Invalid installation receipt")
        text = read(guard(target, rel))
        if text is None:
            raise Changed(f"{rel}: removed since installation")
        try:
            result[rel] = KINDS[entry["kind"]][1](text, entry.get("created") is True)
        except (ValueError, tomllib.TOMLDecodeError) as error:
            raise Changed(f"{rel}: {error}") from None
    return result


def local_env(toolchain, values):
    lines = [f"PLANE_TOOLCHAIN={toolchain}", "PLANE_ENABLED=false"]
    for name, value in values.items():
        if value is not None:
            if not value or any(c.isspace() or c in "\"'#$`\\" for c in value):
                raise ValueError(f"{name}: invalid value")
            lines.append(f"{name}={value}")
    return (
        "# Non-secret local Plane values. Never put PLANE_API_KEY here.\n" + "\n".join(lines) + "\n"
    )


def apply(target, values=None, toolchain=SOURCE):
    target = target.resolve(strict=True)
    receipt_path = guard(target, RECEIPT)
    if receipt_path.exists():
        reverted(target, json.loads(receipt_path.read_text()))
        print("Plane integration already installed")
        return
    changes, entries = {}, {}
    for rel, kind in plan(target):
        old = read(guard(target, rel))
        try:
            changes[rel] = KINDS[kind][0](old)
        except (ValueError, tomllib.TOMLDecodeError) as error:
            raise ValueError(f"{rel}: {error}") from None
        entries[rel] = {"kind": kind, "created": old is None}
    local = guard(target, ".plane/local.env")
    seeded = None if local.exists() else local_env(toolchain, values or {})
    created = [name for name in DIRECTORIES if not (target / name).exists()]
    for rel, text in changes.items():
        write(target / rel, text)
    if seeded is not None:
        write(local, seeded, 0o600)
    receipt = {"version": 1, "directories": created, "entries": entries}
    write(receipt_path, json.dumps(receipt, indent=2) + "\n")
    print("Installed Plane integration; review the diff before committing.")
    print("Next: run 'mise run setup' in the toolchain, set .plane/local.env values,")
    print("run '.plane/plane.sh enable', then restart Codex or Claude sessions.")


def revert(target):
    target = target.resolve(strict=True)
    receipt_path = guard(target, RECEIPT)
    if not receipt_path.exists():
        print("No installation receipt; nothing removed")
        return
    receipt = json.loads(receipt_path.read_text())
    changes = reverted(target, receipt)
    # Ignored local files (values, encrypted key, switch baseline) are the user's to delete.
    plane = target / ".plane"
    ours = {"plane.sh", ".gitignore", "install.json"}
    kept = sorted(p.name for p in plane.iterdir() if p.name not in ours)
    if kept:
        changes.pop(".plane/.gitignore", None)
    for rel, text in changes.items():
        if text is None:
            (target / rel).unlink()
        else:
            write(target / rel, text)
    receipt_path.unlink()
    # .plane/ belongs to the installer; client directories go only if it created them.
    for name in {".plane", *receipt.get("directories", [])}:
        if name in DIRECTORIES and (target / name).is_dir() and not any((target / name).iterdir()):
            (target / name).rmdir()
    print("Removed unchanged Plane integration")
    if kept:
        print("Kept ignored local files in .plane/: " + ", ".join(kept))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("operation", choices=["apply", "revert"])
    parser.add_argument("target", type=Path, help="target project checkout")
    parser.add_argument("--url", help="hosted MCP endpoint written to .plane/local.env")
    parser.add_argument("--workspace", help="workspace slug written to .plane/local.env")
    parser.add_argument("--project", help="project UUID written to .plane/local.env")
    args = parser.parse_args(argv)
    try:
        if args.operation == "apply":
            values = {
                "PLANE_MCP_URL": args.url,
                "PLANE_WORKSPACE": args.workspace,
                "PLANE_PROJECT": args.project,
            }
            apply(args.target, values)
        else:
            revert(args.target)
    except (ValueError, OSError) as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()
