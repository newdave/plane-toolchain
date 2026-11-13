"""Install/remove Plane MCP and task-management integration in another project.

Stdlib only, so an external checkout can run it with any python3 >= 3.11:

    python3 /path/to/plane-toolchain/scripts/install_project.py apply .
    python3 /path/to/plane-toolchain/scripts/install_project.py label .
    python3 /path/to/plane-toolchain/scripts/install_project.py revert .

Every change is recorded in .plane/install.json. Apply never overwrites existing
Plane configuration; revert removes only what is still exactly as installed.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import tomllib
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

SOURCE = Path(__file__).resolve().parents[1]
TEMPLATES = SOURCE / "templates/project"
RECEIPT = ".plane/install.json"
MARKER = "<!-- plane-toolchain:begin"
BLOCK = (TEMPLATES / "AGENTS.md").read_text()
# Plane block as installed by PTC-48, before the awaiting-decision rule. Installs made then
# still carry it: revert accepts it, and apply reports it as outdated without rewriting it.
LEGACY_BLOCK = (
    "<!-- plane-toolchain:begin (managed by scripts/install_project.py; edit outside this block) -->\n"
    "## Task management: Plane\n"
    "\n"
    "Plane is the authoritative system for this project's scope, acceptance criteria, dependencies and status.\n"
    "\n"
    "- Use the `plane` MCP server for every task read and write. Reread each write to verify it.\n"
    "- Do not keep a parallel backlog in repository files, TODO comments or agent todo lists.\n"
    '- Treat "tasks" or "the queue" as this project\'s Plane work items.\n'
    "- Move a work item to In Progress only while it is actively worked on.\n"
    "- Move it to Done only when its acceptance criteria are met. A merged PR alone is not Done.\n"
    "- Store specs, plans and other planning narratives as project Pages, not repository files.\n"
    "- Keep Plane endpoints, project IDs and credentials out of committed files. Non-secret values go in ignored `.plane/local.env`. The API key comes from the environment or SOPS-encrypted `.plane/plane.enc.env`.\n"
    "- Check the local switch with `.plane/plane.sh status`. Change it with `.plane/plane.sh enable` or `disable`.\n"
    "<!-- plane-toolchain:end -->\n"
)
SHIM = (TEMPLATES / "plane.sh").read_text()
# Shim as installed by PTC-48, before the worktree fallback. Installs made then still carry it:
# revert accepts it, and apply reports it as outdated without rewriting it.
LEGACY_SHIM = (TEMPLATES / "plane.sh.legacy").read_text()
# SHA-256 of every shim version revert accepts besides the current template (exact bytes, no
# normalisation, so an edited shim is still refused). A target shfmt hook can reformat the
# shim, so each accepted source is also covered in these shfmt v3.14.1 styles, generated in a
# directory with no .editorconfig: `-i 0` (tabs, the defaults), `-i 2`, `-i 2 -ci`, `-i 4`.
# Any other style is refused: `git checkout` (or reconcile by hand) .plane/plane.sh before
# revert. TO ADD A VERSION: when plane.sh changes, keep the outgoing template in the set
# (its bytes plus the four shfmt forms) with fixtures under tests/fixtures; the test checks
# the set equals exactly the digests of the non-current fixture forms.
KNOWN_SHIM_SHA256 = {
    # plane.sh.legacy (PTC-48 shim; also matched as LEGACY_SHIM text), raw.
    "553f99744e23a956517c92da48625430921c7fda30f4b46ab8ec86c47cca9308",
    # plane.sh.legacy, shfmt -i 0.
    "2f64f488538f08c4d1cf7f395e19923aa9f94f8e0e69c23726cd8d4ba2b4cbe8",
    # plane.sh.legacy, shfmt -i 2.
    "915912c27ab6315ac4963a6da4737671b79330ec24da8eb1a5840e2cf57de867",
    # plane.sh.legacy, shfmt -i 2 -ci.
    "6a73817b925470f93e9fbb9442b8ed811d05e36dab3057b5d6f7af945c2bbeaf",
    # plane.sh.legacy, shfmt -i 4.
    "8b828c55a6399beda01812c860f08954300249c4420c1cbb105619e611c15e54",
    # plane.sh.pre-shfmt (shim before PTC-54, PTC-47 worktree fallback), raw.
    "35124f25fcdf23a3c5b300915cf752161e4721201d2cdc19f1fae30be2c73710",
    # plane.sh.pre-venv (PTC-54 shim before VIRTUAL_ENV clearing), raw. It is also the
    # shfmt -i 2 form of plane.sh.pre-venv and of plane.sh.pre-shfmt (shfmt-stable).
    "ba068b6d1f6ad6b1e09c1e6d1e979587eda7ddc675bdfa9dd2d715ef079b97a9",
    # plane.sh.pre-venv and plane.sh.pre-shfmt, shfmt -i 0 (same bytes from both).
    "17285686487c6c95aec80260927f4ef7cedf74e1a8d078d5cd630127790eb05e",
    # plane.sh.pre-venv and plane.sh.pre-shfmt, shfmt -i 2 -ci (same bytes from both).
    "67c0a37573d63a7ad8a40bd0825a74287ff23b5d8305b78295fd19018aca3d51",
    # plane.sh.pre-venv and plane.sh.pre-shfmt, shfmt -i 4 (same bytes from both).
    "fccb403454719381d6e607105361d1feee74465063b67a2730172376b1be43db",
    # plane.sh.pre-quotes (shim before PTC-76 PLANE_TOOLCHAIN quote stripping), raw and -i 2.
    "9534600d60faf3c8cf343f46690ba794b26d2099033ab1c927a05b620f66ed57",
    # plane.sh.pre-quotes, shfmt -i 0.
    "5ee180219ba62a63a99622bcf6ed63b1d59004114bd7ab5b7abf16a457b7ad97",
    # plane.sh.pre-quotes, shfmt -i 2 -ci.
    "3d333a638bc32443f40203e6d3e035de8aebc2205136890ee46dd030d2ceaf60",
    # plane.sh.pre-quotes, shfmt -i 4.
    "bf5d23d0062e49a0c79b817fa8f42d5a5ae8cb550f0b0ef62f87986badbdd598",
    # plane.sh (current template), shfmt -i 0. Its raw and -i 2 form is SHIM itself.
    "0c646a2bfdad71a388b36cdc0717eababe6c318d523d536ad30d533a636c1541",
    # plane.sh (current template), shfmt -i 2 -ci.
    "c72cf940db29097839da6d0e56653d2f8031f24dea5eef1c7c3ebafa6224b847",
    # plane.sh (current template), shfmt -i 4.
    "b3946f6369659da6f3c9581654758b5f0e580b8ed0bb2747ca4b53258b6d0bdc",
}
SETTING_NAMES = ("PLANE_MCP_URL", "PLANE_WORKSPACE", "PLANE_PROJECT")
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
# Must match the awaiting-decision label definition used in the toolchain's own project.
LABEL = {
    "name": "awaiting-decision",
    "color": "#EF4444",
    "description": "Agent is blocked until a human answers the question in the latest comment.",
}
# Directories the installer may create and, once empty, remove again.
DIRECTORIES = (".plane", ".claude", ".codex")


class Changed(ValueError):
    """An installed artefact no longer matches what the installer wrote."""


def insert_file(content, *, reuse=False, also=(), digests=()):
    def insert(old):
        # Revert keeps an identical .gitignore while ignored local files remain.
        if old is not None and not (reuse and old == content):
            raise ValueError("already exists; refusing overwrite")
        return content

    def remove(text, created):
        known = hashlib.sha256(text.encode()).hexdigest() in digests
        if text != content and text not in also and not known:
            raise Changed("changed since installation")
        return None

    return insert, remove


def eol(old):
    """Line ending of existing text: CRLF when it has any, else LF."""
    return "\r\n" if old and "\r\n" in old else "\n"


def insert_block(old):
    if old is not None and MARKER in old:
        raise ValueError("already contains a plane-toolchain block")
    return separate(old) + BLOCK.replace("\n", eol(old))


def separate(old):
    """Return existing text followed by one blank line, keeping its own line endings."""
    if not old or not old.strip():
        return ""
    ending = eol(old)
    return (old if old.endswith("\n") else old + ending) + ending


def variants(block):
    """The block with LF endings and with CRLF endings."""
    return (block, block.replace("\n", "\r\n"))


def strip(text, block, created):
    """Remove an appended block and its separator; None when nothing else remains."""
    for ending, found in zip(("\n", "\r\n"), variants(block), strict=True):
        if found in text:
            separated = ending + found
            text = (
                text.replace(separated, "", 1) if separated in text else text.replace(found, "", 1)
            )
            break
    return None if created and not text.strip() else text


def remove_block(text, created):
    for block in (BLOCK, LEGACY_BLOCK):
        if any(found in text for found in variants(block)):
            return strip(text, block, created)
    raise Changed("Plane block changed since installation")


def load(old):
    """Parse a JSON object artefact. Revert compares parsed values, never bytes, so a
    formatter's layout changes (indent, spacing, trailing newline) are accepted."""
    data = json.loads(old) if old is not None else {}
    if not isinstance(data, dict):
        raise ValueError("is not a JSON object")
    return data


