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

## Candidate building & rolling promotion

Candidate releases are gathered and staged automatically via the rolling promotion pipeline (see `docs/rolling-promotion.md`).

### Building a candidate locally

To scan allowlisted upstream repositories (`sources/github.json`), gather missing non-draft release manifests, write immutable records under `releases/<owner>/<repo>/<id>.json`, and publish a candidate snapshot locally:

```text
uv run gel-registry build-candidate --repo .
```

The command outputs a single JSON object to stdout detailing the newly rendered snapshot ID, added release record paths, and any deterministically rejected releases.

### Rolling promotion pull request

The production promotion pipeline is scheduled in `.github/workflows/promote.yml` and executed by `.github/scripts/promote.py`:
- Rebuilds candidate branch `promote/registry` fresh from `origin/main`.
- Enforces the core invariant: candidate = current `main` + all eligible unmerged releases.
- Pushes with lease protection: `--force-with-lease=refs/heads/promote/registry:<observed-oid>`.
- Maintains a single rolling PR titled `data: promote registry releases`.
- Runs with `cancel-in-progress: false` to avoid mid-run interruptions.

### Operational recovery

Because candidate construction is completely disposable and deterministically reconstructed from `main` + upstream APIs:
- If a candidate branch is broken, has conflicting merges, or suffers a push-lease race, **do not manually rebase or edit `promote/registry`**.
- Trigger the workflow again via GitHub Actions (`workflow_dispatch`), or re-run `python .github/scripts/promote.py` from a clean checkout of `main`.

## Publication transaction (`publish_registry`)

The publication transaction (`gel_registry.publication.publish_registry`, also accessible via CLI alias `uv run gel-registry publish-bootstrap --repo .`) atomically renders:
- Package indexes composed from frozen `bootstrap/` and immutable records under `releases/`.
- Content-addressed blob files under `public/i/<blob-id>.json`.
- Pinned snapshot registry manifests under `public/s/<snapshot-id>/registry.json`.
- Moving roots `public/registry.json` and `public/v1/snapshots.json`.
- Public JSON Schemas under `public/v1/schema/`.
- Hosting configuration in `vercel.toml`.
- Pointer file `pointers/latest.json`.

All file writes are atomic, symlinks are rejected, and non-whitelisted paths are barred from mutation.

## Public JSON Schema generation

Public schemas for all registry documents are maintained under `public/v1/schema/`:
- `release-manifest.json`: Public schema for publisher `gel-registry.json` manifests (see `docs/release-manifest.md`).
- `release-record.json`: Schema for committed records in `releases/`.
- `package-index.json`, `capture.json`, `pointer.json`, `root.json`, `snapshot-listing.json`.

Regenerate schemas without modifying snapshots:

```text
uv run python -c "from pathlib import Path; from gel_registry.render.schemas import render_schemas; render_schemas(Path('.'))"
```

Verify schemas and snapshots offline:

```text
uv run gel-registry validate --repo .
```

## Legacy rescue runbook

The legacy rescue subsystem captures legacy index packages from `packages.edgedb.com`, plans migration into GitHub draft releases for eligible upstream versions, streams original artifacts directly to draft releases, uploads a replacement-only `gel-registry.json` manifest, and relies exclusively on manual operator review and publication followed by the standard promotion pipeline.

### Capture legacy indexes

Capture the 24-entry matrix from `packages.edgedb.com` into `upstream/packages.edgedb.com/<capture-id>/`:

```text
uv run gel-registry rescue-capture --repo . --captured-at 2026-08-20T12:00:00Z
```

The capture records each index's exact SHA-256 digest and byte size. One known-absent index (`testing/aarch64-pc-windows-msvc`) is explicitly recorded as absent when upstream returns 404.

### Bulk rescue plan

Generate a plan across all four product types in one canonical JSON output:

```text
uv run gel-registry rescue-plan --repo . --capture legacy-2026-08-bootstrap --bulk > plan.json
```

### One-off rescue plans

