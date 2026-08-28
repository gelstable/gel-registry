# Maintainer onboarding and offboarding

Checklist for granting and revoking maintainer permissions (PR approvals, snapshot publication, infrastructure applies). Prerequisites: review `CONTRIBUTING.md`.

Maintainers do not hold direct production deployment credentials or modify Vercel dashboard settings directly. Privileges are scoped to pull request approvals and infrastructure apply approvals.

## Development environment

The toolchain is pinned via Nix. Always enter the shell via:

```text
nix develop
```

Provides `terraform`, `tflint`, `uv`, `gh`, `jq`, and `curl`. CI uses identical binaries from `flake.lock`.

## Onboarding

1. **GitHub Org**: Add to `gelstable` organization with write access to `gelstable/gel-registry`. Require 2FA.
2. **Branch Protection**: Add to accounts permitted to push to `main` (bypass list stays empty).
3. **GitHub Environment**: Add as required reviewer on `production`.
4. **HCP Terraform**: Add to `gelstable` organization with access to `gel-registry` workspace. Authenticate via `terraform login`. (Do not issue Vercel tokens).
5. **Vercel**: Grant least-privilege deployment inspection access (no admin, no domain permissions).
6. **Verification**: Verify access and runbooks by running:
   ```text
   nix develop --command terraform -chdir=infra plan
   ```
   and executing the offline registry regeneration flow in `docs/operations.md`. Fix any runbook discrepancies immediately.

## Offboarding

1. Remove from HCP Terraform organization (revokes plan and state read access).
2. Remove from `production` GitHub Environment reviewers.
3. Remove from protected branch push permissions.
4. Remove from Vercel team.
5. Remove from GitHub organization / repository write access.

Routine offboarding requires no secret rotation (maintainers hold no shared tokens). If offboarding is non-routine, rotate `vercel_api_token` and `TF_API_TOKEN` per `docs/secrets.md`.

## Review obligations

Maintainers must enforce two critical repository invariants:

- **Registry Immutability**: `public/s/` (snapshots) and `public/i/` (blobs) are immutable. `bootstrap/` and capture evidence are frozen. Reject any PR modifying existing bytes in these paths. Rollbacks must be pointer updates, not file edits (see `docs/operations.md`).
- **Static Infrastructure Contract**: Review Terraform plan comments, not just source diffs. Reject any resource introducing compute (functions, ISR, rewrites, redirects, build commands) to preserve the static hosting security model.