def dump(data):
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def member(data, key, kind, *, default):
    """Return data[key], inserting the default when absent; refuse any other type."""
    value = data.setdefault(key, default)
    if not isinstance(value, kind):
        what = "a JSON object" if kind is dict else "a list"
        raise ValueError(f"{key} is not {what}")
    return value


def insert_server(old):
    data = load(old)
    servers = member(data, "mcpServers", dict, default={})
    if "plane" in servers:
        raise ValueError("already defines a plane MCP server")
    servers["plane"] = MCP_ENTRY
    return dump(data)


def remove_server(text, created):
    data = load(text)
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict) or servers.get("plane") != MCP_ENTRY:
        raise Changed("plane MCP server changed since installation")
    del servers["plane"]
    if not servers:
        del data["mcpServers"]
    return None if created and not data else dump(data)


def insert_hook(old):
    data = load(old)
    groups = member(member(data, "hooks", dict, default={}), "SessionStart", list, default=[])
    if HOOK_GROUP in groups:
        raise ValueError("already has the Plane SessionStart hook")
    groups.append(HOOK_GROUP)
    return dump(data)


def remove_hook(text, created, data=None):
    data = load(text) if data is None else data
    hooks = data.get("hooks", {})
    groups = hooks.get("SessionStart", []) if isinstance(hooks, dict) else None
    if not isinstance(groups, list) or HOOK_GROUP not in groups:
        raise Changed("Plane SessionStart hook changed since installation")
    groups.remove(HOOK_GROUP)
    if not groups:
        del data["hooks"]["SessionStart"]
    if not data["hooks"]:
        del data["hooks"]
    return None if created and not data else dump(data)


