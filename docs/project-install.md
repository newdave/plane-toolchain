# Installing into other projects

`scripts/install_project.py` connects another checkout to this toolchain's MCP bridge and makes Plane its authoritative task system. It uses only the Python standard library, so the target project needs no dependencies of its own.

```sh
mise run setup                                        # in this checkout, once
mise run owner:project:apply -- /path/to/project
mise run owner:project:revert -- /path/to/project
```

Without mise, run `python3 /path/to/plane-toolchain/scripts/install_project.py apply /path/to/project`. Apply also accepts `--url`, `--workspace` and `--project` to seed the non-secret values. It never accepts the API key.

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

Apply refuses, without writing anything, when a target already has a `plane` MCP server, a Plane block, a `.plane/plane.sh`, a symlinked path or a file it cannot parse. JSON files it edits are rewritten with two-space indentation. Repeating apply on an intact installation does nothing.

Revert removes only artefacts that are still exactly as installed, and restores files the installer created or appended to. If anything was edited, it refuses and changes nothing; reconcile the file by hand first. Revert keeps ignored local files under `.plane/`, together with the `.gitignore` that protects them, and lists them.

## Connection values

The shim sets `PLANE_CONFIG_ROOT` to the target project. The bridge then reads `PLANE_ENABLED`, `PLANE_MCP_URL`, `PLANE_WORKSPACE` and `PLANE_PROJECT` only from the target's `.plane/local.env`; exported values still take precedence. It does not read the target's `.env` or `config/` files. `PLANE_API_KEY` comes from the environment or SOPS-encrypted `.plane/plane.enc.env`, as described in [configuration](configuration.md).

`PLANE_TOOLCHAIN` in `.plane/local.env`, or exported, locates this checkout. Each developer's machine sets its own path, so committed files contain no absolute paths. When mise is installed, the shim runs the bridge through `mise -C <toolchain> exec` to use the pinned Node.

After apply, set the connection values, run `.plane/plane.sh enable` and restart Codex or Claude sessions. `.plane/plane.sh status` reports the local switch offline; it does not contact Plane.

In a linked git worktree of the project, `.plane/plane.sh`, the session hook and the bridge fall back to the main checkout's git-ignored `.plane/local.env` and `.plane/plane.enc.env`, found through `git rev-parse --git-common-dir`. The worktree's own `.plane/local.env` overrides the main one per key, and a worktree `.plane/plane.enc.env` wins when present. Files are read in place and never copied; symlinks and a common dir not named `.git` get no fallback.
