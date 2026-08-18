# Registry hosting and operations

This runbook describes the production setup for `gelstable/gel-registry`. The
registry is a static, versioned distribution index. Vercel publishes the
checked-in `public/` tree verbatim; it does not run the Python package, fetch
legacy indexes, download release artifacts, or generate registry bytes during a
deployment.

The internal `pointers/latest.json` file is an input to the publication tools
and is never served. `public/s/<snapshot-id>/` and the files beneath it are
immutable history. The moving `/registry.json` and `/v1/snapshots.json` files
are generated from the pointer and are the only moving registry documents.

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
3. In repository Settings, enable Actions only for the checked-in workflows and
   allow only GitHub/verified actions that the workflows pin to full commit
   SHAs. Keep the default `GITHUB_TOKEN` permission read-only.
4. Create the branch protection rule below before accepting registry-data or
   publication pull requests.

## Default-branch protection

Apply these settings to the default branch:

- Require a pull request before merging, at least one independent approval,
  dismissal of stale approvals, and resolution of all conversations.
- Require branches to be up to date before merging and require the exact
  `Registry validation / local` status check from `.github/workflows/validate.yml`.
  Remote release checks remain scoped to the changes that need them, but the
  local check is required for every pull request.
- Restrict who may push to the default branch to the repository maintainers.
  Disable force pushes (**Allow force pushes: disabled**) and disable branch
  deletion (**Allow branch deletion: disabled**).
  Do not permit a workflow token to bypass this rule.
- Enforce the rule for administrators, keep the bypass list empty, and do not
  allow direct commits from a deployment provider. A workflow may prepare a
  branch and pull request; only the protected-branch merge process publishes
  it.

The required local check is reproducible from a clean checkout:

```text
uv run pytest -q && uv run mypy src tests && uv run ruff check . && uv run ruff format --check .
```

The promotion workflow may have job-scoped `contents: write` and
`pull-requests: write` only where it creates its review branch and pull request.
Its workflow-level permissions remain read-only. No workflow uses
`pull_request_target`, a personal access token, or force-push behavior. Review
the Actions audit log after changing permissions.

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
  integration is the only deployment path; repository workflows produce review
  branches and pull requests, not production deployments.

### Preview inspection and production attachment

Before attaching the production domain, inspect a preview deployment at every
available public surface:

```text
curl --fail --silent --show-error --head https://<preview-host>/healthz
curl --fail --silent --show-error https://<preview-host>/registry.json
curl --fail --silent --show-error https://<preview-host>/v1/snapshots.json
curl --fail --silent --show-error https://<preview-host>/s/<SNAPSHOT_ID>/registry.json
curl --fail --silent --show-error https://<preview-host>/s/<SNAPSHOT_ID>/index/<INDEX>.json
```

Confirm that `healthz` is the checked-in static body, the moving root points to
the selected snapshot, the pinned root and indexes are present, and the
snapshot is nonempty. Confirm in the Vercel deployment inspector that there is
no serverless function, rewrite, redirect, or build-time network step. Run the
full local validation command above against the same commit. Attach
`registry.gelstable.org` only after a reviewed bootstrap selection has produced
at least one package index; never attach the production domain to an empty
snapshot.

## DNS and TLS

After preview inspection and the first nonempty publication, add
`registry.gelstable.org` as the Vercel production domain. Follow the DNS records
shown by the Vercel project for the chosen zone (for a delegated subdomain this
is normally a CNAME to Vercel's assigned target). Verify propagation and the
certificate before announcing the endpoint:

```text
dig +short registry.gelstable.org CNAME
curl --fail --silent --show-error --head https://registry.gelstable.org/healthz
```

The TLS certificate must be the Vercel-managed certificate for the exact
production hostname. Do not replace the hostname with an HTTP endpoint or
accept a certificate warning. If DNS is managed outside the organization,
record the owner and expiry/renewal path in the on-call handoff.

## Capture review

The one-time legacy capture is prepared locally on the
`publish/legacy-2026-08-bootstrap` branch. That pull request carries the capture
tool together with the frozen evidence under
`upstream/packages.geldata.com/legacy-2026-08-bootstrap/`, the normalized
`bootstrap/` inputs, and the rendered publication. Review the ordered 24-entry
manifest, status-dependent metadata, response digests, redirect provenance,
and the exact derived output. Require reproducible local validation before
merging.

Routine validation and rendering are offline. Vercel never fetches the legacy
indexes or runs the capture tool during deployment.

## Bootstrap publication and promotion

In the bootstrap publication pull request, inspect the calculated nonempty
snapshot ID, every pinned `public/s/<snapshot-id>/` file,
`pointers/latest.json`, and the generated `public/registry.json` plus
`public/v1/snapshots.json`. Confirm that all pinned bytes are append-only and
that the moving documents reference the selected snapshot. Merge only after
`Registry validation / local` passes and the preview inspection succeeds.

The scheduled or manually dispatched **Promote stable Gel CLI releases**
workflow verifies new release records and opens a pull request. Review the
release provenance, ten canonical assets, digests, installrefs, and the
selection/publication diff. The workflow must not receive credentials from
`gel-cli`; its repository-scoped token is the only writer to its review branch.

## Selection-only rollback

Rollback is a pointer change, not a reconstruction of history:

1. Identify an existing known-good ID in `public/v1/snapshots.json` and verify
   the corresponding `public/s/<snapshot-id>/registry.json` and index files in
   a fresh checkout.
2. On a review branch, set `pointers/latest.json` to that existing ID and run
   `uv run gel-registry select-snapshot --repo .`.
3. Run `uv run gel-registry validate --repo .`, the full static validation
   command, and the preview inspection. Open a pull request containing only the
   pointer and its two derived moving documents.
4. Merge through the protected branch after approval. Confirm the Vercel
   production deployment serves the selected pinned bytes.

Rollback changes the pointer and regenerates exactly `public/registry.json`
and `public/v1/snapshots.json`; it does not rewrite pinned snapshots. Never
delete or edit `public/s/<snapshot-id>/`, `releases/`, `bootstrap/`, or frozen
capture evidence as part of a selection rollback. Do not call snapshot build
or download an artifact to recreate an already pinned snapshot.

## Digest audit

For a routine audit, clone the exact production commit into a clean directory,
run `uv run gel-registry validate --repo .`, and compare the tracked pinned
tree with the deployment. For each checked-in path, compare the local and
served bytes rather than parsed JSON:

```text
curl --fail --silent https://registry.gelstable.org/s/<SNAPSHOT_ID>/registry.json | shasum -a 256
shasum -a 256 public/s/<SNAPSHOT_ID>/registry.json
```

Repeat for representative `index/` files, the moving root, and schemas. Record
the commit, snapshot ID, URL, local digest, served digest, and timestamp. A
digest mismatch blocks publication or triggers the outage procedure; do not
silently normalize or rewrite a pinned file.

## Provider failover and outage triage

There are two independent hosts and two different incidents:

### Registry-host outage

This is an outage of Vercel or the `registry.gelstable.org` DNS/TLS path. The
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
fail. Do not roll the registry pointer, edit a pinned index, or claim that a
selection rollback repairs the artifact host. Page the artifact/CDN owner,
restore the artifact service or its documented alias, and verify representative
installrefs and their recorded digests before closing the incident. Preserve
the registry snapshot and its audit evidence throughout.

If both hosts are impaired, declare both incidents, preserve the last known
good Git commit and snapshot ID, and coordinate the two provider recoveries
separately. `stale-if-error` is a requested cache policy documented in
`docs/hosting-observations.md`; it is not an availability guarantee or a
substitute for provider failover.
