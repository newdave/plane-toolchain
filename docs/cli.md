# Plane CLI

`plane` is a thin command-line interface over the Plane MCP server. It lists and updates tasks, adds comments, creates Pages and adds task relations. Each command maps to existing MCP tools. It has no REST client of its own, so the server's permissions and the CE extensions apply.

## Install and run

No install step beyond `mise run setup`. Run it through mise:

```sh
mise run plane:cli -- task list
```

The same entry point is `python -m plane_toolchain.cli`. Arguments after `--` go to the CLI.

## Configuration

The CLI reads the values in [configuration](configuration.md):

| Variable | Purpose |
| --- | --- |
| `PLANE_ENABLED` | Literal `true` to permit remote access |
| `PLANE_MCP_URL` | HTTPS MCP endpoint, for example `https://plane.example.com/http/api-key/mcp` |
| `PLANE_WORKSPACE` | Workspace slug, for example `example-workspace` |
| `PLANE_PROJECT` | Default project UUID, for example `00000000-0000-0000-0000-000000000001` |
| `PLANE_API_KEY` | Secret API key |

Exported values win. If a non-secret value is not exported, the CLI falls back to `config/local.env`. `PLANE_ENABLED` falls back to the `plane:enable` switch. The API key comes only from the environment. It is never read from arguments or files, and never printed.

Every command accepts `--project UUID` to override `PLANE_PROJECT`. It must be a UUID.

## Task references

A task is a UUID or an identifier such as `PTC-12`. Identifiers are case-insensitive. Anything else is a usage error before any connection opens.

## Commands

Tasks:

```sh
mise run plane:cli -- task list --per-page 20
mise run plane:cli -- task show PTC-12
mise run plane:cli -- task update PTC-12 --priority high --name "New title"
mise run plane:cli -- task update PTC-12 --state 00000000-0000-0000-0000-000000000001
mise run plane:cli -- task update PTC-12 --file description.html
```

`task update` takes `--name`, `--state` (a state UUID), `--priority` (`urgent`, `high`, `medium`, `low`, `none`) and a body from `--html` or `--file`. It needs at least one. `task list` takes `--per-page` and `--cursor`. It never sends a PQL filter, because Plane CE rejects PQL.

Comments (`comment list` takes `--per-page` and `--cursor`):

```sh
mise run plane:cli -- comment list PTC-12
mise run plane:cli -- comment add PTC-12 --html "<p>Deployed.</p>"
```

Pages:

```sh
mise run plane:cli -- page list
mise run plane:cli -- page show 00000000-0000-0000-0000-000000000001
mise run plane:cli -- page create --name "Plan" --file plan.html
mise run plane:cli -- page create --name "Notes" --html "<p>Hi</p>" --public
```

Pages are private by default. `--public` makes a Page visible to the whole project.

Relations:

```sh
mise run plane:cli -- relation list PTC-12
mise run plane:cli -- relation add PTC-12 PTC-3 PTC-4
mise run plane:cli -- relation add PTC-12 PTC-3 --type blocking
```

`--type` is one of `blocking`, `blocked_by` (default), `start_before`, `start_after`, `finish_before` or `finish_after`. The direction is from the first task's point of view. The tasks must be distinct, and a task cannot relate to itself.

Body options: `--html TEXT` or `--file PATH`. Use `--file -` to read standard input. Files must be UTF-8. An empty body is a usage error.

## Output

On success the CLI prints one JSON document to standard output, with sorted keys and two-space indent. Nothing else goes to standard output. Errors are one line on standard error, prefixed `plane: `. The CLI never prints tracebacks, response bodies, headers or the key.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 2 | Usage error |
| 3 | Configuration error, including Plane disabled |
| 4 | MCP, tool or transport failure, including a missing server tool |
| 5 | Write not confirmed by readback |
| 130 | Interrupted |

## Readback

`task update`, `comment add`, `page create` and `relation add` reread the remote state after writing. If the reread does not match, the CLI exits 5 with `plane: write not confirmed by readback; check the remote state`. The write may still have happened. Check before you try again.

## Limits

- No Page editing. `ce_page` update is not exposed.
- No relation deletion.
- Relation tasks must all be in one project.
- No retries. After a failed or uncertain write, check the remote state first.
- Task comments and Pages are written as HTML you supply. The CLI does not convert Markdown.
- `page` and `relation` need the CE extensions deployed. If the server lacks them, the CLI exits 4 with `Required MCP tool unavailable`. See [extensions](extensions.md).
