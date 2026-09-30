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
- **Static Infrastructure Contract**: Review Terraform plan comments, not just source diffs. Reject compute, build commands, and routing outside the generated legacy index rewrites and the single allowlisted package-pool redirect. Package clients verify downloads against signed metadata.

## Native repository keys and clients

`public/keys/gelstable.asc` is the public certificate; its primary fingerprint
is generated in `public/keys/gelstable.fingerprint`. The current certificate is
**disposable development material**, and its private material has been deleted.
Replace it with the production certificate and re-sign all APT and RPM metadata
before merging for production. Never commit private keys.

Render metadata with `gel-registry native render --repo . --cache DIR --out public`.
Sign with an isolated `GNUPGHOME` and the full signing subkey fingerprint in
`GELSTABLE_SIGNING_FPR`, then run `gel-registry native sign --repo .`.
Regenerate hosting configuration and support files with
`gel-registry publish-bootstrap --repo .`. When replacing the certificate,
remove its old generated fingerprint before regenerating. Validate the complete committed tree
with `gel-registry validate --repo .`.

The generated `gelstable.sources` and `gelstable.repo` select stable. The testing
APT source is a separate `gelstable-testing.sources`; the testing DNF section
in `gelstable-testing.repo` starts disabled. Both clients use the same public
certificate, require repository signatures, and verify package checksums.
Install the APT certificate at `/etc/apt/keyrings/gelstable.asc` and compare its
fingerprint with the published fingerprint before installing the source file.

A preview deployment must serve `/apt/dists/stable/InRelease` and return a 307
from an allowlisted `/apt/pool/<repo>/<tag>/<asset>` URL to its GitHub release
asset. Also check metadata, key, and pool cache headers and reject unknown
repositories. Deployment acceptance is pending; local configuration checks do
not exercise the hosting provider.
