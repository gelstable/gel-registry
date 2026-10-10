# Shared native publisher actions

Product workflows use composite actions from `gelstable/gel-registry`, pinned
by full commit SHA. The actions run on Linux; callers install Python 3.13, uv,
gh, GnuPG and RPM tools (and dpkg-deb for publication) as needed. Calls that use
the GitHub API receive `GH_TOKEN` from the calling job. Publishing needs
`contents: write`; planning needs only read access.

| Action | Inputs | Result |
| --- | --- | --- |
| `setup-nfpm` | none | Checksum-verified nFPM on PATH, x86_64 or arm64 |
| `native-plan` | `product`, `source_tag`, `repack`, optional `registry_url` | `plan`: JSON array, empty for an already packaged version |
| `sign-rpms` | `directory` | RPMs signed and verified with public trust only |
| `native-manifest` | `directory`, `repository`, `tag` | `native`: JSON section for the combined release manifest |
| `check-manifest` | `file` | Local v2 contract validation; no published-asset inventory check |
| `publish-native-release` | `directory`, `tag` | Validated native-only release, API digest/size checks before publication |

The CLI calls setup, signing, manifest and checking in its existing release
workflow. Its revision stays 1; repack by cutting a CLI patch release.
Server and PostGIS dispatch jobs call planning, setup, signing and publication.
Their reusable workflow returns signed packages to the caller, which merges the
native section into its combined manifest. PostGIS planning resolves ambiguous
legacy assets through stable portable indexes and selects the matching server
slot's newest portable build for `gel-load-ext`.

`native-plan` outputs items with `slot`, `version`, `native_version`, `revision`,
`tag`, and `sources` keyed by `x86_64`/`aarch64`. Each source has `url`, `sha256`,
and `size`; PostGIS also has `server_sources`. Verify digest and size before
unpacking. Published revisions count; drafts do not.

Signing jobs run in the caller's `package-signing` Environment and pass its
`PACKAGE_SIGNING_KEY` and full `PACKAGE_SIGNING_FPR` as environment variables.
The shared signer removes its temporary key home on exit.
Publication refuses an existing tag and deletes its own draft on failure.
It inspects package versions to mark prereleases, so filenames never select
registry channels.

Example pin (replace with the reviewed registry commit):

```yaml
- uses: gelstable/gel-registry/.github/actions/native-plan@FULL_COMMIT_SHA
  id: native
  with:
    product: server
    source_tag: v7.1
    repack: false
```

To bump a pin, review the registry action diff and contract changes, replace the
full SHA in every product caller, run its fixture and workflow checks, and test
the calling workflow in a fork with disposable signing material. The checker
uses the contract at the same pinned commit, so no vendored schema is needed.
