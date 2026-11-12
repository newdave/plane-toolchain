# Contributing

Run `mise run setup` and `mise run check` before committing. Work on a branch and submit a pull request. CI must pass before merging.

Keep changes project-scoped. Never commit credentials, deployment endpoints, private task contents or global agent configuration. Use fictional example identities. Keep hooks enabled.

Plane owns task scope, acceptance, dependencies and status. Keep task status aligned with actual work; reread updates through MCP. Planning narratives belong in project Pages. Repository documentation describes shipped behaviour rather than duplicating the backlog.

The CE backend adapter is version-specific. Pure unit tests cover validation, normalisation, transport, local controls and deployment payload preservation; they do not replace a staged integration test against a new Plane image. Do not run host deployment tasks as part of ordinary development or CI.

The Actions examples remain under `examples/github/` so they are not active by default. Keep credentials out of pull-request code and use immutable toolchain revisions.
