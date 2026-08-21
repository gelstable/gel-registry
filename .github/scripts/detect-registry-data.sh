#!/usr/bin/env bash
set -euo pipefail

present=false
for root in upstream bootstrap releases promotion pointers public sources; do
  if [[ -e "$root" || -L "$root" ]]; then
    present=true
    break
  fi
done

printf 'present=%s\n' "$present" >>"$GITHUB_OUTPUT"