def approved(data):
    """Return the enabledMcpjsonServers list; refuse any other shape."""
    servers = data.setdefault("enabledMcpjsonServers", [])
    if not isinstance(servers, list):
        raise ValueError("enabledMcpjsonServers is not a list")
    return servers


def insert_settings(old):
    """Hook plus pre-approval of the plane MCP server, so Claude does not prompt for it."""
    data = load(old)
    approved(data).append("plane")
    return insert_hook(dump(data))


def remove_settings(text, created):
    """Drop the hook and the plane approval this install added; other edits stay."""
    data = load(text)
    servers = data.get("enabledMcpjsonServers")
    if isinstance(servers, list) and "plane" in servers:
        servers.remove("plane")
    if servers == []:
        del data["enabledMcpjsonServers"]
    return remove_hook(None, created, data)


def insert_codex(old):
    if "plane" in tomllib.loads(old or "").get("mcp_servers", {}):
        raise ValueError("already defines a plane MCP server")
    text = separate(old) + CODEX_BLOCK.replace("\n", eol(old))
    tomllib.loads(text)
    return text


def remove_codex(text, created):
    if not any(found in text for found in variants(CODEX_BLOCK)):
        raise Changed("plane MCP server changed since installation")
    return strip(text, CODEX_BLOCK, created)


KINDS = {
    "shim": insert_file(SHIM, also=(LEGACY_SHIM,), digests=KNOWN_SHIM_SHA256),
    "gitignore": insert_file(GITIGNORE, reuse=True),
    "include": insert_file("@AGENTS.md\n"),
    "block": (insert_block, remove_block),
    "mcp-json": (insert_server, remove_server),
    "hooks-json": (insert_hook, remove_hook),
    "settings-json": (insert_settings, remove_settings),
    "codex-toml": (insert_codex, remove_codex),
}


