# Gelstable registry

Static registry source and rendering tools for Gelstable. The committed
`public/` tree serves portable package indexes and signed APT/RPM repository
metadata; native package binaries are hosted in GitHub release assets.

See [native package installation](docs/native-packages.md), the
[operations runbook](docs/operations.md), and
[local native repository development](docs/native-development.md).

The production signing certificate has primary fingerprint
`EBFC46CC958983DD73D6E7D7F7C050A836EDB7EC`; its private key is held in custody.
APT/RPM metadata and RPM packages use its automation signing subkey.

The current certificate and fingerprint are committed under
[`public/keys/`](public/keys/). Production setup and rotation procedures are in
the [runbook](docs/signing-keys.md).

Publisher workflows use the [shared actions](docs/publishing.md).