```text
# gel CLI, one version
uv run gel-registry rescue-plan --repo . --capture legacy-2026-08-bootstrap \
  --product cli --version 3.0.0 > plan.json

# gel server, one version
uv run gel-registry rescue-plan --repo . --capture legacy-2026-08-bootstrap \
  --product server --version 4.1.2 > plan.json

# language server, one version
uv run gel-registry rescue-plan --repo . --capture legacy-2026-08-bootstrap \
  --product ls --version 4.1.2 > plan.json

# PostGIS extension, one server slot
uv run gel-registry rescue-plan --repo . --capture legacy-2026-08-bootstrap \
  --product postgis --slot 4.1 > plan.json
```

`--product cli|server|ls` each require exactly one `--version`; `--product postgis` requires exactly one `--slot`. `--bulk` takes neither.

### Dry run and publication

Run the full preflight to inspect every destination and print intended operations without mutating GitHub state:

```text
uv run gel-registry rescue-publish --plan plan.json --dry-run
```

Execute draft creation and artifact uploads:

```text
GITHUB_TOKEN=... uv run gel-registry rescue-publish --plan plan.json
```

### Review, manual publication, and promotion

1. **Review drafts on GitHub**: Operators review each draft release created under `gelstable/gel`, `gelstable/gel-cli`, or `gelstable/gel-postgis`. Confirm that all expected original binary assets are present and `gel-registry.json` contains the expected canonical replacements.
2. **Publish draft release**: When reviewed and approved, an operator manually publishes the draft release on GitHub.
3. **Rolling promotion**: The standard promotion pipeline (`uv run gel-registry build-candidate --repo .` or `.github/scripts/promote.py`) discovers published non-draft releases, extracts `gel-registry.json`, validates that each replacement matches an immutable bootstrap digest, and promotes the replacements into candidate snapshots.

### Streaming and resume behavior

- **Streaming without temp files**: Every original artifact streams directly from `packages.edgedb.com` to GitHub draft release with bounded memory and zero temporary files. Exact byte length and SHA-256 digests are verified on transfer.
- **Manifest uploaded last**: In each draft release, all binary assets are uploaded first. Only when all binaries are successfully uploaded is `gel-registry.json` generated and uploaded to the draft.
- **Resumption from GitHub state**: Execution is fully idempotent and resumable purely from GitHub's reported state (no local journal). Complete matching releases are skipped. Missing binaries are uploaded first, followed by the manifest.
- **Conflict detection and safety**: Preflight is all-or-nothing. It rejects published releases, duplicate asset names, incomplete uploads, and size/digest mismatches. An existing `gel-registry.json` claiming absent or conflicting binaries is rejected. Unrecognized extra assets (e.g. experimental `.zst` files) are permitted and never deleted.
- **Reconciliation**: If an upload error leaves a partial asset behind on GitHub, `rescue-publish` inspects the release and reports the numeric asset ID for manual removal by an operator. Nothing is deleted automatically.

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

## Native package operations

The [installation guide](native-packages.md) documents client setup; the
[development guide](native-development.md) documents local rendering and fixture
tests. Native packages add signed APT and RPM metadata to this static registry.
Package bytes remain GitHub release assets and are excluded from the deployed
tree. The lock `native/packages.lock.json` records every selected package,
channel, format, native architecture, full epoch/version/revision, digest, size
and repository path.

**Launch prerequisite:** the current empty repositories use the production
certificate backed up in signing custody, and all metadata has been re-signed.
Production smoke and migration acceptance remain pending. Empty-channel skips
in the acceptance workflow do not demonstrate a successful installation.

### Publish a product release

1. In the product repository (`gel`, `gel-cli`, or `gel-postgis`), run its native
   packaging workflow from the default branch. The intended pipeline builds
   packages without secrets, verifies source artifact SHA-256 values, creates
   packages with nFPM, and installs them in matching-architecture containers.
2. The signing job uses the default-branch-only `package-signing` Environment.
   It signs RPM assets and verifies them against the public certificate. APT
   authenticates DEB digests through the registry's signed Release metadata.
