# Gelstable registry

Static registry source and rendering tools for Gelstable. The committed
`public/` tree serves portable package indexes and signed APT/RPM repository
metadata; native package binaries are hosted in GitHub release assets.

See [native package installation](docs/native-packages.md), the
[operations runbook](docs/operations.md), and
[local native repository development](docs/native-development.md).

**Native packages are pre-production.** The committed repositories are empty.
Their production signing certificate has primary fingerprint
`EBFC46CC958983DD73D6E7D7F7C050A836EDB7EC`; its private key is held in custody.
All APT/RPM metadata is signed with its automation signing subkey. Fresh-system
production smoke and legacy migration acceptance remain unexecuted.

The current certificate and fingerprint are committed under
[`public/keys/`](public/keys/). Production setup and rotation procedures are in
the [runbook](docs/operations.md#key-rotation-and-compromise).
