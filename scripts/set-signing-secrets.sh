#!/usr/bin/env bash
# Set Environment secrets from a local signing custody bundle; never print keys.
set -euo pipefail
bundle=${1:?usage: set-signing-secrets.sh BUNDLE_DIR}
key="$bundle/automation-subkey.asc"
test -f "$key"
fingerprint=$(python3 - "$bundle/fingerprints.json" <<'PY'
import json, re, sys
fingerprint = json.load(open(sys.argv[1]))['signing']
if not isinstance(fingerprint, str) or not re.fullmatch(r'(?:[0-9A-Fa-f]{40}|[0-9A-Fa-f]{64})', fingerprint):
    raise SystemExit('invalid signing fingerprint')
print(fingerprint.upper())
PY
)
printf 'Signing fingerprint: %s\n' "$fingerprint"
printf 'Set KEY and FPR in gelstable/gel-registry (registry-signing), and gelstable/{gel,gel-cli,gel-postgis} (package-signing).\n'
printf 'Type yes to set these eight secrets: '
read -r confirmation
[[ "$confirmation" == yes ]] || exit 1
for repo in gel-registry gel gel-cli gel-postgis; do
  environment=package-signing
  prefix=PACKAGE
  if [[ "$repo" == gel-registry ]]; then environment=registry-signing; prefix=REGISTRY; fi
  gh secret set "${prefix}_SIGNING_KEY" --repo "gelstable/$repo" --env "$environment" < "$key"
  printf '%s' "$fingerprint" | gh secret set "${prefix}_SIGNING_FPR" --repo "gelstable/$repo" --env "$environment"
done
