# GitHub Actions integration

The examples call the hosted Plane MCP server with the same authentication and permission checks as interactive clients. They are stored outside `.github/workflows/` and do not run automatically.

| Example | Trigger | Plane effect |
| --- | --- | --- |
| `plane-check.yml` | Manual dispatch | Retrieve the configured project; no writes |
| `plane-comment.yml` | Manual dispatch on the default branch | Append a workflow-run link to an explicit work item |
| `plane-complete.yml` | Reusable workflow called after verified deployment | Append evidence, then set an explicit completed state |

The examples intentionally do not mark a task Done when a PR merges. A release may contain multiple tasks, and a merged change may not yet be deployed. Adapt task selection to your release process rather than guessing from PR titles.

## Configure a consuming repository

Set these GitHub Actions repository variables through your normal repository configuration process:

| Variable | Value |
| --- | --- |
| `PLANE_ENABLED` | `true` to allow the example jobs |
| `PLANE_TOOLCHAIN_REF` | Reviewed full 40-character commit SHA from this repository |
| `PLANE_MCP_URL` | Hosted HTTPS streamable HTTP endpoint |
| `PLANE_WORKSPACE` | Workspace slug |
| `PLANE_PROJECT` | Project UUID |
| `PLANE_DONE_STATE_ID` | UUID of that project's completed state; completion example only |

Store `PLANE_API_KEY` as an Actions secret. Use a dedicated Plane identity with only the project access your automation needs. Ensure GitHub-hosted runners can reach the endpoint, or choose a trusted runner with network access. No credentials or repository variables have been configured by publishing these examples.

The workflows validate the toolchain revision before checkout and fetch that exact revision into `.plane-toolchain`. Dependencies come from its lockfile. They do not execute pull-request code. They grant only `contents: read` on GitHub; Plane writes use the separate Plane key.

## Install or remove the examples

From this checkout, install examples into a target checkout using paired owner tasks:

```sh
mise run owner:github-examples:apply -- /path/to/your/checkout
mise run owner:github-examples:revert -- /path/to/your/checkout
```

Installation refuses to overwrite existing files. Removal checks the installation receipt and refuses to remove changed files. Review and commit the resulting workflow files in the consuming repository. No example is installed by setup.

After installing, run **Check Plane MCP** from the Actions tab. Then run **Record Plane evidence** from the default branch with a work-item UUID in the configured project. Each manual invocation appends a comment, including repeated invocations. Failed writes are not automatically retried; check Plane before running again.

## Call completion after deployment

Add a caller job only after your existing deploy job verifies both the expected deployed revision and service health. This fragment assumes your deploy job emits a `verified` output set to the string `true` only after those checks pass:

```yaml
jobs:
  # Your existing deploy job belongs here and must produce outputs.verified.
  plane:
    needs: deploy
    if: needs.deploy.result == 'success' && needs.deploy.outputs.verified == 'true'
    uses: ./.github/workflows/plane-complete.yml
    with:
      workitem_id: ${{ vars.RELEASE_WORKITEM_ID }}
      deployment_verified: true
    secrets:
      PLANE_API_KEY: ${{ secrets.PLANE_API_KEY }}
```

`RELEASE_WORKITEM_ID` identifies one approved release task. For multiple tasks, explicitly map the verified release scope to task UUIDs before adapting the example. The boolean is a caller assertion; the reusable workflow cannot prove your application's deployment independently.

The driver retrieves the task in the configured project, verifies the target state belongs to the completed group, posts and rereads evidence, then updates and rereads status. A failed comment verification stops before the state change. An uncertain remote write fails the job without retries. GitHub concurrency limits overlapping invocations for a task; it is not a transaction with Plane and does not coordinate other clients.

## Trust boundaries

Never pass Plane secrets to fork PR code. Do not add a PR head checkout, untrusted artifacts or shell interpolation of PR titles to these workflows. Write examples reject PR event contexts and require the default branch. If you add richer PR evidence, read GitHub metadata from trusted code and keep acceptance/status policy explicit. Preserve active work owned by another chat or person.
