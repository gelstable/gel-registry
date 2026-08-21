# Registry hosting and local operations

This runbook describes the production setup for `gelstable/gel-registry` and
the local operations that produce its checked-in registry tree. The registry is
a static, versioned distribution index. Vercel publishes the checked-in
`public/` tree verbatim; it does not run the Python package, fetch legacy
indexes, download release artifacts, or generate registry bytes during a
deployment.

The internal `pointers/latest.json` file is an input to the publication tools
and is never served. `public/s/<snapshot-id>/` and `public/i/` are immutable
history. The moving `/registry.json` and `/v1/snapshots.json` files are
generated from the pointer and are the only moving registry documents.

Index bytes are stored once, addressed by their content, at
`public/i/<blob-id>.json`. A snapshot is not a directory of index copies but a
single manifest of relative pointers into that shared store:
`public/s/<snapshot-id>/registry.json`. A release that changes eight indexes
therefore adds eight blobs and one manifest; the indexes it did not change are
referenced by the new snapshot rather than copied into it. A blob is never
deleted, even once no selected snapshot references it, because older pinned
snapshots still do and their `immutable` cache header promises they resolve
forever.

The deployment has no functions, no rewrites, no redirects, no build command,
and no framework preset. There are no build-time network fetches. Each Vercel
deployment is an immutable static artifact promoted atomically; a provider
failover must preserve that same static-only contract.

## Repository creation

1. Create `gelstable/gel-registry` in the `gelstable` GitHub organization and
   choose the default branch name used by the organization (normally `main`).
   Keep the repository's visibility and security settings consistent with the
   organization's distribution-source repositories.
2. Add the repository's default branch as `origin` and push the reviewed
   source, workflow, lockfile, and `public/` tree. Do not create a production
   domain or Vercel project pointing at an empty repository.
3. In repository Settings, enable Actions only for the checked-in validation
   workflow and allow only GitHub/verified actions that the workflow pins to
   full commit SHAs. Keep the default `GITHUB_TOKEN` permission read-only.
4. Create the branch protection rule below before accepting changes to the
   registry tree.

## Default-branch protection

Apply these settings to the default branch:

- Require a pull request before merging, at least one independent approval,
  dismissal of stale approvals, and resolution of all conversations.
- Require branches to be up to date before merging and require the exact
  `Registry validation / local` status check from
  `.github/workflows/validate.yml`.
- Restrict who may push to the default branch to the repository maintainers.
  Disable force pushes (**Allow force pushes: disabled**) and disable branch
  deletion (**Allow branch deletion: disabled**). Do not permit a workflow token
  to bypass this rule.
- Enforce the rule for administrators, keep the bypass list empty, and do not
  allow direct commits from a deployment provider. Only the protected-branch
  merge process publishes the reviewed tree.

The required local check is reproducible from a clean checkout:

```text
uv run pytest -q && uv run mypy src tests && uv run ruff check . && uv run ruff format --check .
```

The checked-in workflow has read-only permissions and performs only offline
validation. Capture, live rehearsal, and bootstrap publication are explicit
local operations; Vercel never fetches legacy indexes or runs the capture tool
during deployment.

## Vercel Git integration

Create one Vercel project for `gelstable/gel-registry` with the following
settings:

- Grant the Vercel Git integration access to this repository only, not the
  whole organization. Set the production branch to the protected default
  branch and leave the repository root as the project root.
- Use the `Other`/static configuration represented by `vercel.json`; output
  directory is `public`. Leave install and build commands empty. Do not add a
  Vercel Function, server, ISR route, rewrite, redirect, or build hook.
- Allow preview deployments for pull requests so that reviewers can inspect
  the exact checked-in tree. Limit production deployment and domain changes to
  Vercel project administrators. Give observers the Viewer role and routine
  maintainers only the least-privilege deployment role.