3. Review the draft release, assets and v2 `gel-registry.json` manifest before
   publication. Confirm channel, architectures, package versions, revisions,
   sizes and SHA-256 values. Testing releases must be GitHub prereleases.
   Asset names use `<name>-<version>-<revision>-<arch>.deb` or `.rpm`, with `~`
   replaced by `-` in filenames. Native versions use epoch `1`, and prerelease
   versions use `~` so distribution version ordering remains correct.
4. Publish the reviewed release. The registry promotion workflow runs hourly
   at minute 17, or can be dispatched manually. It collects published,
   non-draft releases from the allowlist; publication alone does not deploy
   registry metadata. A maintainer must review and merge the promotion PR.

Treat already-published manifests and package bytes as immutable. Corrections
require a new release with a higher revision rather than editing a recorded
release or replacing an asset in place. Product workflows and their acceptance
must be ready before the first production publication.

### Promote testing to stable

Publish a new product release manifest with channel `stable` through the product
release process. Do not edit the committed testing release record or move files
between repository directories. Review the stable manifest's versions, digests
and RPM signatures just as for a new release. If the publication changes package
bytes, including re-signing an RPM, give those bytes a new digest and revision.
The rolling promotion will select and render the stable channel after discovery.

### Review the promotion PR

The single rolling PR is titled `data: promote registry releases`. It is rebuilt
from current `main` plus eligible unmerged releases, so it may grow while under
review. Inspect the latest commit and all three sections of its body:

- **Added release records:** confirm the allowlisted publisher, release identity,
  channel, package family, architecture and asset provenance in each record.
- **Rejected releases:** investigate deterministic manifest rejections. Package
  download, digest, identity or RPM signature errors abort rendering and are
  workflow failures, not accepted rejection entries.
- **Native package lock changes:** compare additions and removals, full versions,
  formats, architectures, digests and sizes with the release intent and yanks.

Confirm the corresponding APT indexes and RPM repodata changed for the expected
channels only, signatures validate against the committed key, and both native
and portable validation pass. No binary pool should be committed. The `render`
job produces unsigned metadata with read-only permissions; `publish` applies
that artifact against the exact render base and signs changed native metadata
inside `registry-signing` before validation and branch publication. A changed
base or lease race requires a rerun, never manual edits to `promote/registry`.
Merge only the reviewed, validated candidate; Vercel then serves that commit.

### Yank a bad native asset

Add every affected DEB/RPM SHA-256 to the JSON array in `native/yanked.json`:

```json
[
  { "sha256": "<64 lowercase hex characters>", "reason": "Explain the defect" }
]
```

Prepare the yank and replacement repository state as one change:

1. In an environment without signing secrets, update `native/yanked.json` and
   regenerate the lock and unsigned metadata:

   ```sh
   uv run gel-registry native render --repo . --cache /tmp/gel-native-cache --out public
   ```
2. Transfer the changed yank list, lock and public metadata tree to an authorized
   signing checkout at the same base commit. Exclude the package cache and any
   scratch package pool; the signer must not download or parse packages.
3. With the authorized signing key home and `GELSTABLE_SIGNING_FPR` configured,
   run `uv run gel-registry native sign --repo .`. Remove the secret key home,
   then run `uv run gel-registry validate --repo .` using only public files.
4. Review and merge the yank list, regenerated lock, metadata and signatures
   together in one PR. A yank-only change fails lock validation.

Until this complete change is deployed, clients can still select the affected
asset. Cover each affected format and architecture; yanks identify bytes,
not version strings. Publish a fixed higher revision and advise affected users
how to upgrade. Clients never downgrade automatically, and yanking does not
uninstall a package already installed. Retain immutable release records and
historical portable snapshots. Selecting an older portable snapshot does not
roll back native repositories.

### Signing keys and Environment secrets

The production certificate uses an RSA 4096 certify-only primary key and a
signing subkey for automation. RSA provides compatibility with supported APT
and EL9 RPM clients. The certificate and metadata have no expiry; unattended
clients do not need periodic trust refreshes, but compromise requires explicit
rotation and an advisory.

