#!/usr/bin/env bash
# Run as root inside a disposable Rocky container. Argument: fixture URL.
set -euo pipefail
base=${1:?fixture base URL required}
dnf install -y --allowerasing curl gnupg2
curl -fsSLo /tmp/fixture.asc "$base/valid/keys/gelstable.asc"
rpm --import /tmp/fixture.asc
configure() {
  cat > /etc/yum.repos.d/fixture.repo <<SOURCE
[fixture]
name=Signed fixture
baseurl=$base/$1/rpm/stable/\$basearch
enabled=1
gpgcheck=1
repo_gpgcheck=1
gpgkey=file:///tmp/fixture.asc
metadata_expire=0
SOURCE
  dnf clean all
}
configure valid
dnf -y --disablerepo='*' --enablerepo=fixture makecache
dnf -y --disablerepo='*' --enablerepo=fixture install gel-7
test "$(cat /usr/share/gel-7/fixture)" = hello
rpm -q gel-7
for variant in tampered unsigned; do
  configure "$variant"
  if dnf -y --disablerepo='*' --enablerepo=fixture --setopt=fixture.skip_if_unavailable=False makecache > /tmp/rejection.log 2>&1; then
    cat /tmp/rejection.log
    echo "DNF accepted $variant repository" >&2
    exit 1
  fi
  cat /tmp/rejection.log
  grep -Ei 'checksum|signature|repomd.xml.asc' /tmp/rejection.log
  echo "DNF rejected $variant repository"
done