- Treat the production deployment as an atomic switch to one reviewed Git
  commit. Keep the previous deployment available for an emergency provider
  rollback, then reconcile the Git pointer through the selection-only rollback
  process below.
- Do not put a Vercel token or provider credential in GitHub Actions. The Git
  integration is the only deployment path; repository workflows validate the
  reviewed tree but do not deploy it.

### Preview inspection and production attachment

Before attaching the production domain, inspect a preview deployment at every
available public surface:

```text
curl --fail --silent --show-error --head https://<preview-host>/healthz
curl --fail --silent --show-error https://<preview-host>/registry.json
curl --fail --silent --show-error https://<preview-host>/v1/snapshots.json
curl --fail --silent --show-error https://<preview-host>/s/<SNAPSHOT_ID>/registry.json
curl --fail --silent --show-error https://<preview-host>/i/<BLOB_ID>.json
```

Confirm that `healthz` is the checked-in static body, the moving root points to
the selected snapshot, the pinned root and indexes are present, and the
snapshot is nonempty. Confirm in the Vercel deployment inspector that there is
no serverless function, rewrite, redirect, or build-time network step. Run the
full local validation command above against the same commit. Attach
`registry.gelstable.com` only after a reviewed bootstrap selection has produced
at least one package index; never attach the production domain to an empty
snapshot.

## DNS and TLS

