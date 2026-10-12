#!/usr/bin/env bash
# Production acceptance, root in a disposable supported container.
set -euo pipefail
channel=${1:-stable}
case "$channel" in stable|testing) ;; *) exit 2 ;; esac
base=https://registry.gelstable.com
verify_key() {
  local key=$1 expected=${GELSTABLE_EXPECTED_FPR:?committed production fingerprint is required} fingerprints
  [[ "$expected" =~ ^[A-Fa-f0-9]{40}$|^[A-Fa-f0-9]{64}$ ]] || {
    echo 'Invalid expected production fingerprint' >&2
    return 1
  }
  fingerprints=$(gpg --batch --with-colons --show-keys "$key" | awk -F: '
    $1 == "pub" { primary = 1; next }
    primary && $1 == "fpr" { print $10; primary = 0 }
  ')
  # Reject extra primary keys as well as a missing or unexpected certificate.
  if test "$fingerprints" != "${expected^^}"; then
    echo 'Downloaded certificate does not match the committed production fingerprint' >&2
    return 1
  fi
}
configure() {
  if command -v apt-get >/dev/null; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y curl ca-certificates gnupg util-linux diffutils
    install -d /etc/apt/keyrings
    curl -fsSLo /etc/apt/keyrings/gelstable.asc "$base/keys/gelstable.asc"
    verify_key /etc/apt/keyrings/gelstable.asc
    config=gelstable.sources
    test "$channel" = stable || config=gelstable-testing.sources
    curl -fsSLo /etc/apt/sources.list.d/gelstable.sources "$base/$config"
    cmp /etc/apt/sources.list.d/gelstable.sources "/expected/$config"
    apt-get update
    apt-get install -y gel-7 gel-server-7-ext-postgis
    dpkg-query -W gel-7 gel-server-7 gel-server-7-ext-postgis gel-cli
  else
    dnf install -y --allowerasing curl gnupg2 util-linux diffutils
    install -d /etc/pki/rpm-gpg
    curl -fsSLo /etc/pki/rpm-gpg/gelstable.asc "$base/keys/gelstable.asc"
    verify_key /etc/pki/rpm-gpg/gelstable.asc
    rpmkeys --import /etc/pki/rpm-gpg/gelstable.asc
    config=gelstable.repo
    test "$channel" = stable || config=gelstable-testing.repo
    curl -fsSLo /etc/yum.repos.d/gelstable.repo "$base/$config"
    cmp /etc/yum.repos.d/gelstable.repo "/expected/$config"
    sed -i 's|^gpgkey=.*|gpgkey=file:///etc/pki/rpm-gpg/gelstable.asc|' /etc/yum.repos.d/gelstable.repo
    if test "$channel" = testing; then
      dnf -y --enablerepo=gelstable-testing install gel-7 gel-server-7-ext-postgis
    else
      dnf -y install gel-7 gel-server-7-ext-postgis
    fi
    rpm -q gel-7 gel-server-7 gel-server-7-ext-postgis gel-cli
  fi
}
start_server() {
  runuser -u gel -- gel-server-7 --data-dir "$data" --runstate-dir "$runstate" \
    --security insecure_dev_mode --port 5656 > /tmp/gel-server.log 2>&1 &
  server_pid=$!
  for attempt in $(seq 1 120); do
    if gel --host localhost --port 5656 --tls-security insecure --user admin --database main query --output-format=tab-separated 'select 1 + 1' > /tmp/query.log 2>&1; then
      grep -qx '2' /tmp/query.log
      return
    fi
    kill -0 "$server_pid" || { cat /tmp/gel-server.log; return 1; }
    sleep 1
  done
  cat /tmp/gel-server.log /tmp/query.log
  return 1
}
query() {
  gel --host localhost --port 5656 --tls-security insecure --user admin --database main query --output-format=tab-separated "$1"
}
stop_server() {
  kill "$server_pid"
  wait "$server_pid" || true
}
# migrate.sh sources these functions and retains the same data directory.
if [[ "${BASH_SOURCE[0]}" = "$0" ]]; then
  configure
  data=/var/lib/gel/native-smoke
  runstate=/run/gel-native-smoke
  install -d -o gel -g gel "$data" "$runstate"
  runuser -u gel -- gel-server-7 --data-dir "$data" --runstate-dir "$runstate" --bootstrap-only --security insecure_dev_mode
  start_server
  trap 'stop_server' EXIT
  query 'create extension postgis;'
  query "select ext::postgis::astext(ext::postgis::geomfromtext('POINT(1 2)'));" | tee /tmp/postgis.log
  grep -q 'POINT(1 2)' /tmp/postgis.log
fi
