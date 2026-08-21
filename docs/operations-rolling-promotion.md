# Rolling promotion operations

This runbook supplements `docs/operations.md` for the PR2 rolling GitHub
Release flow. The default branch remains the protected source of the static
registry; the promotion workflow creates a reviewable candidate pull request
and never merges or deploys it automatically.

## Required branch protection

Branch protection for the default branch must require the exact status check
`Registry validation / gate` from `.github/workflows/validate.yml`. Do not use
`Registry validation / local` as the required PR2 gate: `local` is the offline
PR1 validation job, while `gate` aggregates the result of the complete
workflow.

The aggregate gate always runs after `scope`, `local`, and `candidate` reach a
terminal result. It requires `scope` and `local` to succeed. The `candidate`
job remains conditional and runs only when the changed-path detector marks a
release path: `sources/`, a release record, `promotion/`, `pointers/`, or
`public/`. For a release-scoped change, the gate requires `candidate` to
succeed. For all other pull requests, `candidate` is expected to be skipped
and the gate accepts that skipped result (it also accepts success if the job
is otherwise run). A failed or unexpectedly skipped candidate therefore cannot
be hidden behind a green local job.

Keep the other protected-branch requirements in place: pull-request review,
up-to-date branches, resolved conversations, no force pushes, no workflow
bypass, and no direct deployment-provider writes.

## Rolling flow

The scheduled or manually dispatched `Promote rolling registry candidate`
workflow performs this sequence:

1. Check out the current default branch and discover eligible releases from
   the versioned `sources/products.json` policy.
2. Verify new releases and record deterministic upstream defects in
   `promotion/blocked.json`. Transport, authentication, attestation-service,
   and runner failures abort the run without rewriting the rolling branch.
3. Build one aggregate candidate containing all existing release records and
   the newly verified records, then validate the allowed promotion paths.
4. Rebuild the fixed `promote/registry` branch from the latest default branch
   and update its single pull request, or close and remove the branch when no
   candidate changes remain.
5. Let the required PR validation workflow reproduce and remotely revalidate
   the exact committed candidate before a maintainer merges it.

The promotion branch is updated with `--force-with-lease`; the workflow
concurrency group is `promote-registry` with cancellation disabled. This keeps
one reviewable rolling pull request while detecting unexpected concurrent
branch changes.

## Workflow permissions

The validation workflow is read-only. Its `scope`, `local`, `candidate`, and
`gate` jobs require only `contents: read`; `candidate` additionally requires
`attestations: read` for GitHub artifact verification.

The promotion workflow has read-only workflow defaults. Its single `promote`
job receives only `contents: write`, `pull-requests: write`, and
`attestations: read`, and uses `GH_TOKEN` for the branch and pull-request
operations. It does not receive deployment credentials, Vercel tokens, or a
permission to bypass default-branch protection. Merging and deployment remain
separate protected operations.

## Operator checks

Before enabling or changing protection, confirm the required check is named
exactly `Registry validation / gate` and that a non-release pull request shows
`candidate` as skipped but `gate` as successful. A release-scoped pull request
must show a completed successful `candidate` check before `gate` can pass.

For a failed run, inspect the first failing job and leave the existing
`promote/registry` branch and pull request untouched unless the promotion
script completed its guarded update. Re-run after fixing the upstream or
infrastructure cause; do not manually edit generated release records or
blocked diagnostics in the rolling branch.
