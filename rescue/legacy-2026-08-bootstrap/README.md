# Legacy release rescue: August 2026

Status: **executed and verified in production** on `2026-09-13`.

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

All releases were complete drafts before publication, and the final repeat dry run reported 14 reused drafts, 169 matching assets, and zero uploads. Draft creation and reconciliation used executor commit `5f0e5a6579777d31db17a6213667eba32a7d01ea`.

Published releases:

| Repository | Release ID | Tag | Published at |
| --- | ---: | --- | --- |
| `gelstable/gel` | `387551366` | `gel-ls-v8.0-dev.9812+e773536` | `2026-09-13T14:42:43Z` |
| `gelstable/gel` | `387574735` | `v5.6` | `2026-09-13T14:42:44Z` |
| `gelstable/gel` | `387576229` | `v5.7` | `2026-09-13T14:42:44Z` |
| `gelstable/gel` | `387577516` | `v5.8` | `2026-09-13T14:42:45Z` |
| `gelstable/gel` | `387578793` | `v6.10` | `2026-09-13T14:42:46Z` |
| `gelstable/gel` | `387580052` | `v6.11` | `2026-09-13T14:42:47Z` |
| `gelstable/gel` | `387581085` | `v6.9` | `2026-09-13T14:42:48Z` |
| `gelstable/gel` | `387582543` | `v7.0` | `2026-09-13T14:42:48Z` |
| `gelstable/gel` | `387583701` | `v7.1` | `2026-09-13T14:42:49Z` |
| `gelstable/gel-cli` | `387584678` | `v7.10.0` | `2026-09-13T14:18:35Z` |
| `gelstable/gel-cli` | `387584809` | `v7.10.1` | `2026-09-12T16:16:10Z` |
| `gelstable/gel-cli` | `387622580` | `v7.10.2` | `2026-09-13T14:18:36Z` |
| `gelstable/gel-postgis` | `387622830` | `legacy-gel-server-6-ext-postgis` | `2026-09-13T14:50:53Z` |
| `gelstable/gel-postgis` | `387624038` | `legacy-gel-server-7-ext-postgis` | `2026-09-13T14:50:54Z` |

Promotion record:

| Batch | Promotion PR | Merge commit | Snapshot |
| --- | --- | --- | --- |
| CLI canary `v7.10.1` | [#10](https://github.com/gelstable/gel-registry/pull/10) | `035baeb4c9aca797a7cf77019682221468c329eb` | `10e0b32852eb0553` |
| Remaining CLI | [#12](https://github.com/gelstable/gel-registry/pull/12) | `aac5798277f0af413285b3332da90f658b164e6d` | `070dc956c0a5d9c3` |
| Server and language server | [#13](https://github.com/gelstable/gel-registry/pull/13) | `ae4babe95616cf120a16f5b20ee11549af15f40c` | `da3ef09b0d0a5f89` |
| PostGIS | [#14](https://github.com/gelstable/gel-registry/pull/14) | `7ab96812c83ebf69c676f8b2a3aaf822d3491ef8` | `f9e4884e4eb26e87` |

Each promotion passed registry validation and its Vercel preview before merge. Production verification on `2026-09-13` confirmed that `public/v1/snapshots.json` selected `f9e4884e4eb26e87` and retained the bootstrap and four rollout snapshots. The canary verification also confirmed that all five affected platform indexes exposed the digest-matched GitHub download URLs.

See the [rescue runbook](../../docs/operations.md#legacy-rescue-runbook) and [rolling promotion runbook](../../docs/rolling-promotion.md).
