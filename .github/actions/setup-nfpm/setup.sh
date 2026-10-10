#!/usr/bin/env bash
set -euo pipefail
version=2.47.0
case "$(uname -m)" in
  x86_64) arch=x86_64; digest=0660ca602b2d2d2ae4781a06c692b3eeb9d437ffea05b831d76e41f4a3188783 ;;
  aarch64|arm64) arch=arm64; digest=1c0f5f2999b9a974bfb04fdb0cc3306096de530ac5dbb25d739cc5f5219c919c ;;
  *) echo 'unsupported nFPM runner architecture' >&2; exit 1 ;;
esac
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
curl -fsSL "https://github.com/goreleaser/nfpm/releases/download/v${version}/nfpm_${version}_Linux_${arch}.tar.gz" -o "$work/nfpm.tar.gz"
printf '%s  %s\n' "$digest" "$work/nfpm.tar.gz" | sha256sum --check
tar -xzf "$work/nfpm.tar.gz" -C "$work" nfpm
install -d "$RUNNER_TEMP/nfpm-bin"
install -m 755 "$work/nfpm" "$RUNNER_TEMP/nfpm-bin/nfpm"
printf '%s\n' "$RUNNER_TEMP/nfpm-bin" >> "$GITHUB_PATH"