def read(path):
    """Text exactly as stored: UTF-8, line endings untouched. None when missing."""
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8", newline="") as source:
            return source.read()
    except UnicodeDecodeError:
        raise ValueError(f"{path.name}: not valid UTF-8; refusing to change it") from None


def fingerprint(text):
    """SHA-256 of a file's text, None for a missing file; records the state before apply."""
    return None if text is None else hashlib.sha256(text.encode()).hexdigest()


def write(path, text, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".install-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as output:
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
            if part == path and rel in ("CLAUDE.md", "AGENTS.md"):
                raise ValueError(
                    f"{rel}: is a symlink, and the installer never writes through symlinks. "
                    "Replace the link with a plain file (for CLAUDE.md, one containing "
                    "'@AGENTS.md' next to the real AGENTS.md), then run the installer again."
                )
            raise ValueError(f"{rel}: refusing symlinked installation path")
    raise ValueError(f"{rel}: outside target")


def settings_kind(target, preapprove=True):
    """Pre-approval is recorded as its own kind, so revert removes `plane` only when apply
    added it. Receipts from before it (hooks-json), installs that found it listed and installs
    that declined it never touch the list. Unreadable settings fall through; apply then
    refuses with the reason."""
    if not preapprove:
        return "hooks-json"
    try:
        text = read(guard(target, ".claude/settings.json"))
        servers = load(text).get("enabledMcpjsonServers", [])
        return "hooks-json" if isinstance(servers, list) and "plane" in servers else "settings-json"
    except ValueError:
        return "settings-json"


def plan(target, preapprove=True):
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
        (".claude/settings.json", settings_kind(target, preapprove)),
        (".codex/config.toml", "codex-toml"),
        (".codex/hooks.json", "hooks-json"),
    ]


def reverted(target, receipt):
    """Return {path: new text or None}; raise Changed when anything was edited."""
    entries = receipt.get("entries")
    if receipt.get("version") != 1 or not isinstance(entries, dict) or not entries:
        raise ValueError("Invalid installation receipt")
    result = {}
    pending = receipt.get("pending") is True
    for rel, entry in entries.items():
        if entry.get("kind") not in KINDS:
            raise ValueError("Invalid installation receipt")
        text = read(guard(target, rel))
        if pending and fingerprint(text) == entry.get("before", False):
            continue  # interrupted apply never reached this file; nothing to undo
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


def missing_values(text):
    """Names of the settings still unset or empty in local.env text (values are never shown)."""
    found = {}
    for line in text.splitlines():
        match = re.fullmatch(r"\s*(?:export\s+)?(PLANE_[A-Z_]+)=(.*?)\s*", line)
        if match:
            found[match[1]] = match[2].split(" #", 1)[0].strip().strip("\"'")
    return [name for name in SETTING_NAMES if not found.get(name)]