| Repository | GitHub Environment | Secrets |
| --- | --- | --- |
| `gel-registry` | `registry-signing` | `REGISTRY_SIGNING_KEY`, `REGISTRY_SIGNING_FPR` |
| `gel`, `gel-cli`, `gel-postgis` | `package-signing` | `PACKAGE_SIGNING_KEY`, `PACKAGE_SIGNING_FPR` |

Restrict signing Environments to each repository's default branch. Key secrets
contain the exported signing subkey material; fingerprint secrets identify the
full **signing subkey** fingerprint, distinct from the primary fingerprint
published to users. Registry signing exposes `REGISTRY_SIGNING_FPR` to the CLI
as `GELSTABLE_SIGNING_FPR`; local signing uses that same variable and an explicit
`GNUPGHOME`. Keep the primary private key out of CI, Git and committed files.
Export only secret subkeys for the protected GitHub Environments, never the
primary private key. The current batch import and signing workflows have no
passphrase input: the automation subkey export must have no passphrase. Keep
that export protected by 1Password and GitHub Environment secret storage;
an encrypted export that needs an interactive unlock will not work in CI.
An ordinary export of a passphrase-protected subkey retains that protection.
If removing a subkey passphrase for automation, work only on an isolated copy
containing secret subkeys and no primary private key; never remove protection
from the custody primary or its backup.
The primary private-key backup remains protected by its own strong passphrase.

### Local signing-key helper

`scripts/signing-key.py` wraps standard GnuPG locally using Python's standard
library. Install Python 3.13 and GnuPG 2.4 with a working pinentry; on macOS,
`brew install python@3.13 gnupg pinentry-mac` supplies them. Run from a private
operator terminal. For terminal pinentry, set `GPG_TTY="$(tty)"`; a graphical
pinentry can be selected with `--pinentry-program /absolute/path/to/pinentry-mac`.
Trusted executable paths resolve to their canonical executable, including
Homebrew symlinks; secret/input/output paths still refuse symlinks.
The helper creates isolated temporary homes and stops their agents, preserving
your existing `GNUPGHOME` and keyrings. It never installs secrets, publishes
keys, changes your public trust, or revokes custody/caller keys.

Choose a **new** output directory outside every Git checkout. Existing paths
and symlink destinations are refused. The directory is private (`0700`) and
all artifacts are private (`0600`), including the public certificate:

```sh
python3 scripts/signing-key.py create \
  --uid 'Gelstable Package Signing <maintainer-controlled-address>' \
  --out "$HOME/SigningCustody/gelstable-initial"
```

