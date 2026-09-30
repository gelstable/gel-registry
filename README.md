# Gelstable registry

Static registry source and rendering tools for Gelstable. The committed
`public/` tree serves portable package indexes and signed APT/RPM repository
metadata; native package binaries are hosted in GitHub release assets.

See [native package installation](docs/native-packages.md), the
[operations runbook](docs/operations.md), and
[local native repository development](docs/native-development.md).

**Native packages are pre-production.** The committed repositories are empty.
Their disposable development certificate has primary fingerprint
`F8572499192C6FA4FCF9F41D60925A049A5C41FF`; its private key has been deleted.
This is not the production trust anchor. Before launch, replace the public key,
regenerate its fingerprint, update this README and the installation guide, and
re-sign all APT/RPM metadata with the production signing key. Fresh-system
production smoke and legacy migration acceptance remain unexecuted.

The current certificate and fingerprint are committed under
[`public/keys/`](public/keys/). Production setup and rotation procedures are in
the [runbook](docs/operations.md#key-rotation-and-compromise).
