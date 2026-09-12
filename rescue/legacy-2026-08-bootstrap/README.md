# Legacy release rescue: August 2026

Status: **not yet executed**.

Capture: `legacy-2026-08-bootstrap`, recorded at `2026-08-31T00:00:00Z`. The original 24 files are preserved under [`upstream/packages.edgedb.com/legacy-2026-08-bootstrap/`](../../upstream/packages.edgedb.com/legacy-2026-08-bootstrap/). The capture has not been rerun.

Generator commit: `5468a9b303ddfadfc66c8eb65d6ab27d204e7499` ([tooling PR #6](https://github.com/gelstable/gel-registry/pull/6)).

Canonical plan: [`plan.json`](plan.json).

Plan SHA-256: `2b4b98ad6d4715c148be6d693217f053e32aed1e5b41a710fa8e18d1a5a84075`.

Generated from the preserved capture and upstream Git tags using:

```sh
uv run gel-registry rescue-plan --repo . --capture legacy-2026-08-bootstrap --bulk > rescue/legacy-2026-08-bootstrap/plan.json
```

The plan contains 14 releases, 155 binary assets, and 155 unique SHA-256 digests. Execution also uploads one `gel-registry.json` manifest per release, for 169 total uploads when starting from empty destinations.

| Repository | Releases | Binary assets |
| --- | ---: | ---: |
| `gelstable/gel` | 9 | 98 |
| `gelstable/gel-cli` | 3 | 15 |
| `gelstable/gel-postgis` | 2 | 42 |

Planned destinations:

| Repository | Tag | Binary assets |
| --- | --- | ---: |
| `gelstable/gel` | `gel-ls-v8.0-dev.9812+e773536` | 2 |
| `gelstable/gel` | `v5.6` | 12 |
| `gelstable/gel` | `v5.7` | 12 |
| `gelstable/gel` | `v5.8` | 12 |
| `gelstable/gel` | `v6.10` | 12 |
| `gelstable/gel` | `v6.11` | 12 |
| `gelstable/gel` | `v6.9` | 12 |
| `gelstable/gel` | `v7.0` | 12 |
| `gelstable/gel` | `v7.1` | 12 |
| `gelstable/gel-cli` | `v7.10.0` | 5 |
| `gelstable/gel-cli` | `v7.10.1` | 5 |
| `gelstable/gel-cli` | `v7.10.2` | 5 |
| `gelstable/gel-postgis` | `legacy-gel-server-6-ext-postgis` | 34 |
| `gelstable/gel-postgis` | `legacy-gel-server-7-ext-postgis` | 8 |

All releases must first be complete drafts, and a repeat dry run must report zero uploads before publication begins. Publish the CLI `v7.10.1` canary, then the remaining CLI, server/language-server, and PostGIS batches through the ordinary rolling promotion PR workflow. See the [rescue runbook](../../docs/operations.md#legacy-rescue-runbook) and [rolling promotion runbook](../../docs/rolling-promotion.md).

Execution details, GitHub release IDs, promotion PRs and merge commits, snapshot IDs, and production verification will be recorded here after rollout. No release or published registry index is changed by committing these inputs.
