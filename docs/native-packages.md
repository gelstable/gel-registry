# Rendering native repositories

Run `gel-registry native render --repo . --cache /tmp/gel-native-cache --out public`.
The renderer downloads and verifies native assets from release records, validates
package ownership and RPM signatures, and generates APT and RPM metadata with
standard distribution tools. The signing public key must be available at
`public/keys/gelstable.asc`. Only metadata and `native/packages.lock.json` are
written to the repository; package blobs remain in the cache.

`--base-url` defaults to `https://registry.gelstable.com` and controls the RPM
package URL base. For example, use `--base-url http://localhost:8000` when testing
a local server. APT uses paths relative to the configured APT repository root.

The lock is a sorted JSON array. Each entry contains `channel`, `format` (`deb`
or `rpm`), `name`, full `version` (including epoch and revision), native `arch`,
`sha256`, `size`, and a format-root-relative `path` such as
`pool/gel-cli/pkg-gel-cli-7.1-1/gel-cli-7.1-1-amd64.deb`.
A matching committed lock leaves metadata untouched. Remove the lock to request
a fresh render. Both channels and architectures are generated on the first
render, including empty repositories.

To withdraw an asset, add `{ "sha256": "<digest>", "reason": "<reason>" }` to
`native/yanked.json`, a JSON array. Downloads and package validation failures
abort rendering; they are never recorded as release rejections.

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
