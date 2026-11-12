# Security policy

## Supported versions

Only the latest commit on `main` receives security fixes. Once releases exist, the latest release
will also be supported.

## Reporting a vulnerability

Please do not open a public issue for a vulnerability.

Report it privately with GitHub private vulnerability reporting: open the **Security** tab and
choose **Report a vulnerability**. This is the same as a security advisory.

Include:

- The affected file or tool, and the commit or version you tested.
- Steps to reproduce, or a proof of concept.
- The impact you expect.

Never include live credentials, API keys, SSH keys or private endpoints in a report. Describe the
problem and use placeholder values instead.

## Scope

In scope:

- The CE MCP tools (`src/plane_toolchain/ce_tools.py`).
- The Pages backend extension (`src/plane_toolchain/pages_backend.py`).
- The MCP stdio bridge (`scripts/plane-mcp.sh`).
- Deployment helpers (`src/plane_toolchain/pages_deploy.py`, `src/plane_toolchain/mcp_deploy.py`).
- GitHub Actions examples (`examples/github/`).

Out of scope:

- Plane itself. Report those issues upstream at [makeplane/plane](https://github.com/makeplane/plane).
- Third-party services and tools that this project calls.

## Response

This project has a single maintainer. I aim to acknowledge reports within a week and to keep you
updated as the fix progresses. Responses are best effort, not a guaranteed service level.

Thanks for helping keep this project and its users safe.
