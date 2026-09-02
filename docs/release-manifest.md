# Release Manifest Specification and Publisher Guide

This document defines how trusted product repositories publish package releases to the Gel Registry.

The Gel Registry operates on a **trusted-publisher model**. Product release workflows are responsible for authoring and verifying package metadata, and publishing that metadata in a single manifest asset named `gel-registry.json` attached to the GitHub release. The registry discovers eligible releases, validates the manifest against its schema, and promotes entries into the distribution index.

## Publisher responsibilities and boundary

The boundary between product release workflows and the registry is strictly defined.

### Product release workflows own product-specific correctness:

- Determining which platforms, architectures, and encodings constitute a complete release.
- Defining package `basename`, `name`, `version`, `slot`, `tags`, and `version_details`.
- Constructing final client-compatible `PackageEntry` decorations.
- Computing exact artifact `size`, `sha256`, and `blake2b` digests.
- Enforcing build provenance, signatures, and supply-chain attestations prior to manifest publication.

### The registry owns registry-wide correctness:

- Ingesting releases only from allowlisted repositories declared in `sources/github.json`.
- Enforcing that every artifact URL names an asset hosted directly on the declaring repository and tag (`https://github.com/<owner>/<repo>/releases/download/<tag>/...`).
- Ensuring rescue digests resolve uniquely against historical mirror records.
- Verifying that package entries satisfy the structural contract and do not conflict with existing identities.
- Ensuring committed release records are canonical and append-only under `releases/<owner>/<repository>/<release-id>.json`.
- Ensuring rendered public distribution snapshots exactly match committed inputs.
- Maintaining the cumulative candidate invariant without losing unmerged releases.

There are **no registry-owned product regexes, version parsing rules, or adapter conventions**. The registry does not infer package metadata from filenames, reconstruct package decoration, or download binary artifacts during promotion.

---

## Publisher Checklist

Product release automation must execute this checklist in order:

```text
build artifacts
compute size, SHA-256, and BLAKE2b
run product-specific platform/coverage tests
produce final PackageEntry values
validate gel-registry.json against release-manifest.json
publish and attest artifacts according to product policy
attach gel-registry.json to the same GitHub release
```

1. **Build artifacts**: Compile binaries and packages for all supported target platforms.
2. **Compute size, SHA-256, and BLAKE2b**: Compute the byte size, 64-character hex SHA-256, and 128-character hex BLAKE2b digests for each artifact.
3. **Run product-specific platform/coverage tests**: Verify test matrices and artifact completeness within the product CI.
4. **Produce final `PackageEntry` values**: Construct complete metadata structures for each package according to client requirements.
5. **Validate `gel-registry.json` against `release-manifest.json`**: Validate the generated JSON payload against the public schema published at `https://registry.gelstable.com/v1/schema/release-manifest.json` (or locally via schema validation).
6. **Publish and attest artifacts according to product policy**: Create the non-draft GitHub release and upload binary assets and attestations.
7. **Attach `gel-registry.json` to the same GitHub release**: Upload the validated manifest asset named exactly `gel-registry.json` to the release.

---

## Manifest Format and Examples

A `gel-registry.json` file adheres to schema version 1 and contains:
- `schema_version`: Must be `1`.
- `replacements`: A list of rescue replacements for historical artifacts.
- `indexes`: A list of channel/platform index fragments containing net-new package entries.

At least one `replacement` or `index` fragment must be present.

### Example 1: Rescue Manifest

A rescue manifest replaces URLs of historical artifacts originally mirrored from `packages.geldata.com` with assets hosted on GitHub Releases. SHA-256 is the sole match key. A rescue updates only matching artifact URLs (and the singular `installref` when selected); it never changes historical package metadata.

All asset URLs must point to assets on the declaring repository and release tag (`https://github.com/<owner>/<repo>/releases/download/<tag>/...`).

```json
{
  "schema_version": 1,
  "replacements": [
    {
      "sha256": "0cb77e14086216cb5bbe56f279b5022207e0b685bfc45f3a650371cde692dd3d",
      "url": "https://github.com/gelstable/gel-cli/releases/download/v1.0.0/edgedb-cli-1.0.0+5724c50.zst"
    },
    {
      "sha256": "a2ed03138ee4d9c005a6c08ed864f914f682fb94932e0fb4eb48378fe76bdac0",
      "url": "https://github.com/gelstable/gel-cli/releases/download/v1.0.0/edgedb-cli-1.0.0+5724c50"
    }
  ],
  "indexes": []
}
```

### Example 2: Net-New Package Manifest

A net-new manifest supplies complete `PackageEntry` structures for packages added to the registry index. Each entry must provide complete version details, revision, build date, architecture, slot, tags, and verified install references.

```json
{
  "schema_version": 1,
  "replacements": [],
  "indexes": [
    {
      "channel": "stable",
      "platform": "x86_64-unknown-linux-musl",
      "packages": [
        {
          "architecture": "x86_64",
          "basename": "gel-cli",
          "build_date": "2026-09-02T09:00:00+00:00",
          "installref": "https://github.com/gelstable/gel-cli/releases/download/v8.0.0/gel-cli-8.0.0+abcdef0",
          "installrefs": [
            {
              "encoding": "identity",
              "ref": "https://github.com/gelstable/gel-cli/releases/download/v8.0.0/gel-cli-8.0.0+abcdef0",
              "type": "application/x-pie-executable",
              "verification": {
                "blake2b": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                "sha256": "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
                "size": 15000000
              }
            },
            {
              "encoding": "zstd",
              "ref": "https://github.com/gelstable/gel-cli/releases/download/v8.0.0/gel-cli-8.0.0+abcdef0.zst",
              "type": "application/x-pie-executable",
              "verification": {
                "blake2b": "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210",
                "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                "size": 5000000
              }
            }
          ],
          "name": "gel-cli",
          "revision": "202609020900",
          "slot": "",
          "tags": {},
          "version": "8.0.0+abcdef0",
          "version_details": {
            "major": 8,
            "metadata": {
              "build_hash": "abcdef0",
              "build_revision": "202609020900"
            },
            "minor": 0,
            "patch": 0,
            "prerelease": []
          },
          "version_key": "8.0.0.202609020900"
        }
      ]
    }
  ]
}
```

---

## Schema Reference

The public JSON Schema for validating `gel-registry.json` is published at:

```text
https://registry.gelstable.com/v1/schema/release-manifest.json
```

Locally within this repository, the schema can be rendered using:

```bash
uv run python -c "from pathlib import Path; from gel_registry.render.schemas import render_schemas; render_schemas(Path('.'))"
```
