#!/usr/bin/env bash
# Rotate the Vercel provisioning token held as a workspace variable in HCP
# Terraform.
#
# This script is the scripted middle of a three-step procedure. It cannot mint
# or revoke a Vercel token; Vercel exposes neither operation at the scope this
# credential needs. See docs/secrets.md for the full procedure.
#
#   Step 1 (human)   Create the replacement token in the Vercel dashboard and
#                    record the OLD token's identifier.
#   Step 2 (this)    Verify, install, and prove the new token.
#   Step 3 (human)   Revoke the old token by the recorded identifier.
#
# The old token stays valid throughout. Nothing here revokes anything.
#
# Usage:
#   scripts/rotate-vercel-token.sh --old-token-id <id> < new-token.txt
#   pbpaste | scripts/rotate-vercel-token.sh --old-token-id <id>
#
# The new token is read from stdin. Never pass a credential as an argument:
# argv is visible in `ps` and lands in shell history.

set -euo pipefail

ORG="${TF_CLOUD_ORGANIZATION:-gelstable}"
WORKSPACE="${TF_WORKSPACE:-gel-registry}"
VAR_KEY="vercel_api_token"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

old_token_id=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --old-token-id)
      old_token_id="${2:-}"
      shift 2
      ;;
    -h | --help)
      sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

die() {
  echo "error: $*" >&2
  exit 1
}

for cmd in curl jq terraform; do
  command -v "$cmd" >/dev/null || die "$cmd not found; run inside 'nix develop'"
done

[[ -n "$old_token_id" ]] ||
  die "--old-token-id is required. Record it from the Vercel dashboard before
       creating the replacement; two tokens with adjacent creation dates are
       hard to tell apart afterwards."

: "${TF_API_TOKEN:?TF_API_TOKEN is required to update the HCP workspace variable}"

if [[ -t 0 ]]; then
  die "the new Vercel token is read from stdin, not from a terminal"
fi
IFS= read -r new_token || true
[[ -n "$new_token" ]] || die "no token on stdin"

tfe_api() {
  local method="$1" path="$2"
  shift 2
  curl --fail --silent --show-error \
    --request "$method" \
    --header "Authorization: Bearer $TF_API_TOKEN" \
    --header "Content-Type: application/vnd.api+json" \
    "https://app.terraform.io/api/v2${path}" "$@"
}

# --- Verify the token before writing it anywhere -----------------------------

echo "Verifying the new token against the Vercel API."

user_json="$(curl --fail --silent --show-error \
  --header "Authorization: Bearer $new_token" \
  https://api.vercel.com/v2/user)" ||
  die "the new token failed authentication against Vercel"

echo "  authenticated as: $(jq -r '.user.username // .user.email // "unknown"' <<<"$user_json")"

# Scope, not the token value, is what usually goes wrong. Confirm the token can
# actually see the team the configuration targets.
team_id="$(terraform -chdir="$REPO_ROOT/infra" console <<<'var.vercel_team_id' 2>/dev/null |
  tr -d '"' || true)"

if [[ -n "$team_id" && "$team_id" != "null" ]]; then
  curl --fail --silent --show-error \
    --header "Authorization: Bearer $new_token" \
    "https://api.vercel.com/v2/teams/${team_id}" >/dev/null ||
    die "the new token authenticated but cannot see team ${team_id}; its scope is wrong"
  echo "  team scope confirmed: ${team_id}"
else
  echo "  no team configured; skipping the team scope check"
fi

# --- Install it as the workspace variable ------------------------------------

echo "Locating the HCP workspace ${ORG}/${WORKSPACE}."
workspace_id="$(tfe_api GET "/organizations/${ORG}/workspaces/${WORKSPACE}" |
  jq -r '.data.id')"
[[ -n "$workspace_id" && "$workspace_id" != "null" ]] ||
  die "could not resolve workspace ${ORG}/${WORKSPACE}"

var_id="$(tfe_api GET "/workspaces/${workspace_id}/vars" |
  jq -r --arg k "$VAR_KEY" '.data[] | select(.attributes.key == $k) | .id')"
[[ -n "$var_id" ]] ||
  die "workspace variable ${VAR_KEY} not found; create it in HCP first"

echo "Updating workspace variable ${VAR_KEY}."
jq -n --arg id "$var_id" --arg value "$new_token" \
  '{data: {id: $id, type: "vars", attributes: {value: $value, sensitive: true}}}' |
  tfe_api PATCH "/workspaces/${workspace_id}/vars/${var_id}" --data @- >/dev/null

# --- Prove it ----------------------------------------------------------------
# The plan, not the API call above, is what proves the token is sufficient.

echo "Running a plan with the new token."
terraform -chdir="$REPO_ROOT/infra" init -input=false >/dev/null
if ! terraform -chdir="$REPO_ROOT/infra" plan -input=false -no-color; then
  die "the plan failed with the new token. The old token is still valid and
       still installed nowhere; restore the previous value in HCP and
       investigate before revoking anything."
fi

# --- Hand back to a human ----------------------------------------------------
# This script must not exit quietly. Its last output is a revocation
# instruction, and a lost instruction turns a rotation into a second live
# credential.

cat <<EOF

================================================================================
ROTATION INCOMPLETE. One manual step remains.

The new token is installed and a plan has succeeded with it. The old token is
still valid and must be revoked by hand.

  Revoke token:  ${old_token_id}
  Dashboard:     https://vercel.com/account/tokens

Do this after the change has been merged and a CI apply has succeeded. Confirm
the token is gone rather than assuming, then resolve the revocation note on the
pull request.

An open revocation note is an incomplete rotation.
================================================================================
EOF
