# Contributing

Run `mise run setup` and `mise run check` before committing. Work on a branch and submit a pull request. CI must pass before merging.

Install the git hooks once per clone with `pre-commit install --hook-type pre-commit --hook-type commit-msg`. A global git template hook usually installs only the pre-commit stage, so without this command the commit-msg check does not run. The hooks run ruff, actionlint, shellcheck, YAML/JSON and private-key checks on commit, and reject AI attribution trailers (`Co-authored-by:` or "Generated with/by" naming an AI tool) in commit messages; human co-authors are fine. Tool versions come from `uv.lock` and `mise.toml`. Pytest stays in `mise run check` and CI.

The default branch is protected by the ruleset in `.github/rulesets/main.json`: pull requests only, the `check` status must pass on an up-to-date branch, linear history (rebase or squash), and no force-push or deletion. Pull requests may use auto-merge, and merged branches are deleted. The repository owner maintains it with `gh`:

```sh
gh api repos/OWNER/REPO/rulesets                                      # find the ruleset id
gh api -X PUT repos/OWNER/REPO/rulesets/ID --input .github/rulesets/main.json  # update
gh api -X DELETE repos/OWNER/REPO/rulesets/ID                         # roll back
```

Keep changes project-scoped. Never commit credentials, deployment endpoints, private task contents or global agent configuration. Use fictional example identities. Keep hooks enabled.

Plane owns task scope, acceptance, dependencies and status. Keep task status aligned with actual work; reread updates through MCP. Planning narratives belong in project Pages. Repository documentation describes shipped behaviour rather than duplicating the backlog.

The CE backend adapter is version-specific. Pure unit tests cover validation, normalisation, transport, local controls and deployment payload preservation; they do not replace a staged integration test against a new Plane image. Do not run host deployment tasks as part of ordinary development or CI.

The Actions examples remain under `examples/github/` so they are not active by default. Keep credentials out of pull-request code and use immutable toolchain revisions.

## Releases and the release dry run

The `release` workflow runs when a `v*.*.*` tag is pushed. It requires the tag to be on the default branch and to match the `pyproject.toml` version, requires a `CHANGELOG.md` section for the version, and requires a successful `check` run from GitHub Actions on the tagged commit before it creates the release. Only the gate and release steps hold `GH_TOKEN`.

Dry run on a scratch fork (never on the real repository):

1. Fork, then enable Actions. Merge a version bump and its CHANGELOG section to the fork's default branch and wait for `check` to pass.
2. Push the tag `vX.Y.Z`. Expect the release to be created with the notes and a `Pin:` line.
3. Negative test: tag a commit whose `check` is red, still running or absent (for example a commit pushed with CI skipped). Expect the gate step to fail with "no successful 'check' run" and no release.
4. If a tag is pushed before CI finishes, re-run the workflow once `check` has passed.

Run a subset of tests with `uv run pytest -q tests/test_x.py`. `mise run test` (and so `mise run check`) enforces the 81% coverage floor over the whole suite, so a subset run through it fails the floor.
