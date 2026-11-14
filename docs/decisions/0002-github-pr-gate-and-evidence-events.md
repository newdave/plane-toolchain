# 0002: GitHub events for the PR gate and evidence actions

- **Status:** Accepted
- **Date:** 2026-11-14
- **Follow-up tasks:** PTC-122 (this record and the actions), PTC-120 (core module),
  PTC-121 (entry point)

## Context

The `plane-pr-gate` and `plane-evidence` composite actions (`.github/actions/`) let a
repository tie pull requests to Plane tasks. The gate posts the `Plane task references`
commit status. Evidence writes comments that record CI and build results.

`docs/github-actions.md` and the example workflows refuse `pull_request_target` and PR
contexts, because those events can hand secrets to code a PR author controls. The gate needs
`pull_request_target`: a fork PR gets no secrets under `pull_request`, and the status must
still be posted. This record sets the one exception and the rules around it.

## Decision

### The gate may run on `pull_request_target`

The gate runs on `pull_request_target` and nothing else. It stays safe because:

- It never checks out PR code and never downloads a PR artifact.
- It reads PR data only from the GitHub API, by the event's PR number.
- It runs trusted code from the pinned action revision, installed with
  `uv sync --locked --no-dev` from that revision's own `uv.lock`.
- The caller grants only `statuses: write` and `pull-requests: read`.
- The status is posted with `github.token`, so its creator is `github-actions[bot]`.

The gate fails closed. A Plane error or a transport error posts a `failure` status with the
real cause. The gate never uses `continue-on-error`.

### Evidence runs on same-repo events only

Evidence accepts a same-repo `pull_request` and a `push` to the default branch. It refuses
fork PRs and `pull_request_target`. With no Plane key it skips with a notice and exits 0.
Callers set `continue-on-error: true` on the evidence step, so evidence never fails the
caller's job. A failure becomes a warning annotation and a job-summary line.

### The secret

The caller stores a dedicated Plane bot key as an Actions secret and passes it as the
`plane-api-key` input. The bot has Member access to one project only. Composite actions
cannot read `secrets` or `vars`, so the caller passes the key and gates the step with
`if: vars.PLANE_ENABLED == 'true'`.

### Scope of the writes

Both actions write comments, and the gate writes one commit status. Neither changes a task's
state. Neither records deployed health. A merged PR is not acceptance.

### The examples keep their refusal lists

`examples/github/*.yml` and their PR-context refusals do not change. The exception covers
these two actions only.

## Consequences

- A caller that adopts the gate accepts `pull_request_target` for that one workflow. The
  workflow must not check out PR code or run PR-controlled scripts.
- Pinning to a full commit SHA is mandatory, because the action runs with the bot key.
- A change to the trust rules above needs a new decision record.
