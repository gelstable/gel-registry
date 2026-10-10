# Rendering native repositories

For client installation, see [native packages](native-packages.md). Production
release, signing-key rotation and acceptance procedures are in the
[operations runbook](operations.md#native-package-operations). Local fixtures use
disposable keys; the committed production certificate's private key is held in
signing custody and is not available from the checkout. Signing newly rendered
metadata requires the authorized automation subkey.

Run `gel-registry native render --repo . --cache /tmp/gel-native-cache --out public`.
The renderer downloads and verifies only newly seen native assets, validates
ownership and RPM signatures, and extracts their full metadata using standard
distribution tools. It retains Debian stanzas and RPM primary/filelists/other
package records in `native/package-metadata.json`, keyed by SHA-256. Routine
promotions merge these records into complete client indexes; old package bytes
are not needed. Existing repositories bootstrap this retained metadata once.
The signing public key must be available at `public/keys/gelstable.asc`.
Records are committed and reviewed, including each RPM's signature issuer.
Certificate updates never cause package downloads: validation resolves recorded
issuers against current signing subkeys. Every render composes the whole tree;
sorted entries, fixed timestamps and retained unchanged Release dates make it
byte-identical when inputs do not change. Git status decides whether to publish.
Git restores an interrupted render; there is no separate recovery state.

`--base-url` defaults to `https://registry.gelstable.com` and controls the RPM
package URL base. For example, use `--base-url http://localhost:8000` when testing
a local server. APT uses paths relative to the configured APT repository root.

The lock is a sorted JSON array. Each entry contains `channel`, `format` (`deb`
or `rpm`), `name`, full `version` (including epoch and revision), native `arch`,
`sha256`, `size`, and a format-root-relative `path` such as
`pool/gel-cli/pkg-gel-cli-7.1-1/gel-cli-7.1-1-amd64.deb`.
If records are deleted, the next render downloads only those packages again.
Restore records from Git to retain withdrawn identity history. Both channels and
architectures are generated even when empty.

To withdraw an asset, add `{ "sha256": "<digest>", "reason": "<reason>" }` to
`native/yanked.json`, a JSON array. Downloads and package validation failures
abort rendering; they are never recorded as release rejections. Yank digests must
be exactly 64 lowercase hexadecimal characters. Lock and yank inputs must be
regular files with no symlinks in their paths.

## Signing and offline validation

Check unsigned structure first with
`gel-registry native validate --unsigned --repo .`. This requires no secrets or
signatures and checks records, lock, package indexes, checksums and inventory.
The privileged promotion job runs it before importing a signing secret;
`native sign` also runs it before writing any signature.

After rendering, set `GNUPGHOME` to the signing key home and
`GELSTABLE_SIGNING_FPR` to the full signing subkey fingerprint, then run
`gel-registry native sign --repo .`. The command preserves signatures that verify with the committed certificate,
and signs missing or invalid APT Release signatures as
InRelease and Release.gpg, and RPM repomd.xml as repomd.xml.asc, using SHA512.
It immediately verifies each signature against `public/keys/gelstable.asc`.

`gel-registry validate --repo .` checks these signatures, metadata checksums,
package sets, the lock against release records after yanks, and unexpected
repository files. Validation needs only public metadata and the public key;
it does not download packages or read the signing key home. Package name and
version identities are checked during rendering; offline checks also compare
lock names, full versions, and architectures against signed APT/RPM metadata.
Release records bind artifact URLs, digests and sizes without package binaries;
lock channels are derived from package versions.

## Local Linux tests

The fixture suite builds real packages with nFPM and generates disposable signing
keys. Missing tools skip these tests locally. `REQUIRE_NATIVE_TOOLS=1` makes
missing tools fail, as in CI. Run the suite in a disposable Ubuntu container
from the repository root:

```sh
docker run --rm -v "$PWD":/w -w /w ubuntu:24.04 bash -ceu '
  apt-get update
  apt-get install -y apt-utils createrepo-c rpm gnupg curl ca-certificates
  case "$(uname -m)" in
    aarch64) arch=arm64; digest=1c0f5f2999b9a974bfb04fdb0cc3306096de530ac5dbb25d739cc5f5219c919c ;;
    x86_64) arch=x86_64; digest=0660ca602b2d2d2ae4781a06c692b3eeb9d437ffea05b831d76e41f4a3188783 ;;
  esac
  curl -fsSL "https://github.com/goreleaser/nfpm/releases/download/v2.47.0/nfpm_2.47.0_Linux_${arch}.tar.gz" -o /tmp/nfpm.tar.gz
  echo "$digest  /tmp/nfpm.tar.gz" | sha256sum --check
  tar -xzf /tmp/nfpm.tar.gz -C /usr/local/bin nfpm
  curl -LsSf https://astral.sh/uv/0.11.6/install.sh | sh
  UV_PROJECT_ENVIRONMENT=/tmp/venv REQUIRE_NATIVE_TOOLS=1 /root/.local/bin/uv run pytest -q -m native_tools
'
```

The root-container suite includes an `apt-get update` and installation of a tiny
fixture package, then purges that fixture. Run it only in a disposable container.

Certificate-change PR validation uses `validate --base MERGE_BASE_TREE`. If
current trust rejects an old metadata signature, only exact unchanged base
metadata, lock and package records may verify with the merge-base certificate.
Live RPM signer checks still use the new certificate. This permits the ordinary
certificate/yank PR; promotion and standalone validation require signatures
under current trust and replace revoked signatures before publishing.
