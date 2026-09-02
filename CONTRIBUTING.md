# Contributing

`gel-registry` is a static, versioned distribution index. The tree under
`public/` is served verbatim. There is no build step, no server, and no network
fetch at deployment time. Most of the constraints below follow from that.

This document is for contributors without commit access. Maintainer procedures
are in `docs/maintainers.md`; the production runbook is in
`docs/operations.md`.

## Setup

If you use Nix, `nix develop` gives you the pinned toolchain. It is not
required for a source or documentation change.

Python tooling is [uv](https://docs.astral.sh/uv/).

```text
uv sync --locked
```

Use `--locked`. An unlocked sync can resolve different dependency versions than
CI and produce results that do not reproduce.

## Checks

Run these before opening a pull request. CI runs the same set.

```text
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest -q
```

If your change touches the registry tree, also run the non-writing registry
checks:

```text
uv run gel-registry normalize --repo . --check
uv run gel-registry validate --repo .
```

## What a pull request may change

Source under `src/`, tests, documentation, and tooling work the way they do in
any repository.

Some paths are append-only or frozen. A pull request that edits an existing
byte under them will be rejected regardless of intent.

`public/s/<snapshot-id>/` and `public/i/` are immutable published history.
Every pinned file is content-addressed and served with an `immutable` cache
header promising it resolves forever. Add new files; never edit or delete
existing ones, including when no current snapshot references them.

`bootstrap/` and `upstream/` hold frozen capture evidence.

`pointers/latest.json` selects which snapshot is live. Changing it is a
publication or a rollback, and a maintainer operation.

`public/registry.json` and `public/v1/snapshots.json` are generated from the
pointer. Regenerate them rather than hand-editing them.

Changes that add a Vercel Function, a rewrite, a redirect, a build command, or
any build-time network fetch will not be accepted. These are load-bearing for
the registry's integrity model, not stylistic preferences.

## Package publishing & registry content

The registry operates on a **trusted-publisher model**. Package metadata and releases are not added through manual pull requests to this repository.

### Product Publishers

If you are a maintainer of a trusted product repository (such as `gelstable/gel` or `gelstable/gel-cli`):
- Package entries and rescue replacements are declared by attaching a validated `gel-registry.json` manifest asset directly to your GitHub release.
- See [`docs/release-manifest.md`](docs/release-manifest.md) for full publisher instructions, the publisher checklist, schema validation, and complete manifest examples.

### Rolling Promotion

The registry automatically discovers non-draft releases bearing `gel-registry.json` from allowlisted repositories and maintains a cumulative rolling pull request (`promote/registry`).
- See [`docs/rolling-promotion.md`](docs/rolling-promotion.md) for runbook details on how rolling candidates are gathered, validated, and merged.
- Registry maintainers review and merge this rolling pull request; manual commits or pull requests modifying index bytes directly are rejected.

If registry content appears incorrect or an upstream release was rejected, inspect the diagnostic summary on the rolling PR or open an issue rather than submitting a pull request editing published index bytes.

## Infrastructure

Hosting infrastructure is Terraform under `infra/`, described in
`docs/infrastructure.md`. Without credentials you can still run the offline
checks:

```text
terraform -chdir=infra fmt -check
terraform -chdir=infra validate
```

`terraform plan` requires HCP Terraform access and will not run on a pull
request from a fork. This is intended. A maintainer will produce the plan and
post it on the pull request.

## Pull requests

Keep a pull request to one concern. A registry pointer change, an
infrastructure change, and a source change belong in three pull requests; each
carries a different reviewer obligation.

Every pull request needs the `Registry validation / local` check passing, at
least one maintainer approval, an up-to-date branch, and all conversations
resolved. Preview deployments are enabled, so a reviewer can inspect the exact
tree your branch would publish.
