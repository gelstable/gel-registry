#!/usr/bin/env bash
# Manual launch acceptance: run only in a fresh Debian 12 or Rocky 9 container.
set -euo pipefail
source "$(dirname "$0")/smoke.sh" stable
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y curl ca-certificates gnupg util-linux
  install -d /etc/apt/keyrings
  curl -fsSLo /etc/apt/keyrings/legacy.gpg https://packages.geldata.com/keys/gel-keyring.gpg
  . /etc/os-release
  echo "deb [signed-by=/etc/apt/keyrings/legacy.gpg] https://packages.geldata.com/apt $VERSION_CODENAME main" > /etc/apt/sources.list.d/legacy.list
  apt-get update
  apt-get install -y gel-7
else
  dnf install -y --allowerasing curl util-linux
  curl -fsSLo /etc/yum.repos.d/legacy.repo https://packages.geldata.com/rpm/gel-rhel.repo
  dnf install -y gel-7
fi
if command -v dpkg-query >/dev/null; then
  old_version=$(dpkg-query -W -f='${Version}' gel-server-7)
else
  old_version=$(rpm -q --qf '%{EPOCHNUM}:%{VERSION}-%{RELEASE}' gel-server-7)
fi
echo "Legacy server: $old_version"
data=/var/lib/gel/native-migration
runstate=/run/gel-native-migration
install -d -o gel -g gel "$data" "$runstate"
runuser -u gel -- gel-server-7 --data-dir "$data" --runstate-dir "$runstate" --bootstrap-only --security insecure_dev_mode
start_server
trap 'stop_server' EXIT
query 'create type MigrationProbe { required property value -> str; };'
query "insert MigrationProbe { value := 'before-upgrade' };"
stop_server
trap - EXIT
rm -f /etc/apt/sources.list.d/legacy.list /etc/yum.repos.d/legacy.repo
configure
if command -v apt-get >/dev/null; then
  apt-get full-upgrade -y
else
  dnf upgrade -y
fi
if command -v dpkg-query >/dev/null; then
  new_version=$(dpkg-query -W -f='${Version}' gel-server-7)
else
  new_version=$(rpm -q --qf '%{EPOCHNUM}:%{VERSION}-%{RELEASE}' gel-server-7)
fi
echo "New server: $new_version"
test "$old_version" != "$new_version"
start_server
trap 'stop_server' EXIT
query 'select MigrationProbe.value;' | tee /tmp/migration.log
grep -q before-upgrade /tmp/migration.log
