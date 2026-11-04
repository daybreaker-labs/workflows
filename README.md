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
  all callers receive the source commit as `VCS_REF`.
- `publish: true` accepts a `vX.Y.Z` push, or a manual dispatch on `main` or
  `develop`. Stable releases must already belong to fetched `origin/main`.
  Branch dispatches publish `sha-<full commit>` for qualification.
- `promote: true` additionally requires a stable release. It opens a **draft**
  GitOps PR; a maintainer reviews and merges it to deploy. Qualification images
  cannot promote.
- Required caller configuration: `CI_DOCKER_RUNNER` for native ARM builds,
  `CI_RUNNER` for promotion, `OCIR_USERNAME` and secret `OCIR_AUTH_TOKEN`.
  Promotion also needs explicit `DEPLOY_APP_ID` and `DEPLOY_APP_PRIVATE_KEY`
  secrets for a GitHub App installed on `daybreaker-gitops` with Contents write
  and Pull requests write. No `secrets: inherit`.
- The promotion contract is exactly one image pin in each of
  `components/<image-name>/base/deployment.yaml` and `migrate-job.yaml`.
  Promotion changes both pins in one Git commit and leaves the PR in draft.

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

## Checks

Run `python3 -m unittest discover -s scripts/tests`, `actionlint` and `zizmor --persona=regular .`. The required organization scanner also runs online on pull requests. Offline local checks can use `zizmor --offline --persona=regular .`.

Renovate's central runner discovers this repository from its root config. Maintainers review dependency changes.

## Rollback

A caller can restore its previous reviewed workflow SHA through a pull request. Close an unmerged promotion PR to cancel deployment; after deployment, revert both GitOps image pins to the last verified digest after checking schema compatibility. Preserve database storage, backups and published tags. Credential custody and workload rollout remain in their owning private repositories.
