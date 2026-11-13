# Installing into other projects

`scripts/install_project.py` connects another checkout to this toolchain's MCP bridge and makes Plane its authoritative task system. It uses only the Python standard library, so the target project needs no dependencies of its own.

```sh
mise run setup                                        # in this checkout, once
mise run owner:project:apply -- /path/to/project
mise run owner:project:label -- /path/to/project      # optional, needs PLANE_API_KEY
mise run owner:project:revert -- /path/to/project
```

Without mise, run `python3 /path/to/plane-toolchain/scripts/install_project.py apply /path/to/project`. Apply also accepts `--url`, `--workspace` and `--project` to seed the non-secret values. It never accepts the API key. When apply finishes it lists only the `PLANE_MCP_URL`, `PLANE_WORKSPACE` and `PLANE_PROJECT` values still missing from `.plane/local.env`, then points at `.plane/plane.sh enable`. The shim, the bridge and the owner tasks run `uv` with `VIRTUAL_ENV` cleared, so an active unrelated virtualenv gives no uv warning.

## What apply changes

| Path | Change |
| --- | --- |
| `.plane/plane.sh` | Shim with `mcp`, `session`, `status`, `enable` and `disable` commands |
| `.plane/.gitignore` | Ignores local values, encrypted keys and switch state |
| `.plane/local.env` | Created if absent: toolchain path, `PLANE_ENABLED=false` and seeded values; mode 0600; ignored |
| `.plane/install.json` | Receipt used by revert; commit it with the rest |
| `AGENTS.md` | Appends a marked block making Plane authoritative for tasks, status and planning Pages |
| `CLAUDE.md` | Created as `@AGENTS.md` if absent; gets the same block if it exists without that include |
| `.mcp.json`, `.codex/config.toml` | Add a `plane` server that runs `.plane/plane.sh mcp` |
| `.claude/settings.json`, `.codex/hooks.json` | Add a SessionStart hook that runs `.plane/plane.sh session` |
| `.claude/settings.json` | Also adds `plane` to `enabledMcpjsonServers`, so Claude connects without an approval prompt |

Apply refuses, without writing anything, when a target already has a `plane` MCP server, a Plane block, a `.plane/plane.sh`, a symlinked path or a file it cannot parse. JSON files it edits are rewritten with two-space indentation. Repeating apply on an intact installation does nothing, except that it names an outdated shim or a missing `plane` pre-approval (receipts from before PTC-55) and changes no files. To upgrade an existing installation, revert, then apply again. It also refuses when `.claude/settings.json` is not valid JSON or its `enabledMcpjsonServers` is not a list.

`plane` is appended to `enabledMcpjsonServers` once; other keys and servers are kept. If `plane` was already listed, the receipt records the file with the hook only, and revert never removes your entry. Otherwise revert removes only the `plane` entry (and the hook), so entries or keys you added to the file after apply survive, and a file the installer created is deleted when nothing else is left. Receipts from before this change have no such record; revert leaves the list alone for them.

Revert removes only artefacts that are still as installed. JSON files (`.mcp.json`, hook files, the receipt) are parsed and only the installed entries are removed, so a prettier reformat does not block revert. The shim is accepted byte for byte, and also as shfmt v3.14.1 formats it with `-i 0` (tabs, the defaults), `-i 2`, `-i 2 -ci` or `-i 4`, for the current shim and every earlier version. Any other shfmt style, and any other edit, is refused: run `git checkout` (or reconcile by hand) on `.plane/plane.sh` before revert. Revert restores files the installer created or appended to. If anything was edited, it refuses and changes nothing; reconcile the file by hand first. Revert keeps ignored local files under `.plane/`, together with the `.gitignore` that protects them, and lists them.

## Provisioning the awaiting-decision label

Apply never calls Plane. The label is a separate, explicit owner step:

```sh
export PLANE_API_KEY=...                              # environment only; never an argument
mise run owner:project:label -- /path/to/project
```

It reads `PLANE_ENABLED`, `PLANE_WORKSPACE`, `PLANE_PROJECT` and `PLANE_API_URL` (the Plane REST base, for example `https://plane.example.com`) from the target's `.plane/local.env`; exported values take precedence. It refuses, before any network call, when the installation receipt is missing, Plane is disabled, a value is missing or invalid, or `PLANE_API_KEY` is not exported. The key is sent only as the `X-API-Key` header and is never printed or written.

The step lists the project's labels over `/api/v1/workspaces/<workspace>/projects/<project>/labels/` and creates `awaiting-decision` (colour `#EF4444`) only if no label of that name exists, then rereads the list to verify it and records the label id under `label` in `.plane/install.json`. Repeating it creates nothing. A same-named label with a different colour or description is reported and left unchanged, with nothing recorded.

Revert never deletes the label, because work items may still use it. It drops the receipt, including the label id, and the rule from the `AGENTS.md` block. Installations made before this rule existed keep their older block unchanged: revert still restores them exactly, and repeating apply or running the label step reports the block as outdated. To get the current block, revert and apply again.

## Connection values

The shim sets `PLANE_CONFIG_ROOT` to the target project. The bridge then reads `PLANE_ENABLED`, `PLANE_MCP_URL`, `PLANE_WORKSPACE` and `PLANE_PROJECT` only from the target's `.plane/local.env`; exported values still take precedence. It does not read the target's `.env` or `config/` files. `PLANE_API_KEY` comes from the environment or SOPS-encrypted `.plane/plane.enc.env`, as described in [configuration](configuration.md).

`PLANE_TOOLCHAIN` in `.plane/local.env`, or exported, locates this checkout. Each developer's machine sets its own path, so committed files contain no absolute paths. When mise is installed, the shim runs the bridge through `mise -C <toolchain> exec` to use the pinned Node.

After apply, set the connection values, run `.plane/plane.sh enable` and restart Codex or Claude sessions. `.plane/plane.sh status` reports the local switch offline; it does not contact Plane.

In a linked git worktree of the project, `.plane/plane.sh`, the session hook and the bridge fall back to the main checkout's git-ignored `.plane/local.env` and `.plane/plane.enc.env`, found through `git rev-parse --git-common-dir`. The worktree's own `.plane/local.env` overrides the main one per key, and a worktree `.plane/plane.enc.env` wins when present. Files are read in place and never copied; symlinks and a common dir not named `.git` get no fallback.
