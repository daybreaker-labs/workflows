# workflows

Public reusable GitHub Actions workflows. Callers pin full commit SHAs and pass secrets explicitly. Keep credentials and private rollout notes in their owning repositories.

## Organization context

Read local guidance first. Consult [Sunrise's repository map](https://github.com/daybreaker-labs/sunrise/blob/main/docs/organization/README.md) and [shared agent guidance](https://github.com/daybreaker-labs/sunrise/blob/main/docs/organization/agents.md) for cross-project architecture and organization conventions. Local implementation guidance takes precedence.

## Work tracking

Track Daybreaker work in Linear. Before creating, planning, implementing or completing a task, read the [work-tracking policy](https://github.com/daybreaker-labs/sunrise/blob/main/docs/organization/work-tracking.md). Before publishing an issue or changing its scope, require a [primary owning-repository label](https://github.com/daybreaker-labs/sunrise/blob/main/docs/organization/work-tracking.md#repository-labels). Link the Linear issue to the worktree and PR, and record verification and remaining blockers in the issue. GitHub hosts code, PRs and Renovate Dependency Dashboards.

## Checks and changes

Run `scripts/check` before every push: it runs the `check` job of `checks.yml` (program tests, actionlint, zizmor), and `.githooks/pre-push` runs it once `git config core.hooksPath .githooks` is set (the `orca.yaml` setup hook sets it for new worktrees). README.md lists the checks. Changes to a workflow must keep its offline release-gate and promotion tests passing. Use Conventional Commits, draft PRs and reviewed SHA pins. Check public content for secrets and private infrastructure before pushing. Repository settings and rulesets belong to daybreaker-stack Terraform; this repository's own CI uses GitHub-hosted runners.
