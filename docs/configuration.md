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

The bridge reads the non-secret values (`PLANE_ENABLED`, `PLANE_MCP_URL`, `PLANE_WORKSPACE`, `PLANE_PROJECT`) from ignored `config/local.env` as literal text; exported values take precedence. If `PLANE_API_KEY` is not exported, the bridge decrypts it from optional SOPS-encrypted `config/plane.enc.env` and exports only that value to `mcp-remote`. If sops fails (missing binary, wrong or absent age key), the bridge stops with a message naming the file and `SOPS_AGE_KEY_FILE` instead of reporting a missing key. A symlinked `plane.enc.env` is refused, in a main checkout, an installed project and a worktree alike; an exported `PLANE_API_KEY` skips the file. Create it in the SOPS editor, adding a `PLANE_API_KEY=` line: `sops --age "$(age-keygen -y "$SOPS_AGE_KEY_FILE")" config/plane.enc.env`.

In a git worktree, the git-ignored `config/local.env`, `config/plane.enc.env` and `tools/mcp-remote/node_modules` are not checked out. In a linked worktree, the bridge and the session status hook use the worktree's own `config/plane.enc.env` and `mcp-remote` when present, otherwise the main checkout's (found through `git rev-parse --git-common-dir`). `config/local.env` is merged per key: the main checkout's values first, the worktree's own file overriding them; a worktree with only a legacy `.deploy/env` keeps its old behaviour. There is no fallback without git, in a main checkout, or in a copy nested inside an unrelated repository. The main checkout's files are read in place, never copied. A symlinked main file, or one whose real path leaves the main checkout, gets no fallback, and neither does a common dir not named `.git`. The main checkout's `mcp-remote` is reused only when both `tools/mcp-remote/package-lock.json` files have the same SHA-256 and the bin's real path stays inside the main `node_modules`; otherwise the bridge fails with the `mise run setup` hint. Every `mcp-remote` the bridge starts, local or reused, must also be the version `package-lock.json` pins (read from `node_modules/mcp-remote/package.json`); a stale or hand-edited tree fails with the same hint. The worktree fallback needs git 2.31 or newer (`--path-format=absolute`); on an older git the bridge says so on stderr and uses no main checkout files. Writes such as `plane:enable` always target the current checkout's `config/local.env`.

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

With mise activated in zsh, entering this project installs `[Plane:enabled]`, `[Plane:disabled]` or `[Plane:invalid]` in the prompt. The badge refreshes before each prompt, but the status comes from a cache: python3 runs again only when `PLANE_ENABLED`, `config/defaults.env`, `.env`, `config/local.env`, `.deploy/env` or `.plane/local.env` changed (modification time or size), or after 30 seconds. Without the zsh `zstat` and `datetime` modules it runs every prompt. Leaving the project removes it and restores the previous prompt. Bash, fish and non-interactive shells can use `mise run plane:status`; they do not get a prompt badge.

## Blocked tasks

An agent blocked on a human decision posts a comment with the exact question and options, adds the project label `awaiting-decision` and moves the task out of In Progress unless other work on it continues. It rereads both writes to confirm them. The label means the agent is blocked until a human answers the question in the latest comment. The agent removes it once the answer arrives and then resumes the task.

## Local project data

Keep real endpoints and resource identities in ignored local configuration or your secret manager. The public repository contains no deployment destinations, real task IDs or credentials. Plane holds planning and task scope; the public code is usable with any compatible CE instance.