GPG first asks for a strong passphrase protecting the RSA4096 certify-only
primary and its RSA4096 signing subkey; neither expires under current policy.
Save this passphrase separately in custody. Generation, backup export and
subkey export may ask you to unlock that key again. In a **separate subkey-only
home**, GPG then asks for the existing passphrase followed by a new passphrase:
leave the new passphrase empty and confirm removal. The helper uses GPG agent's
[interactive PASSWD command](https://www.gnupg.org/documentation/manuals/gnupg/Agent-PASSWD.html)
for this selected subkey. The protected custody primary never undergoes that
change. The script never reads a passphrase and accepts no passphrase argument
or environment variable. Do not record the ceremony terminal or pinentry.

| Artifact | Custody purpose |
| --- | --- |
| `primary-backup.asc` | Passphrase-protected primary plus existing subkeys; offline recovery and future rotation |
| `automation-subkey.asc` | Selected **unprotected** signing secret only; source for Environment key secrets |
| `public.asc` | Full public certificate, retaining historical public subkeys |
| `fingerprints.json` | Primary, selected signing and historical subkey fingerprints; artifact integrity hashes |
| `primary-revocation.rev` | Primary emergency revocation artifact, with GPG's import-protection colon intact |
| `README.txt` | Bundle custody and manual recovery reminders |

Upload these files as separate 1Password Documents or attachments and store the
primary passphrase in the separate custody item described below. Protect the
unprotected automation export with vault and Environment access controls. Never
upload the primary backup or its passphrase as a CI signing secret. Compare the
printed fingerprints with the independently recorded ceremony inventory before
using any exported key.

For routine signing-subkey replacement, download the protected primary backup
**and its original primary revocation artifact** to private files. Supply the
recorded full primary fingerprint, rather than a UID or short key ID:

```sh
python3 scripts/signing-key.py rotate-subkey \
  --backup /secure/downloads/primary-backup.asc \
  --revocation-certificate /secure/downloads/primary-revocation.rev \
  --primary-fingerprint '<40-character recorded primary fingerprint>' \
  --out "$HOME/SigningCustody/gelstable-rotation-2026-10"
```

The prompt sequence is the same: unlock the protected primary to certify the
replacement, then remove protection only from the new isolated automation copy.
The refreshed primary backup and public certificate retain historical subkeys.
The supplied revocation artifact is authenticated against the exact primary
before replacement key generation, then carried forward unchanged. No custody
or caller key is revoked or deleted.
If the primary is compromised, use `create` and the new-trust procedure below.

The helper verifies every completed bundle. Also verify downloaded vault copies
in a new private directory, using fingerprints from your independent inventory:

```sh
python3 scripts/signing-key.py verify \
  --bundle /secure/downloads/gelstable-bundle \
  --primary-fingerprint '<40-character recorded primary fingerprint>' \
  --signing-fingerprint '<40-character recorded signing-subkey fingerprint>'
```

`verify` checks artifact integrity, matching certificate fingerprints and key
policy, protected primary backup, absence of primary or unrelated CI secrets,
and a test signature from the exact signing subkey. Fresh isolated agents with
`--pinentry-mode error` ensure an encrypted/nonfunctional CI export fails rather
than succeeding through a cached passphrase. Public-only verification checks the
actual signing fingerprint. All four artifact hashes must be present and valid;
they provide consistency, not a replacement for independently recorded
fingerprints. Revocation authentication uses a separate disposable public-only
copy: that copy must begin unrevoked and become revoked by the supplied
certificate for exactly the recorded primary. The import-protection colon is
removed only from the temporary validation copy; the original artifact, custody
primary and caller keyrings stay unchanged. Separately perform
the manual primary-unlock restore test below with the saved passphrase.

On cancellation or failure, temporary homes are cleaned up and output retains
an `INCOMPLETE` marker and any backups already exported. Keep those backups,
investigate the failure and choose a new output directory for another ceremony.
Do not install an incomplete bundle. After verified vault restore and manual
primary unlock, remove local exports according to custody policy.

Continue the reviewed key-rotation procedure below: update GitHub Environment
secrets, public certificates and fingerprints, the Docker image's primary-key
fingerprint pin, and re-sign all bootstrap metadata.
These remain operator actions. Emergency revocation activation and distribution
also remain manual; never remove the `.rev` colon or import it during routine
restore or rotation.

### Signing-key custody and recovery

While the project has one maintainer, a personal 1Password account with a
dedicated **Gelstable Signing** vault is an acceptable custody model. Generate
the keys locally on a maintained Mac with disk encryption, in a protected
`GNUPGHOME` outside the checkout. A separate offline key-generation ceremony
and independent encrypted backups are optional stronger arrangements; a second
maintainer is not a prerequisite for the solo setup.

Keep these project items in the dedicated vault:

| Item | Contents |
| --- | --- |
| Primary private-key backup | Passphrase-protected private-key export for certification and recovery; never upload it to CI. |
| Primary-key passphrase | The strong passphrase for the primary backup, recorded separately from the backup item. |
| Signing-subkey backup | Secret-subkey-only export matching the automation passphrase policy above. |
| Revocation certificate | The certificate needed to revoke the primary if it is compromised; treat it as sensitive. |
| Public certificate and fingerprints | Public certificate plus full primary and signing-subkey fingerprints, clearly labeled. |
| Recovery and rotation guide | This procedure, item/file inventory, restore-test record and custodian details; no secrets in the repository copy. |

Store key exports and the revocation certificate as Document items or attached
files, following [1Password's file instructions](https://support.1password.com/files/).
Before deleting temporary exports, download the saved backups and restore them
into isolated temporary `GNUPGHOME` directories with mode `700` outside the
checkout. Compare the primary and subkey fingerprints with the recorded public
certificate using `gpg --with-fingerprint --with-subkey-fingerprint` when
listing restored keys. Confirm the primary backup unlocks with its saved
passphrase. In a separate home containing only the automation export, confirm
the primary secret is absent and make a batch test signature selecting the
full signing-subkey fingerprint with `!`. Use a fresh agent with no cached
passphrase and `--pinentry-mode error` to prove no interactive unlock is needed;
verify that signature using only the public certificate and check the
signing-subkey fingerprint. Record the result,
stop the temporary GPG agents, and remove the temporary homes and exports.

Prepare the account [Emergency Kit](https://support.1password.com/emergency-kit/)
and an [individual-account recovery code](https://support.1password.com/recovery-codes/).
Keep accessible protected copies outside this same vault, such as in a physical
safe, so losing vault access does not also lose recovery access. Plan recovery
of the account's email and authenticator/2FA separately: recovery-code use needs
email access and leaves enabled 2FA in place. Update the Emergency Kit after
account credentials change. These account-recovery materials are personal
account credentials, separate from the project's key-custody items.

When other maintainers join, move only project items into a restricted
**Gelstable Signing** custody vault in an organization/team 1Password account.
Use the [account migration instructions](https://support.1password.com/migrate-business-account/)
for moving items, choosing the shared custody vault rather than an Employee or
private vault. Verify every item and all Document/attachment files are present
and downloadable in the destination, then repeat the restore test before
removing old copies. Assign appropriate organization owners and designated
key custodians, with actual item access and a working account-recovery path;
review [vault permissions](https://support.1password.com/create-share-vaults-teams/),
including inherited group and owner access. Contributors who only review or
approve releases do not need key custody. Team account recovery uses
administrators rather than individual recovery codes.

Moving custody preserves the key fingerprints and does not require rotation
unless compromise is suspected. Remove obsolete copies and access after the
verified handoff, but neither deletion nor removing vault access cryptographically
revokes a key already copied by someone. Use the compromise procedure below
when previously exposed key material can no longer be trusted.

### Key rotation and compromise

Use a reviewed maintenance change for the first production certificate and
subsequent rotations. The deleted development private key cannot be reused.

1. For the first production certificate, create an RSA 4096 certify-only
   primary key locally under the custody model above, then create its signing
   subkey. Save the primary backup and passphrase, signing-subkey backup,
   revocation certificate and public fingerprints in the custody vault and
   complete the restore test before deleting temporary exports or publishing.
   For later rotations, restore the existing primary into a protected local
   key home to create a replacement signing subkey, then update and restore-test
   the custody backups. Revoke a compromised subkey and export the updated
   public certificate with its revocation and the new subkey. If the primary
   is compromised, create and back up a new primary
   certificate, then distribute its new fingerprint through the established
   maintainer advisory channel.
2. Replace `public/keys/gelstable.asc` with the armored public certificate.
   Update all four repositories' Environment key and fingerprint secrets listed
   above. Coordinate their public-key copies and signing configuration so new
   RPM assets can be verified by the registry.
3. Remove the stale generated fingerprint before regenerating support files;
   support rendering rejects changed predecessor bytes. From the registry root:

   ```sh
   rm public/keys/gelstable.fingerprint
   uv run python -c "from pathlib import Path; from gel_registry.render.schemas import render_schemas; render_schemas(Path('.'))"
   cat public/keys/gelstable.fingerprint
   ```

   Independently inspect the new primary fingerprint with
   `gpg --show-keys --with-fingerprint public/keys/gelstable.asc`. Update the
   fingerprint and key status in `README.md` and `docs/native-packages.md` to
   match the newly generated file exactly.
4. Re-sign **all** existing APT and RPM metadata, even if the package lock is
   unchanged. Ordinary promotion signs only changed native metadata; a render
   no-op is not a rotation. Import the signing subkey into a temporary protected
   key home and run:

   ```sh
   export GNUPGHOME="$(mktemp -d)"
   chmod 700 "$GNUPGHOME"
   gpg --batch --import /secure/path/signing-subkey.asc
   export GELSTABLE_SIGNING_FPR='<full new signing subkey fingerprint>'
   uv run gel-registry native sign --repo .
   gpgconf --kill gpg-agent
   rm -rf "$GNUPGHOME"
   unset GNUPGHOME GELSTABLE_SIGNING_FPR
   uv run gel-registry validate --repo .
   ```

   Signing writes every APT `InRelease` and `Release.gpg`, and every RPM
   `repomd.xml.asc`, and immediately checks the signatures using the public
   certificate only. Final validation runs after the secret key home is removed.
   Also run the configured repository checks before publishing this change.
5. Review the key, fingerprint, documentation and all signatures as one change.
   Before removing an old usable key from the public certificate, account for
   existing RPM assets signed by it. Re-signing RPMs changes their bytes and
   digests: publish replacement assets with higher revisions and new manifests,
   yank compromised assets as appropriate, and rebuild native metadata.
   Metadata re-signing alone does not change RPM package signatures.
6. Publish the reviewed change and issue an advisory with the affected key,
   replacement primary fingerprint, affected releases and client remediation.
   Tell APT users to re-download `/etc/apt/keyrings/gelstable.asc` and verify its
   fingerprint; tell DNF users to verify and import the updated certificate
   before refreshing metadata. A revoked key cannot authenticate its own
   replacement reliably; use the advisory to establish replacement trust.
   Check production acceptance after publication and track users of installed
   compromised packages separately.

If a compromise is suspected, stop affected publication and promotion until
trusted signing is restored. Preserve evidence and do not disable client
signature checks to recover availability.

### Native production acceptance

`.github/workflows/native-smoke.yml` schedules production smoke checks daily
at 06:43 UTC, runs after successful production deployments, and accepts manual
`mode=smoke` or `mode=migration`. Smoke covers both channels on Debian 12/13,
Ubuntu 22.04/24.04/26.04 and Rocky 9, on x86_64 and aarch64 runners. It checks the
committed lock for `gel-7` first; launch-empty combinations exit successfully
without installation. Verify logs actually show installed packages, the server
query and PostGIS spatial query before recording acceptance.

`tests/clients/smoke.sh` installs from the production registry with normal
signature enforcement, starts a disposable Gel server, and checks PostGIS.
Manual migration mode runs `tests/clients/migrate.sh` on Debian 12 and Rocky 9,
installs legacy Gel 7, writes a sentinel, switches repositories, upgrades and
queries the retained data. These scripts run as root in disposable containers;
they are not instructions for upgrading a production database. Fresh VM checks
are still needed to establish service behavior under a real init system.
Neither production smoke nor migration has been executed for launch yet.

When acceptance fails, the report job opens an issue titled
`Native package production acceptance failed` with a link to the workflow run.
Read the failing distribution, architecture and channel logs, and separate:

- DNS/TLS or static-host errors: compare deployed metadata with the reviewed
  commit and check `/healthz`, key and source configuration endpoints.
- Signature or fingerprint errors: stop publication, compare deployed and
  committed public keys and signatures, and inspect the signing job. Use the
  rotation procedure if necessary; never bypass authentication.
- Asset download/digest errors: compare GitHub release assets, recorded digests
  and lock entries. Cache bytes are verified on every use; rerun transient
  download failures and publish a higher revision for bad assets.
- Install, server, PostGIS or migration errors: reproduce the same matrix entry
  in a disposable system, inspect package versions and server logs, and yank
  defective assets while preparing the fixed revision.

Track skipped empty channels separately from passing installs. After a reviewed
fix is deployed, rerun the failed mode and verify the actual queries and retained
data. Keep the incident open until the affected matrix entries are accepted.
