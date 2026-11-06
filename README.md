# workflows

Shared GitHub Actions workflows for Daybreaker repositories. Public refs let caller-scoped tokens resolve workflow revisions during online zizmor audits.

`.github/workflows/product-image.yml` builds native arm64 images for
Daybreaker product repositories. Call it from `daybreaker-labs/workflows/.github/workflows/product-image.yml` by full commit SHA, with an explicit secret map and a matching version comment when tagged. [ADR 0006](https://github.com/daybreaker-labs/sunrise/blob/main/docs/adr/0006-organization-ci-baseline.md)
defines the shared pipeline; [FOUND-257](https://linear.app/daybreaker-labs/issue/FOUND-257)
introduces the first caller, `explicavet-api`.

## Caller contract

- `image-name` equals the caller's repository name under `daybreaker-labs`.
  Terraform must precreate that name in `shared_registry_images`.
- `publish: false` builds without registry or deploy-App credentials. Every
  external Dockerfile base must carry a SHA-256 digest. The resulting image
  must run as non-root. The workflow adds source, revision, version and creation
  time OCI labels and checks the native architecture.
  Go callers derive the exact `GO_VERSION` build argument from `go.mod`;
  all callers receive the source commit as `VCS_REF` and `GIT_COMMIT`,
  `VERSION` (`X.Y.Z` for stable `vX.Y.Z` tags, otherwise `dev`), and
  `BUILD_TIME` (UTC RFC 3339, shared with the creation label).
- `publish: true` accepts a `vX.Y.Z` push, or a manual dispatch on `main` or
  `develop` (and a `develop` push with the staging opt-in below). Stable releases
  must already belong to fetched `origin/main`.
  Branch dispatches publish `sha-<full commit>` for qualification.
- `promote: true` requires a stable release unless the staging opt-in below is
  set. By default it opens a **draft** GitOps PR; a maintainer reviews and merges it to deploy. Qualification images
  cannot promote without that opt-in.
- Required caller configuration: `CI_DOCKER_RUNNER` for native ARM builds,
  `CI_RUNNER` for promotion, `OCIR_USERNAME` and secret `OCIR_AUTH_TOKEN`.
  Promotion also needs explicit `DEPLOY_APP_ID` and `DEPLOY_APP_PRIVATE_KEY`
  secrets for a GitHub App installed on `daybreaker-gitops` with Contents write
  and Pull requests write. No `secrets: inherit`.
- The promotion contract requires exactly one image pin in
  `components/<image-name>/base/deployment.yaml`. The boolean input
  `migration-job` defaults to `true` and also requires exactly one pin in
  `components/<image-name>/base/migrate-job.yaml`. Set `migration-job: false`
  for products that migrate at startup; promotion then reads and changes only
  the deployment pin. All selected pins change in one Git commit and the PR
  stays in draft. Missing or duplicate selected pins stop promotion before writes.
- The boolean input `update-version-label` defaults to `false`. When `true`,
  promotion also sets `app.kubernetes.io/version` in `deployment.yaml` to the
  stable release tag, keeping the leading `v` (for example `v1.1.3`), in the
  same commit as the pin. The key must exist as a block-style label under
  `spec.template.metadata.labels`; a `metadata.labels` entry on the Deployment
  is updated when present. Only that key changes, `migrate-job.yaml` is never
  edited, and a missing, empty, flow-style, duplicate or misplaced label, or a
  file with several YAML documents, stops promotion before writes instead of
  being created. Callers that derive `service.version` from the pod label set it
  to keep that value in step with the promoted image.

The optional string input `promotion-overlay` defaults to an empty string, retaining
that release contract. Set it to `daybreaker-02` only for `opensourceradar-api`
on `develop`, with `migration-job: false` and `update-version-label: true`.
This allows a push or manual qualification dispatch to publish and promote a
`sha-<full commit>` image. Other overlays, products and refs fail before building.
The caller must gate promotion on its source lint and test checks.

Staging promotion changes only the digest in
`components/opensourceradar-api/overlays/daybreaker-02/kustomization.yaml` and
the pod `app.kubernetes.io/version` label in the overlay's `staging.yaml`.
Both existing values must have the expected format and occur exactly once.
The GitOps PR is ready for the separately guarded staging merger; release PRs
remain drafts. Its branch begins `staging-promotion/opensourceradar-api/`.
Promotion rechecks `develop` before and after PR creation and before closing
each older App staging PR. If superseded, it skips promotion or closes its own
new PR; it never closes a newer PR or a fork PR. The caller must also close older
pending staging PRs on every develop push before running qualification, including
pushes that later fail their checks. The GitOps merger rechecks that the PR stays
open immediately before merging. In-flight publication
is never canceled because source image tags are immutable. Staging runs have
separate concurrency groups by source SHA so a delayed old run cannot replace
the latest pending build. All publication jobs lock the repository and resolved
source SHA so staging and releases cannot race to publish the same immutable
source tags. The default consumer workflow group remains unchanged.

The build job runs caller code. The publication job never checks out or runs
that code: it restores an archive under a run-attempt-specific cache key,
checks its SHA-256, image ID, revision and architecture, then logs in. Registry
credentials use a job-local Docker configuration and an unconditional cleanup.
The promotion job only calls the GitHub API; it never executes product code.

OCIR Frankfurt rejects registry-enforced immutability. The workflow refuses
existing source, architecture and release tags, including ambiguous registry
errors. It publishes a `sha-<commit>-arm64` manifest, then a single-arm64 index
at `sha-<commit>` and, for stable releases, `vX.Y.Z`. Both index tags must resolve
to the same digest. Deployments consume that digest. See the private Stack [OCIR runbook](https://github.com/daybreaker-labs/daybreaker-stack/blob/main/docs/OCIR.md) for identity/bootstrap operations.

Each source SHA can publish once. Inspect partial publication before retrying;
use a new reviewed source commit and release tag if any tag already exists.
Do not delete or overwrite tags to make a retry pass.

## Example

```yaml
jobs:
  image:
    uses: daybreaker-labs/workflows/.github/workflows/product-image.yml@<full-commit-SHA> # v1
    with:
      image-name: your-repository
      publish: false
      promote: false
```

For publishing, pass `OCIR_AUTH_TOKEN` explicitly from the caller. Promotion also needs the two deploy-App secrets above. Never store caller credentials in this repository. `workflow_call` executes in the caller context; the caller owns runner variables, secrets and cache access. This repository's own checks use GitHub-hosted runners.

## Testbox

`.github/workflows/testbox.yml` is the shared frame for a
[Blacksmith Testbox](https://docs.blacksmith.sh/blacksmith-testbox/overview):
`begin-testbox`, then `useblacksmith/checkout` without persisted credentials,
then the caller's setup script, then the SSH environment export and its check,
and finally `run-testbox`, which runs even after a failed setup so the session
reports the failure. It reads no secrets and asks only for `contents: read`.
[FOUND-334](https://linear.app/daybreaker-labs/issue/FOUND-334) introduces it.

| Input | Meaning |
| --- | --- |
| `testbox_id` | The caller's `workflow_dispatch` input. Empty runs validation mode: setup runs, no session starts. |
| `runs-on` | An Ubuntu Blacksmith label, normally a routing variable with a literal fallback. Testbox works only on Blacksmith, and the SSH export relies on Ubuntu's bash and a bash login shell. |
| `timeout-minutes` | Absolute job limit, idle time included. Default 15. |
| `setup` | Repository-relative script, run with `bash -eo pipefail` from the checkout root. |

SSH commands inherit neither `GITHUB_PATH` nor the workflow environment, so
the workflow exports them. After setup, `~/.testbox-env` holds the final
`PATH`, `RUNNER_TEMP`, `GITHUB_WORKSPACE` and every variable the setup wrote
to `GITHUB_ENV`. A line at the top of `~/.bashrc` loads it, before the
interactive-shell guard, because bash reads `~/.bashrc` for commands run by
sshd. The next step checks this in an emulated SSH shell and fails the job if
the values differ. Variables the script only `export`s don't persist. Setup
can't run other actions, so it installs its tools itself. Testbox values are
readable on the VM and in `~/.testbox-env`: use disposable test fixtures only,
never secrets.

### Add Testbox to a repository

1. Write `.github/testbox/setup.sh`. Install tools under `$RUNNER_TEMP`, then
   append their directories to `$GITHUB_PATH` and write each variable the
   tests need to `$GITHUB_ENV`.
2. Add a caller, for example `.github/workflows/testbox.yml`:

   ```yaml
   name: Blacksmith Testbox
   on:
     workflow_dispatch:
       inputs:
         testbox_id:
           type: string
           description: Testbox session ID
           required: true

   permissions:
     contents: read

   jobs:
     testbox:
       uses: daybreaker-labs/workflows/.github/workflows/testbox.yml@<full-commit-SHA> # vX.Y.Z
       with:
         testbox_id: ${{ inputs.testbox_id }}
         runs-on: ${{ vars.CI_RUNNER || 'blacksmith-2vcpu-ubuntu-2404-arm' }}
         setup: .github/testbox/setup.sh
     # The Blacksmith CLI (0.5.3) warms up only a workflow whose own file has a
     # Testbox step. This job satisfies that check and never runs.
     testbox-cli-marker:
       if: false
       runs-on: ubuntu-24.04
       steps:
         - uses: useblacksmith/run-testbox@9b976c32aa3bcbffb348889673585a5deee1b2f3 # v2
   ```

3. Merge the caller to the default branch. GitHub returns 404 when you
   dispatch a workflow file it has not registered yet. Afterwards, any branch
   that contains the file can be warmed up.
4. Run a session from the repository root. Don't pass `--job`: the CLI checks
   that job's own steps and rejects the reusable-workflow job.

   ```sh
   blacksmith testbox warmup testbox.yml --idle-timeout 1
   blacksmith testbox run --id <ID> '<test command>'
   blacksmith testbox stop --id <ID>
   blacksmith testbox list
   ```

   The one-minute idle timeout limits unattended compute: a pause of more
   than about a minute between `run` commands ends the box, so raise it for
   interactive work. `list` and `status` show the marker job's name; the session itself runs in
   the `testbox / Testbox` job. Stop the box after the last command. Stopping
   cancels the Actions job, so a canceled conclusion is expected. The exit code
   of `run` is the test result.

## Versioning

Consumers pin full commit SHAs with a matching version comment. Release compatible
new inputs as a minor `vX.Y.Z` tag and compatible fixes as a patch tag, after the
PR is reviewed, merged and its required checks pass. Create the tag at the merge
commit; never move an existing tag. Existing SHA pins keep their behavior until
their callers explicitly update them.

## Checks

Run `python3 -m unittest discover -s scripts/tests`, `actionlint` and `zizmor --persona=regular .`. The required organization scanner also runs online on pull requests. Offline local checks can use `zizmor --offline --persona=regular .`. `scripts/check` runs all three. The `testbox` job in `checks.yml` calls the Testbox workflow in validation mode on a GitHub-hosted runner with `scripts/tests/fixtures/testbox-setup.sh`.

Renovate's central runner discovers this repository from its root config. Maintainers review dependency changes.

## Rollback

A caller can restore its previous reviewed workflow SHA through a pull request. Close an unmerged promotion PR to cancel deployment; after deployment, revert the promotion commit, restoring the last verified digest (and version label, when enabled), after checking schema compatibility. Preserve database storage, backups and published tags. Credential custody and workload rollout remain in their owning private repositories.
