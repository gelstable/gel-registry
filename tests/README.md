# Registry behavior tests

These tests describe the registry's inputs, published bytes, and operational
failure boundaries. Start with the complete flows below; the smaller suites
exercise adversarial inputs and recovery states around those flows.

| Behavior | Start here | Invariants protected |
| --- | --- | --- |
| Capture and normalize upstream evidence | `test_legacy_capture.py`, `test_normalization.py`, `test_reproduction.py` | Exact captured bytes, approved origins, complete evidence, reproducible immutable history, no partial installation on failure |
| Discover and compose releases | `test_release_gathering.py`, `test_candidate_build.py`, `test_combined_composition.py` | Releases bound to their repository, tag and asset inventory; invalid releases isolated; identity conflicts stop composition |
| Publish a portable snapshot | `test_publication_transaction.py`, `test_offline_validation.py` | Moving documents select one immutable snapshot, unchanged indexes share blobs, drift is rejected offline, publication is idempotent and failures preserve the previous tree |
| Update the promotion PR | `test_rolling_promotion.py`, `native/test_promotion_boundary.py` | Fresh-main reconciliation, lease-protected pushes, allowed paths only, stale or unsafe artifacts rejected, secret-free rendering and cache-free publication |
| Rescue existing artifacts | `test_rescue_selection.py`, `test_rescue_operations.py`, `test_rescue_transfer.py` | Select actual available builds, verify source and uploaded bytes, preserve draft ownership, resume matching uploads, report ambiguous remote state without unsafe retries |
| Publish native packages | `test_native_manifest.py`, `native/test_render.py`, `native/test_incremental.py` | Publisher contract compatibility, real package identity and signature checks, deterministic indexes, stable included in testing, retained metadata avoids re-fetches, withdrawn identities stay reserved |
| Sign and validate native repositories | `native/test_sign_validate.py`, `native/test_metadata_integrity.py` | Package/index/lock consistency, auxiliary metadata integrity, current signing subkeys, signature reuse, revoked signer withdrawal and recovery |
| Shared publisher actions | `actions/test_publishers.py`, `native/test_shared_signer.py` | Immutable revision allocation across combined and native-only releases, source-specific build selection, final-byte manifests, verified uploads, safe draft cleanup |
| Hosting and custody | `test_hosting.py`, `test_signing_key_script.py`, `test_signing_secrets.py` | Static route/cache behavior, production hostname, isolated key ceremony and recovery, private material never printed |
| Real client acceptance | `clients/apt.sh`, `clients/dnf.sh` | Signed installation, tamper/unsigned rejection, testing-only prerelease-to-final upgrades |

Run the Python suite with `uv run pytest -q`. Native cases use real Linux package
tools and a disposable signing key. Follow
[the native development instructions](../docs/native-development.md) for the
Linux environment and client scripts. Set `REQUIRE_NATIVE_TOOLS=1` for the full
run so missing tools cause failure rather than silently skipping acceptance.

When adding a test, name the behavior and assert a consumer-visible result:
published bytes, selected package versions, validation failure, preserved state,
or refused external mutation. Mock external I/O when needed, keeping composition,
publication and validation real. Parameterize distinct failure classes rather
than every spelling of the same case. Avoid tests of private helpers, module
layout, exact subprocess options, serialization algorithms, or delegation.
