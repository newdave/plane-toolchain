# Configuration and client integration

All configuration applies to this checkout. No global Codex, Claude or shell files are changed.

## Connection values

Inject these values into the process environment using your secret manager or CI settings:

| Variable | Purpose |
| --- | --- |
| `PLANE_ENABLED` | Literal `true` to permit remote access; default is disabled |
| `PLANE_MCP_URL` | HTTPS streamable HTTP endpoint, typically `/http/api-key/mcp` |
| `PLANE_WORKSPACE` | Existing workspace slug |
| `PLANE_PROJECT` | Project UUID; resolve it with the authenticated MCP project tool |
| `PLANE_API_KEY` | Secret API key for a user with the required project access |

The client needs the hosted MCP endpoint, not the Plane web URL. HTTP is allowed only for loopback development. Keys are never accepted in URL query strings or command arguments.

The bridge reads the non-secret values (`PLANE_ENABLED`, `PLANE_MCP_URL`, `PLANE_WORKSPACE`, `PLANE_PROJECT`) from ignored `config/local.env` as literal text; exported values take precedence. If `PLANE_API_KEY` is not exported, the bridge decrypts it from optional SOPS-encrypted `config/plane.enc.env` and exports only that value to `mcp-remote`. Create it in the SOPS editor, adding a `PLANE_API_KEY=` line: `sops --age "$(age-keygen -y "$SOPS_AGE_KEY_FILE")" config/plane.enc.env`.

In a git worktree, the git-ignored `config/local.env`, `config/plane.enc.env` and `tools/mcp-remote/node_modules` are not checked out. The bridge and the session status hook use the worktree's own copy when present, otherwise the main checkout's (found through `git rev-parse --git-common-dir`). Outside git there is no fallback. Writes such as `plane:enable` always target the current checkout's `config/local.env`. The root `.worktreeinclude` lists the two config files so Claude Code copies them into worktrees it creates.

`.env.example` contains fictional non-secret examples. It is not automatically sourced. Do not copy a real key into it, `config/local.env`, a task argument or a shell command. Use your existing encrypted environment loader. This repository does not create credentials or read another project's secret files.

## Enable and disable

```sh
mise run plane:enable
mise run plane:status
mise run plane:disable
```

These tasks change only `PLANE_ENABLED` in ignored `config/local.env`. They preserve unrelated values. `owner:plane:apply` remembers the first baseline; `owner:plane:revert` restores it. Explicit `plane:disable` always disables the local switch. An exported `PLANE_ENABLED` takes precedence over the file; clear that override when using local controls.

`plane:status` reads literal values without executing shell text. It reports enabled, disabled or invalid. It does not test credentials or contact Plane.

## Codex and Claude

The project includes `.codex/config.toml` and `.mcp.json`, both launching `scripts/plane-mcp.sh`. The bridge validates settings, then runs `mcp-remote` from the locked tree in `tools/mcp-remote` (exact version, integrity-checked `package-lock.json`, installed by `npm ci --ignore-scripts` under the pinned Node) to translate stdio into hosted HTTP. The bridge never uses `npx` or installs at runtime, and exits with a hint if the tree is missing. The bin's `#!/usr/bin/env node` uses the first `node` on PATH, so launch the client via mise to get the pinned Node. Run `mise run setup` first and launch the client from this repository with the secret environment injected.

Both clients have project SessionStart hooks calling the same offline status reporter. Restart a session to load new hooks or MCP configuration. The bridge refuses remote access while disabled; existing client sessions may retain an established connection until restarted.

## Shell prompt

With mise activated in zsh, entering this project installs `[Plane:enabled]`, `[Plane:disabled]` or `[Plane:invalid]` in the prompt. The badge refreshes before each prompt. Leaving the project removes it and restores the previous prompt. Bash, fish and non-interactive shells can use `mise run plane:status`; they do not get a prompt badge.

## Local project data

Keep real endpoints and resource identities in ignored local configuration or your secret manager. The public repository contains no deployment destinations, real task IDs or credentials. Plane holds planning and task scope; the public code is usable with any compatible CE instance.