def apply(target, values=None, toolchain=SOURCE, preapprove=True):
    target = target.resolve(strict=True)
    receipt_path = guard(target, RECEIPT)
    resumed = None
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text())
        if receipt.get("pending") is not True:
            reverted(target, receipt)
            print("Plane integration already installed")
            outdated(target, receipt)
            return
        resumed = receipt
        print("Finishing an interrupted installation")
    changes, entries = {}, {}
    if resumed is None:
        for rel, kind in plan(target, preapprove):
            old = read(guard(target, rel))
            try:
                changes[rel] = KINDS[kind][0](old)
            except (ValueError, tomllib.TOMLDecodeError) as error:
                raise ValueError(f"{rel}: {error}") from None
            entries[rel] = {"kind": kind, "created": old is None, "before": fingerprint(old)}
            if rel == ".claude/settings.json":
                # Whether apply put `plane` in enabledMcpjsonServers; revert acts on the kind.
                entries[rel]["approval"] = (
                    "added"
                    if kind == "settings-json"
                    else "declined"
                    if not preapprove
                    else "listed"
                )
        created = [name for name in DIRECTORIES if not (target / name).exists()]
    else:
        # Files still as they were get their change; written ones must be intact.
        entries, created = resumed["entries"], resumed.get("directories", [])
        for rel, entry in entries.items():
            old = read(guard(target, rel))
            if fingerprint(old) == entry.get("before", False):
                try:
                    changes[rel] = KINDS[entry["kind"]][0](old)
                except (ValueError, tomllib.TOMLDecodeError) as error:
                    raise ValueError(f"{rel}: {error}") from None
        reverted(target, resumed)
    local = guard(target, ".plane/local.env")
    seeded = None if local.exists() else local_env(toolchain, values or {})
    # The pending receipt goes first, so a crash or OSError midway leaves something revert
    # (or a repeat apply) can finish from; the final receipt replaces it last.
    receipt = {"version": 1, "directories": created, "entries": entries}
    write(receipt_path, json.dumps({**receipt, "pending": True}, indent=2) + "\n")
    for rel, text in changes.items():
        write(target / rel, text)
    if seeded is not None:
        write(local, seeded, 0o600)
    for entry in entries.values():
        entry.pop("before", None)
    write(receipt_path, json.dumps(receipt, indent=2) + "\n")
    print("Installed Plane integration; review the diff before committing.")
    print("Next: run 'mise run setup' in the toolchain.")
    missing = missing_values(seeded if seeded is not None else local.read_text())
    if missing:
        names = ", ".join(missing[:-1]) + (" and " if len(missing) > 1 else "") + missing[-1]
        print(f"Set {names} in .plane/local.env.")
    print("Then run '.plane/plane.sh enable' and restart Codex or Claude sessions.")


def settings(target, env):
    """Return (api base, workspace, project, key); refuse before any network call."""
    local = guard(target, ".plane/local.env")
    if not local.is_file():
        raise ValueError(".plane/local.env not found; run apply first")
    values = {}
    for line in local.read_text().splitlines():
        match = re.fullmatch(r"\s*(?:export\s+)?(PLANE_[A-Z_]+)=(.*?)\s*", line)
        if match:
            values[match[1]] = match[2].split(" #", 1)[0].strip().strip("\"'")
    # Exported values win, as they do for the MCP bridge. The key is never read from files.
    for name in ("PLANE_ENABLED", "PLANE_WORKSPACE", "PLANE_PROJECT", "PLANE_API_URL"):
        if env.get(name):
            values[name] = env[name]
    if values.get("PLANE_ENABLED") != "true":
        raise ValueError("Plane integration disabled; run .plane/plane.sh enable")
    try:
        project = str(UUID(values.get("PLANE_PROJECT", "")))
    except ValueError:
        raise ValueError("PLANE_PROJECT must be set to a UUID in .plane/local.env") from None
    workspace = values.get("PLANE_WORKSPACE", "")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", workspace):
        raise ValueError("PLANE_WORKSPACE must be set in .plane/local.env")
    base = values.get("PLANE_API_URL", "")
    parts = urlsplit(base)
    local_host = parts.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        not parts.hostname
        or parts.username
        or parts.query
        or parts.fragment
        or parts.scheme not in {"https", "http"}
        or (parts.scheme == "http" and not local_host)
    ):
        raise ValueError("PLANE_API_URL (Plane REST base, HTTPS) must be set")
    key = env.get("PLANE_API_KEY", "")
    if not key or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ValueError("PLANE_API_KEY must be exported in the environment")
    return base.rstrip("/"), workspace, project, key


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib copies X-API-Key to a redirect target, even another host or plain http."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError(
            f"Plane answered with a redirect (HTTP {code}); refusing to send the API key "
            "onward. Set PLANE_API_URL to the final URL."
        )


def http_client(base, key):
    """Return request(method, path, body) -> parsed JSON using the Plane REST API."""
    opener = urllib.request.build_opener(NoRedirect)

    def request(method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data=data, method=method)  # noqa: S310
        req.add_header("X-API-Key", key)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with opener.open(req, timeout=30) as response:
            return json.load(response)

    return request


def list_labels(request, path):
    """Return every label, following Plane's cursor pagination when present."""
    labels, cursor = [], None
    for _ in range(100):
        page = request("GET", path + "?per_page=100" + (f"&cursor={cursor}" if cursor else ""))
        if isinstance(page, list):
            return labels + page
        labels += page.get("results", [])
        cursor = page.get("next_cursor")
        if not page.get("next_page_results") or not cursor:
            return labels
    raise ValueError("Label list did not end; refusing to continue")