After preview inspection and the first nonempty publication, add
`registry.gelstable.com` as the Vercel production domain. Follow the DNS records
shown by the Vercel project for the chosen zone (for a delegated subdomain this
is normally a CNAME to Vercel's assigned target). Verify propagation and the
certificate before announcing the endpoint:

```text
dig +short registry.gelstable.com CNAME
curl --fail --silent --show-error --head https://registry.gelstable.com/healthz
```

The TLS certificate must be the Vercel-managed certificate for the exact
production hostname. Do not replace the hostname with an HTTP endpoint or
accept a certificate warning. If DNS is managed outside the organization,
record the owner and expiry/renewal path in the on-call handoff.

## Local legacy capture

Capture and live rehearsal are explicit local operations. They are not build
steps, deployment steps, or validation-workflow steps. The capture destination
is `upstream/packages.geldata.com/legacy-2026-08-bootstrap/`; it must be empty
before capture begins, and `capture` refuses to overwrite an existing
destination.

For an original/live capture, use a scratch repository root and one reviewed
RFC3339 UTC timestamp for the entire attempt:

```text
CAPTURE_REPO="$(mktemp -d)"
uv run gel-registry capture \
  --repo "$CAPTURE_REPO" \
  --captured-at 2026-08-20T12:00:00Z
uv run gel-registry verify-capture-live --repo "$CAPTURE_REPO"
```

The capture command fetches the fixed 24-entry legacy matrix and installs the
complete evidence tree atomically. The live verification command deliberately
re-fetches that evidence for a human-reviewed rehearsal. Review the manifest,
status-dependent metadata, response digests, redirect provenance, and the
absence of partial output before moving evidence into a review branch.

## Offline regeneration from committed evidence

Routine regeneration starts from the committed capture evidence and does not
contact `packages.geldata.com`. To reproduce the checked-in output without
modifying the current checkout, archive the reviewed commit into a scratch
root, normalize the capture, publish the bootstrap snapshot, and validate it:

```text
REPRODUCTION_REPO="$(mktemp -d)"
git archive HEAD | tar -x -C "$REPRODUCTION_REPO"
uv run gel-registry normalize --repo "$REPRODUCTION_REPO"
SNAPSHOT_ID="$(uv run gel-registry publish-bootstrap --repo "$REPRODUCTION_REPO")"
test "$SNAPSHOT_ID" = "0f776b71381237c8"
uv run gel-registry validate --repo "$REPRODUCTION_REPO"
```

The expected bootstrap snapshot is exactly `0f776b71381237c8`. The pinned
`public/s/0f776b71381237c8/registry.json` and every blob under `public/i/` it
references are content-addressed and append-only; any byte difference in them
requires separate review. On the working checkout, the
non-writing checks are:

```text
uv run gel-registry normalize --repo . --check
uv run gel-registry validate --repo .
```

Bootstrap publication may update the pointer and the two moving public
documents, but it must not rewrite an existing pinned snapshot.

## Selection-only rollback

Rollback is a pointer change, not a reconstruction of history:

1. Identify an existing known-good ID in `public/v1/snapshots.json` and verify
   the corresponding `public/s/<snapshot-id>/registry.json` and the
   `public/i/` blobs it references in a fresh checkout.
2. On a review branch, set `pointers/latest.json` to that existing ID and run
   `uv run gel-registry select-snapshot --repo .`.
3. Run `uv run gel-registry validate --repo .`, the full static validation
   command, and the preview inspection. Open a pull request containing only
   the pointer and its two derived moving documents.
4. Merge through the protected branch after approval. Confirm the Vercel
   production deployment serves the selected pinned bytes.

Rollback changes the pointer and regenerates exactly `public/registry.json`
and `public/v1/snapshots.json`; it does not rewrite pinned snapshots. Never
delete or edit `public/s/<snapshot-id>/`, `public/i/`, `bootstrap/`, or frozen
capture evidence as part of a selection rollback. Do not build a new snapshot or
download an artifact to recreate an already pinned snapshot.

## Manual CLI acceptance

After preview inspection, resolve the available server versions through the
existing CLI with the production registry selected explicitly:

```text
GEL_PKG_ROOT=https://registry.gelstable.com gel server list-versions
```

Record the selected index, response status, and result with the preview or
production deployment identifier.

## Digest audit

For a routine audit, clone the exact production commit into a clean directory,
run `uv run gel-registry validate --repo .`, and compare the tracked pinned
tree with the deployment. For each checked-in path, compare the local and
served bytes rather than parsed JSON:

```text
curl --fail --silent https://registry.gelstable.com/s/<SNAPSHOT_ID>/registry.json | shasum -a 256
shasum -a 256 public/s/<SNAPSHOT_ID>/registry.json
curl --fail --silent https://registry.gelstable.com/i/<BLOB_ID>.json | shasum -a 256
shasum -a 256 public/i/<BLOB_ID>.json
```

A blob's filename is the first 32 hex digits of its own SHA-256, so a served
blob can be checked against its URL without a local copy. Repeat for
representative blobs, the moving root, and schemas. Record
the commit, snapshot ID, URL, local digest, served digest, and timestamp. A
digest mismatch blocks publication or triggers the outage procedure; do not
silently normalize or rewrite a pinned file.

## Provider failover and outage triage

There are two independent hosts and two different incidents:

### Registry-host outage

This is an outage of Vercel or the `registry.gelstable.com` DNS/TLS path. The
registry metadata and static health endpoint are unavailable even though the
artifact URLs may still work. Deploy the same reviewed Git commit and
unchanged `public/` directory to the pre-approved alternate static provider,
verify the byte and header checks there, and change DNS only through the
provider's documented cutover. The alternate provider must serve files
verbatim, with no server, functions, rewrites, redirects, or network fetches.
Restore the primary provider after it is healthy and repeat the digest audit.

### Artifact-host outage

This is an outage of `packages.geldata.com` or another host named by an
`installref`. Registry JSON may still be available, while package downloads
fail. Do not roll the registry pointer or edit a pinned index as a response to
an artifact-host outage. Page the artifact/CDN owner, restore the artifact
service or its documented alias, and verify representative installrefs and
their recorded digests before closing the incident. Preserve the registry
snapshot and its audit evidence throughout.

If both hosts are impaired, declare both incidents, preserve the last known
good Git commit and snapshot ID, and coordinate the two provider recoveries
separately. `stale-if-error` is a requested cache policy documented in
`docs/hosting-observations.md`; it is not an availability guarantee or a
substitute for provider failover.
