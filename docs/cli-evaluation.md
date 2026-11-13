# Live CLI evaluation and recommendation

Both CLIs were tested on 12 November 2026 against an authenticated Plane CE v1.4.3 deployment. Compatibility with custom endpoint names was not required: the comparison asked whether the CLIs could provide equivalent capabilities and let us remove the extensions.

| Capability | cpatrickalves/plane-cli 0.6.0 | plane-cli-requiem 0.3.3 |
| --- | --- | --- |
| Projects and work-item reads | Passed; 622 work items paginated | Passed in diagnostic build; 622 work items paginated |
| Modules | 50 returned; exhaustive pagination not established | 50 returned; exhaustive pagination not established |
| Comment creation | Passed; independently reread through MCP | Passed in diagnostic build; independently reread through MCP |
| Intake | Read passed with an empty queue | No command |
| Pages | Standard list, known-page read and create returned HTTP 404 | No command |
| Relations | No command | List and tree returned HTTP 401 from the internal endpoint |
| Project-local operation | Evaluation isolated config/cache through a harness | Stock CLI required a global workspace registry despite environment and project config |

## What was tested

The Python release was `planecli` 0.6.0, release commit `ba300d49413176df0e9abcf507cdfcfe58adf3a2`, with `plane-sdk` 0.3.2 and Python 3.11 or newer. The harness relocated config/cache storage and paced requests; it did not change authentication or API routes.

Requiem 0.3.3 was installed from its published crate with a locked build. Stock operation stopped at the required global registry: global user configuration was outside the authorised scope. A diagnostic rebuild changed only configuration-directory selection to an isolated directory. Functional results above belong to that rebuild, not an unmodified installation. A dummy registry key and a real environment-only key demonstrated that tested requests used the environment credential. API and authentication code remained unchanged.

The configured MCP server could read Pages and relations where the CLI requests failed with the same credentials. Persisted test comments were reread through that server. Write evidence remains in the original private Plane evaluation task; deployment identities and task contents are excluded from this public report.

## Decision

Neither CLI replaces the CE extensions. Python's CLI is the stronger optional standalone fallback for supported public API operations. Requiem's required global registry and unavailable CE relations make it a poorer fit for project-scoped operation.

For this toolchain, build a thin MCP client: translate command arguments into existing tools, preserve their permissions and reuse the bounded transport. Start with task list/show/update, comments, Pages list/read/create and relations list/add, with JSON output and predictable exit codes. Keep business logic in the MCP server and keep the CE extensions beneath it. Existing Page editing remains unsupported.

The general-purpose CLI is described in [CLI](cli.md). The GitHub automation driver demonstrates MCP calls but is separate from that CLI.

## Upstream sources

- [cpatrickalves/plane-cli](https://github.com/cpatrickalves/plane-cli)
- [plane-cli-requiem crate](https://crates.io/crates/plane-cli-requiem)
- [Official Plane MCP server](https://github.com/makeplane/plane-mcp-server)