def label(target, env=None, client=None):
    """Ensure the awaiting-decision label exists in the target's Plane project.

    client(base, key) returns request(method, path, body); tests inject a fake.
    """
    target = target.resolve(strict=True)
    receipt_path = guard(target, RECEIPT)
    if not receipt_path.is_file():
        raise ValueError("No installation receipt; run apply first")
    receipt = json.loads(receipt_path.read_text())
    if receipt.get("pending") is True:
        raise ValueError("Installation was interrupted; run apply again to finish it")
    outdated(target, receipt)
    base, workspace, project, key = settings(target, os.environ if env is None else env)
    request = (client or http_client)(base, key)
    path = f"/api/v1/workspaces/{workspace}/projects/{project}/labels/"
    found = [x for x in list_labels(request, path) if x.get("name") == LABEL["name"]]
    if found:
        existing = found[0]
        if any(existing.get(k) != v for k, v in LABEL.items()):
            print(
                f"Label {LABEL['name']} exists with a different colour or description; "
                "left unchanged. Reconcile it in Plane."
            )
            return
        print(f"Label {LABEL['name']} already exists ({existing['id']}); nothing created")
    else:
        request("POST", path, LABEL)
        found = [x for x in list_labels(request, path) if x.get("name") == LABEL["name"]]
        if len(found) != 1 or any(found[0].get(k) != v for k, v in LABEL.items()):
            raise ValueError("Label write could not be verified by rereading; check Plane")
        existing = found[0]
        print(f"Created and verified label {LABEL['name']} ({existing['id']})")
    receipt["label"] = {"name": LABEL["name"], "id": existing["id"]}
    write(receipt_path, json.dumps(receipt, indent=2) + "\n")


def outdated(target, receipt):
    """Say so when an intact install predates the current files; never rewrite anything."""
    for rel, entry in receipt["entries"].items():
        text = read(target / rel)
        if text is None:
            continue
        if entry["kind"] == "block" and BLOCK not in text and LEGACY_BLOCK in text:
            print(
                f"{rel}: Plane block predates the awaiting-decision rule. Revert, then apply "
                "again to get the current block."
            )
        if entry["kind"] == "shim" and text != SHIM:
            digest = hashlib.sha256(text.encode()).hexdigest()
            if text == LEGACY_SHIM or digest in KNOWN_SHIM_SHA256:
                print(
                    f"{rel}: shim is not the current version. Revert, then apply again "
                    "to update this installation."
                )
        if (
            rel == ".claude/settings.json"
            and entry["kind"] == "hooks-json"
            and entry.get("approval") != "declined"
        ):
            try:
                servers = load(text).get("enabledMcpjsonServers")
            except ValueError:
                print(f"{rel}: not a JSON object; cannot check the pre-approval of plane.")
                continue
            if not isinstance(servers, list) or "plane" not in servers:
                print(
                    f"{rel}: plane is not pre-approved in enabledMcpjsonServers. Revert, then "
                    "apply again to update this installation."
                )


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
    parser.add_argument("operation", choices=["apply", "revert", "label"])
    parser.add_argument("target", type=Path, help="target project checkout")
    parser.add_argument("--url", help="hosted MCP endpoint written to .plane/local.env")
    parser.add_argument("--workspace", help="workspace slug written to .plane/local.env")
    parser.add_argument("--project", help="project UUID written to .plane/local.env")
    parser.add_argument(
        "--no-preapprove",
        action="store_true",
        help="apply only: do not add plane to enabledMcpjsonServers in .claude/settings.json",
    )
    args = parser.parse_args(argv)
    try:
        if args.operation == "apply":
            values = {
                "PLANE_MCP_URL": args.url,
                "PLANE_WORKSPACE": args.workspace,
                "PLANE_PROJECT": args.project,
            }
            apply(args.target, values, preapprove=not args.no_preapprove)
        elif args.operation == "label":
            label(args.target)
        else:
            revert(args.target)
    except (ValueError, OSError) as error:
        sys.exit(str(error))
    except Exception as error:  # noqa: BLE001 - one line, never a traceback
        sys.exit(f"Installer failed ({type(error).__name__}): {error}")


if __name__ == "__main__":
    main()
