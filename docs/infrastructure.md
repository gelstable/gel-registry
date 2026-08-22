# Infrastructure

Hosting infrastructure for `gelstable/gel-registry` (Vercel project, custom domains, DNS records) is defined in Terraform under `infra/`. The declared configuration is the source of truth.

All changes go through pull requests and apply via CI. Do not run `terraform apply` locally or edit settings in the Vercel dashboard.

## Credential paths

Deployment and provisioning use separate paths:

- **Deployment (Vercel Git integration)**: Vercel watches `main` and serves `public/` verbatim. No workflow deploys code, and GitHub Actions holds no deployment credentials.
- **Provisioning (Terraform)**: Manages project configuration, domains, and DNS. Uses a Vercel API token stored as a sensitive variable in HCP Terraform. It never deploys files or modifies `public/`.

Compromise of the provisioning token allows retargeting the production domain (registry integrity incident; see `docs/secrets.md`). Deployment compromise has distinct triage.

## Declared resources

`infra/` declares:
- `vercel_project.registry`: Static project with output directory `public`, no build command, production branch `main`, bound to the GitHub repo.
- `vercel_project_domain.production`: Attaches `registry.gelstable.com` to the project.
- `vercel_dns_record.registry_cname`: The `registry` CNAME in the apex zone.

The apex domain (`gelstable.com`) is not managed in Terraform; zone creation and delegation are manual bootstrap steps. Terraform manages individual records within the zone.

The static contract is strictly enforced: `infra/` defines no serverless functions, rewrites, redirects, build commands, or network fetches. Any addition of compute or proxying alters the security model and requires security review.

## State

State is stored remotely in HCP Terraform (`gelstable/gel-registry` workspace), where plans and applies execute.

- CI authenticates using `TF_API_TOKEN` (HCP team token).
- Maintainers authenticate with `terraform login`.
- Neither CI nor maintainers store the Vercel provisioning token locally.

State contains plaintext resource attributes. Restrict HCP workspace membership to repository maintainers.

## Change flow

```text
pull request  → fmt, validate, tflint, terraform plan → plan posted to PR
merge to main → terraform apply (requires approval)
```

Review the **plan comment**, not just the diff: benign diffs can trigger destructive resource replacements.

Apply runs in a protected GitHub Environment requiring explicit approval. Plans run on `pull_request` (not `pull_request_target`), so forks cannot access secrets and will fail until re-run from a local branch by a maintainer.

Toolchains are pinned via Nix in `.github/workflows/infra-plan.yml` and `.github/workflows/infra-apply.yml`.

## Local verification

Test changes locally before opening a pull request:

1. **Offline validation (no credentials)**:
   ```text
   terraform -chdir=infra fmt -check
   terraform -chdir=infra validate
   tflint --chdir=infra
   ```
2. **Remote speculative plan**:
   ```text
   terraform -chdir=infra plan
   ```
   Executes in HCP using the workspace's Vercel token. Avoid leaving plans hanging (locks state).

There is no staging environment; speculative plans against real state provide sufficient validation without the drift of duplicate environments.

## Provider version pinning

`infra/.terraform.lock.hcl` is committed with multi-platform hashes (`linux_amd64`, `darwin_arm64`, `darwin_amd64`). Update locks using:

```text
terraform -chdir=infra providers lock \
  -platform=linux_amd64 -platform=darwin_arm64 -platform=darwin_amd64
```

Terraform CLI is pinned via `flake.nix` to prevent state format upgrades.

## Bootstrap checklist

Manual setup required before running the first CI plan:

1. **Vercel team & apex domain**: Create `gelstable` team; add `gelstable.com` with nameservers delegated to Vercel (or manage CNAME at an external registrar and drop `vercel_dns_record.registry_cname`).
2. **Vercel GitHub integration**: Install on `gelstable` organization with access to `gel-registry`.
3. **Vercel provisioning token**: Create team-scoped token in dashboard (see `docs/secrets.md`).
4. **HCP Terraform workspace**: Create `gelstable/gel-registry` workspace (CLI-driven workflow). Set Terraform version to match `flake.nix`. Set sensitive variable `vercel_api_token` and string variable `vercel_team_id`.
5. **GitHub Actions secret**: Store HCP team token as `TF_API_TOKEN`.
6. **GitHub Environment**: Configure `production` environment with required reviewers.
7. **Resource imports**: If adopting existing resources, populate and uncomment IDs in `infra/imports.tf` before the first apply. Leave commented for greenfield setups.
