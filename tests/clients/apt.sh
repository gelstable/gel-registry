#!/usr/bin/env bash
# Run as root inside a disposable Debian/Ubuntu container. Argument: fixture URL.
set -euo pipefail
base=${1:?fixture base URL required}
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y curl ca-certificates gnupg
install -d /etc/apt/keyrings
curl -fsSLo /etc/apt/keyrings/fixture.asc "$base/valid/keys/gelstable.asc"
source_file=/etc/apt/sources.list.d/fixture.sources
configure() {
  cat > "$source_file" <<SOURCE
Types: deb
URIs: $base/$1/apt
Suites: ${2:-stable}
Components: main
Signed-By: /etc/apt/keyrings/fixture.asc
SOURCE
  rm -rf /var/lib/apt/lists/*
}
update() {
  apt-get -o Dir::Etc::sourcelist="$source_file" -o Dir::Etc::sourceparts=- \
    -o APT::Update::Error-Mode=any update
}
configure valid
update
apt-get install -y gel-7
test "$(cat /usr/share/gel-7/fixture)" = hello
dpkg-query -W gel-7
for variant in tampered unsigned; do
  configure "$variant"
  if update > /tmp/rejection.log 2>&1; then
    cat /tmp/rejection.log
    echo "APT accepted $variant repository" >&2
    exit 1
  fi
  cat /tmp/rejection.log
  grep -Ei 'Hash Sum mismatch|File has unexpected size|not signed' /tmp/rejection.log
  echo "APT rejected $variant repository"
done

configure prerelease testing
update
apt-get install -y gel-7
test "$(dpkg-query -W -f='${Version}' gel-7)" = '1:7.2~rc.1-1'
configure final testing
update
apt-get install -y gel-7
test "$(dpkg-query -W -f='${Version}' gel-7)" = '1:7.2-1'
echo 'APT testing-only prerelease to final upgrade passed'
