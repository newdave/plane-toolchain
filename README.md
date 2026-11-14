# Plane Toolchain

Plane CE MCP extensions, project integration controls and GitHub Actions examples.
No running services are changed by installing this repository.

- **CE tools:** additive work-item dependencies and project Pages (list, read, create).
- **Project controls:** `mise run plane:enable`, `plane:disable` and offline `plane:status`.
- **Agent integration:** a zsh prompt badge and shared SessionStart status for Codex and Claude.
- **Deployment helpers:** explicit owner apply, revert and check tasks for existing Docker services.
- **Project installer:** connects another checkout to the MCP bridge and makes Plane its authoritative task system.
- **Automation:** authenticated MCP transport and example workflows with explicit task IDs.

A general-purpose command-line interface is planned; it is not implemented yet. The CE tools remain necessary for capabilities missing from the evaluated third-party CLIs.

## Get started

Install [mise](https://mise.jdx.dev/), then run:

```sh
mise trust
mise install
mise run setup
mise run check
mise run plane:status
```

Follow [configuration](docs/configuration.md) before enabling remote access. Status reports the checkout switch; it does not claim the server is reachable. The prompt hook requires an activated mise zsh shell. Session hooks take effect in new client sessions.

## Documentation

- [Configuration and client integration](docs/configuration.md)
- [CE extension contract and deployment](docs/extensions.md)
- [Workspace slug rename runbook](docs/workspace-rename.md)
- [Installing into other projects](docs/project-install.md)
- [Agent workflow guide](docs/agent-workflow.md)
- [GitHub Actions integration](docs/github-actions.md)
- [Plane CLI](docs/cli.md)
- [Live CLI evaluation and recommendation](docs/cli-evaluation.md)
- [Contributing](CONTRIBUTING.md)

The project uses Plane for scope and task status. Repository docs explain the shipped tools rather than maintaining another backlog.
