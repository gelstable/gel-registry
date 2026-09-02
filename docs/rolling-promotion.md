# Cumulative Rolling Promotion Runbook

This document describes the design and operational procedures for the automated rolling promotion pipeline in `gelstable/gel-registry`.

## The Core Invariant

The promotion workflow operates around a single invariant:

```text
candidate = current main + every valid manifest identity absent from current main
```

The candidate branch `promote/registry` is completely disposable. It is never used as the cumulative source of truth; `origin/main` is the sole source of truth. Every run starts afresh by detaching at `origin/main` and gathering all valid release records from allowlisted repositories that are not yet committed to `main`.

### The A-then-B-before-merge Sequence

Consider the scenario where release **A** is published upstream:

1. A scheduled or manual workflow run starts from current `main`.
2. It discovers release A (which is absent from `main`), writes its record under `releases/`, renders a new candidate snapshot, and pushes branch `promote/registry`, opening a pull request titled `data: promote registry releases`.
3. Before the PR is reviewed or merged, another release **B** is published upstream.
4. The next workflow run triggers. It fetches current `main`. Because the PR has not merged, neither release A nor release B exists on `main`.
5. Discovery gathers both release A and release B.
6. The candidate branch is rebuilt from `main` containing the cumulative union of **both A and B**, and force-pushed with a lease to update the existing PR.
7. Once the PR merges into `main`, both identities (A and B) exist on `main`. The subsequent workflow run skips both; if no other releases are pending, the PR is closed and the remote candidate branch is deleted.

This ensures that an earlier unmerged release is never lost or orphaned when subsequent releases arrive.

---

## Promotion Automation Details

The automated pipeline is defined in `.github/workflows/promote.yml` and driven by `.github/scripts/promote.py`.

### Fixed Branch and Pull Request

- **Fixed Branch**: `refs/heads/promote/registry`
- **Fixed PR Title**: `data: promote registry releases`

There is only ever at most one open promotion PR. If no changes are needed (all eligible upstream releases are already merged to `main`), any existing promotion PR is closed and the remote `promote/registry` branch is deleted.

### Concurrency and Disabled Cancellation

```yaml
concurrency:
  group: promote-registry
  cancel-in-progress: false
```

Cancellation is explicitly **disabled** (`cancel-in-progress: false`). If a workflow run is active, any new run queues rather than aborting the running execution midway. This protects in-flight candidate construction and remote git lease updates against arbitrary mid-run cancellation.

### Force-with-Lease Protection

Before rebuilding the candidate, `.github/scripts/promote.py` queries and records the remote OID of `refs/heads/promote/registry` (if the branch exists remotely).

When pushing the rebuilt candidate branch, it executes:

```bash
git push --force-with-lease=refs/heads/promote/registry:<observed-oid> origin promote/registry
```

If another process or maintainer pushed to `promote/registry` during the run, the lease fails, the push is rejected, and the run aborts safely without corrupting the remote state.

### Allowed Path Restrictions

Candidate generation permits modifications **only** within generated distribution paths:

- `releases/**`: Immutable release records (`releases/<owner>/<repository>/<release-id>.json`).
- `pointers/**`: Selection pointers (`pointers/latest.json`).
- `public/**`: Rendered distribution files, schemas, and content-addressed blobs.
- `vercel.toml`: Static hosting configuration generated from the selected snapshot.

Any change to code (`src/`), workflow files (`.github/`), test suites (`tests/`), or documentation aborts the promotion script immediately before any commit or push occurs.

---

## PR Review and Approval

When reviewing the rolling promotion PR (`data: promote registry releases`), maintainers should verify two primary sections of the PR summary:

1. **Promoted Release Records**:
   - Inspect the added records in `releases/<owner>/<repository>/<release-id>.json`.
   - Verify that the source repository, release ID, tag, and publication timestamp match the upstream GitHub release.
   - For rescues: verify that only artifact URLs are modified in the resulting snapshot.
   - For net-new packages: verify that package version, channel, and platform targets are intended.
2. **Rejected Releases**:
   - The PR description lists any non-draft releases bearing invalid or unparseable `gel-registry.json` manifests, along with the deterministic reason for rejection.
   - If an expected release appears under the rejections list, notify the product team to inspect the reason (e.g., missing asset, syntax error, or URL mismatch) and publish a corrected GitHub release.

---

## Failure Classification and Handling

Failures are bifurcated based on determinism:

### 1. Deterministic Manifest Defects (Per-Release Rejection)

Deterministic defects in an individual release manifest reject **only that release** and allow candidate construction to proceed for all other releases:

- Malformed or invalid JSON syntax in `gel-registry.json`.
- Schema violations against `ReleaseManifest` (e.g. empty manifest, duplicate digest replacements, invalid digest format).
- Asset URL binding failure: an artifact URL does not match `https://github.com/<repo>/releases/download/<tag>/...`.
- Referenced asset missing: an asset named in the manifest does not exist on that GitHub release.
- Multiple manifest assets: more than one `gel-registry.json` asset attached to the release.

These rejections are reported in the PR description as observable diagnostic feedback.

### 2. Infrastructure and Transport Errors (Whole-Run Abort)

Transient or unexpected errors abort the entire run immediately without committing or pushing:

- GitHub API network or connection timeouts.
- GitHub rate limiting (403/429) or authentication errors (401).
- Filesystem I/O failures.
- Non-deterministic environment failures.

These failures indicate that the environment cannot reliably observe upstream state. They are never recorded as durable judgments against a release.

### 3. Composition Invariant Conflicts (Candidate Abort)

Certain semantic conflicts require human resolution and abort candidate publication:

- **Contested Package Identity**: Two different package entries claim the same installable identity `(kind, basename, version, ...)`.
- **Unknown Rescue Digest**: A rescue replacement digest is absent from the historical mirror.
- **Ambiguous Rescue Digest**: A rescue replacement digest matches more than one distinct historical artifact.

---

## Operational Recovery

Because the candidate branch is strictly derived from `origin/main` + upstream APIs:

1. **Discarding a broken candidate**:
   If the candidate branch `promote/registry` enters an unexpected state or a run fails due to a push race:
   - Do not attempt to rebase or surgically edit `promote/registry`.
   - Simply trigger the `Promote Releases` workflow manually via GitHub Actions (`workflow_dispatch`), or run the promotion script locally against a clean checkout of `main`.
2. **Local Candidate Inspection**:
   To reproduce candidate generation locally without pushing:
   ```bash
   uv run gel-registry build-candidate --repo .
   ```
   Inspect the JSON output on stdout detailing the candidate snapshot ID, added record files, and any rejections.
3. **Validating Candidate State**:
   ```bash
   uv run gel-registry validate --repo .
   ```
