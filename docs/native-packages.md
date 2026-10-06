# Install Gel with APT or DNF

**Pre-production:** the committed repositories are empty and signed with the
production certificate backed up in signing custody. Production installation
and legacy migration acceptance have not been executed. The commands below
describe installation after package publication; they are not evidence of a
working production service today.

The intended supported systems are Debian 12 and 13, Ubuntu 22.04, 24.04 and
26.04, and Rocky Linux 9, on x86_64 and aarch64. APT calls these architectures
`amd64` and `arm64`; DNF calls them `x86_64` and `aarch64`. Each distribution,
architecture and channel is covered by the configured production smoke matrix,
but acceptance on fresh systems remains pending.

## Verify the repository key

The production primary key fingerprint is:

```text
EBFC46CC958983DD73D6E7D7F7C050A836EDB7EC
```

The armored certificate is published at
`https://registry.gelstable.com/keys/gelstable.asc`, with its full primary
fingerprint at `/keys/gelstable.fingerprint`. At launch, compare the downloaded
key with the production fingerprint published here and in the registry README.
Stop if they disagree.

## APT: Debian and Ubuntu

Install prerequisites, download the key, and inspect its primary fingerprint:

```sh
sudo apt-get update
sudo apt-get install -y curl ca-certificates gnupg
sudo install -d -m 0755 /etc/apt/keyrings
sudo curl -fsSLo /etc/apt/keyrings/gelstable.asc https://registry.gelstable.com/keys/gelstable.asc
sudo chmod 0644 /etc/apt/keyrings/gelstable.asc
gpg --show-keys --with-fingerprint /etc/apt/keyrings/gelstable.asc
```

After verifying the fingerprint, configure stable and install Gel 7:

```sh
sudo curl -fsSLo /etc/apt/sources.list.d/gelstable.sources https://registry.gelstable.com/gelstable.sources
sudo apt-get update
sudo apt-get install gel-7
```

The source file contains:

```text
Types: deb
URIs: https://registry.gelstable.com/apt
Suites: stable
Components: main
Signed-By: /etc/apt/keyrings/gelstable.asc
```

These APT versions read armored `.asc` keys directly. Trust is scoped with
`Signed-By`; do not use `apt-key` or disable signature checking.

## DNF: Rocky Linux

Download and inspect the same key, then import it after checking the fingerprint:

```sh
sudo dnf install curl ca-certificates gnupg2
curl -fsSLo /tmp/gelstable.asc https://registry.gelstable.com/keys/gelstable.asc
gpg --show-keys --with-fingerprint /tmp/gelstable.asc
sudo rpm --import /tmp/gelstable.asc
sudo curl -fsSLo /etc/yum.repos.d/gelstable.repo https://registry.gelstable.com/gelstable.repo
sudo dnf install gel-7
```

Run the import and installation only after fingerprint verification. The repo
file contains:

```ini
[gelstable]
name=Gelstable
baseurl=https://registry.gelstable.com/rpm/stable/$basearch
enabled=1
gpgcheck=1
repo_gpgcheck=1
gpgkey=https://registry.gelstable.com/keys/gelstable.asc
```

Both RPM packages and repository metadata must pass signature checks. Confirm
any additional DNF key-import prompt matches the verified certificate.

## Packages and testing channel

`gel-7` installs the Gel 7 server and CLI. The corresponding packages are
`gel-server-7` and `gel-cli`. Install `gel-server-7-ext-postgis` to use the
PostGIS extension. Native server packages use **bundled libraries** instead of
relying on distribution versions of their private runtime libraries. Server
and extension updates come through these packages; keep them updated together.

APT testing uses a second source file:

```sh
sudo curl -fsSLo /etc/apt/sources.list.d/gelstable-testing.sources https://registry.gelstable.com/gelstable-testing.sources
sudo apt-get update
sudo apt-get install -t testing gel-7 gel-server-7-ext-postgis
```

With both suites enabled, APT may choose a newer testing version during ordinary
upgrades. Use testing on a disposable system or configure APT pinning if you
need package-specific selection. To stop receiving testing updates, remove
`/etc/apt/sources.list.d/gelstable-testing.sources` and run `apt-get update`.

DNF testing is disabled by default and enabled explicitly for a transaction:

```sh
sudo curl -fsSLo /etc/yum.repos.d/gelstable-testing.repo https://registry.gelstable.com/gelstable-testing.repo
sudo dnf --enablerepo=gelstable-testing install gel-7 gel-server-7-ext-postgis
```

The testing repository has `enabled=0`, `gpgcheck=1` and `repo_gpgcheck=1`.
Removing testing configuration does not downgrade installed packages. Wait for
a higher stable version or plan an explicit downgrade after checking data
compatibility.

## Migrate from packages.geldata.com

Back up the database and record the installed package versions before upgrading.
Locate the legacy repository configuration:

```sh
# Debian / Ubuntu
sudo grep -R -n 'packages.geldata.com' /etc/apt/sources.list /etc/apt/sources.list.d
# Rocky Linux
sudo grep -R -n 'packages.geldata.com' /etc/yum.repos.d
```

Remove the legacy source file or only its entries if the file contains other
repositories. Do not remove Gel packages or their data. Follow the stable setup
above, including fingerprint verification, then review and apply the upgrade:

```sh
# Debian / Ubuntu
sudo apt-get update
sudo apt-get --simulate full-upgrade
sudo apt-get full-upgrade
# Rocky Linux
sudo dnf upgrade --assumeno
sudo dnf upgrade
```

Review removals and replacements before accepting. APT needs `full-upgrade`
because replacing the legacy `edgedb-cli` transitional package involves a
removal that ordinary `upgrade` can hold back. New package versions carry epoch
`1` (`1:<version>-<revision>`), which makes them upgrade candidates over legacy
packages. Data under `/var/lib/gel`, the `gel` system user, and existing service
unit names are preserved. Check the service and query existing data after the
upgrade. The production migration acceptance that verifies retained data is
still pending; see the [operator runbook](operations.md#native-production-acceptance).

For signing failures, stop and check the fingerprint and the operator advisory.
Never bypass APT authentication, `gpgcheck`, or `repo_gpgcheck` to finish an install.

Repository maintainers can use the [operations runbook](operations.md#native-package-operations)
and [local development guide](native-development.md).
