# Registry hosting and local operations

Runbook for production operations and local registry generation for `gelstable/gel-registry`.

The registry is an immutable, static distribution index. Vercel serves the checked-in `public/` tree verbatim without build steps, runtime functions, or network fetches.

## Data model & storage layout

- `pointers/latest.json`: Internal tool input pointing to the active snapshot (never served).
- `public/i/<blob-id>.json`: Content-addressed index blobs (SHA-256 truncated to 32 hex chars). Immutable and append-only.
- `public/s/<snapshot-id>/registry.json`: Snapshot manifest containing relative references to blobs in `public/i/`. Immutable.
- `public/registry.json` and `public/v1/snapshots.json`: The only moving documents; generated directly from `pointers/latest.json`.

Blobs in `public/i/` are never deleted: older snapshots referenced by pinned URLs rely on immutable caching.

## Repository setup & branch protection

1. Create `gelstable/gel-registry` on GitHub. Push the initial tree.
2. Configure Actions permissions to read-only; pin actions to commit SHAs.
3. Apply branch protection to `main`:
   - Require pull request with at least one approval; dismiss stale approvals.
   - Require branches to be up to date and pass status check: `Registry validation / local` (`.github/workflows/validate.yml`).
   - Restrict push access to maintainers; disable force pushes and branch deletion.
   - Enforce rules for administrators (no bypasses).

Reproduce CI validation locally:

```text
uv run pytest -q && uv run mypy src tests && uv run ruff check . && uv run ruff format --check . && uv run zizmor --offline .github/workflows
```

## Vercel configuration

Infrastructure is declared in `infra/` (see `docs/infrastructure.md`). End-state configuration:

- Bound to `gelstable/gel-registry`, production branch `main`, root directory `.`.
- Output directory `public`, framework preset `Other`, install/build commands
  empty. No Functions, no ISR, no redirects. The only rewrites are the legacy
  `/archive/.jsonindexes/<platform><channel>.json` aliases, one literal rule per
  published index, which let a client configured with `GEL_PKG_ROOT` address the
  selected snapshot through the paths it already knows.
- `vercel.toml` is rendered from the selected snapshot by
  `gel_registry.render.hosting.hosting_config`, not hand-edited. Verify it with
  `uv run gel-registry validate --repo .`, which fails on any drift between the
  committed file and a fresh render.
- Previews enabled for pull requests.
- Deployment access via Vercel Git integration only. No deployment credentials in GitHub Actions.

### Preview inspection

Before attaching production domains, inspect preview endpoints:

```text
curl --fail --silent --show-error --head https://<preview-host>/healthz
curl --fail --silent --show-error https://<preview-host>/registry.json
curl --fail --silent --show-error https://<preview-host>/v1/snapshots.json
curl --fail --silent --show-error https://<preview-host>/s/<SNAPSHOT_ID>/registry.json
curl --fail --silent --show-error https://<preview-host>/i/<BLOB_ID>.json
```

Verify `healthz` returns static body, roots point to valid snapshots, and no runtime functions are active. Never attach the production domain to an empty snapshot.

## DNS and TLS

Attach `registry.gelstable.com` in Vercel once verified. Add DNS CNAME pointing to Vercel. Validate:

```text
dig +short registry.gelstable.com CNAME
curl --fail --silent --show-error --head https://registry.gelstable.com/healthz
```

Ensure TLS uses the Vercel-managed certificate.

## Legacy capture (Local only)

Live captures fetch the 24-entry legacy matrix into `upstream/packages.geldata.com/legacy-2026-08-bootstrap/` (destination must be empty).

Run in a scratch directory:

```text
CAPTURE_REPO="$(mktemp -d)"
uv run gel-registry capture \
  --repo "$CAPTURE_REPO" \
  --captured-at 2026-08-20T12:00:00Z
uv run gel-registry verify-capture-live --repo "$CAPTURE_REPO"
```

Verify digests, manifest status, and redirect provenance before committing.

## Offline regeneration from committed evidence

Regenerate the bootstrap snapshot deterministically from committed capture evidence without network access:

```text
REPRODUCTION_REPO="$(mktemp -d)"
git archive HEAD | tar -x -C "$REPRODUCTION_REPO"
uv run gel-registry normalize --repo "$REPRODUCTION_REPO"
SNAPSHOT_ID="$(uv run gel-registry publish-bootstrap --repo "$REPRODUCTION_REPO")"
test "$SNAPSHOT_ID" = "0f776b71381237c8"
uv run gel-registry validate --repo "$REPRODUCTION_REPO"
```

Expected bootstrap snapshot ID: `0f776b71381237c8`.

Non-mutating working tree checks:

```text
uv run gel-registry normalize --repo . --check
uv run gel-registry validate --repo .
```

## Selection rollback

Rollbacks update pointers without modifying historical snapshot files:

1. Pick a known-good snapshot ID from `public/v1/snapshots.json` and verify its files in a clean checkout.
2. Update `pointers/latest.json` and run:
   ```text
   uv run gel-registry select-snapshot --repo .
   ```
3. Run validations:
   ```text
   uv run gel-registry validate --repo .
   ```
4. Open PR updating only `pointers/latest.json`, `public/registry.json`, and `public/v1/snapshots.json`.
5. Merge via standard PR flow.

Never edit or delete `public/s/` or `public/i/` files during rollback.

## CLI validation

Test against the production registry with the Gel CLI:

```text
GEL_PKG_ROOT=https://registry.gelstable.com gel server list-versions
```

## Digest audit

Audit production deployment integrity by verifying byte hashes against a clean checkout:

```text
curl --fail --silent https://registry.gelstable.com/s/<SNAPSHOT_ID>/registry.json | shasum -a 256
shasum -a 256 public/s/<SNAPSHOT_ID>/registry.json

curl --fail --silent https://registry.gelstable.com/i/<BLOB_ID>.json | shasum -a 256
shasum -a 256 public/i/<BLOB_ID>.json
```

Blob IDs correspond to the first 32 characters of their SHA-256 hash. Any mismatch blocks releases and triggers incident triage.

## Outage triage & failover

Distinguish registry-host outages from artifact-host outages:

### Registry-host outage (Vercel / DNS / TLS)
Registry metadata and `/healthz` fail; artifact downloads may still work.
1. Deploy the reviewed commit and `public/` directory to the pre-approved backup static host.
2. Verify headers and byte hashes.
3. Update DNS CNAME to the backup provider.
4. Restore primary provider once resolved and repeat digest audit.

### Artifact-host outage (`packages.geldata.com`)
Registry JSON responds normally, but binary downloads (`installref`) fail.
1. Do **not** modify registry snapshots or rollback pointers.
2. Page artifact host / CDN owners.
3. Verify artifact hashes against recorded digests after recovery.

If both fail, track both incidents independently. Keep last known good Git commit and snapshot ID intact.
