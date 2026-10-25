#!/usr/bin/env bash
# Refuse to deploy a commit that never reached main: production ships only what staging already ran.
# Usage: scripts/assert-on-main.sh <sha>
set -euo pipefail
sha="${1:?usage: assert-on-main.sh <sha>}"
# Explicit destination: a shallow Actions checkout has no origin/main until we create it.
git fetch -q origin '+refs/heads/main:refs/remotes/origin/main' 2>/dev/null || true
if git merge-base --is-ancestor "$sha" origin/main 2>/dev/null; then
  echo "$sha is on origin/main"
else
  echo "::error::$sha is not on origin/main; production deploys only commits that reached main" >&2
  exit 1
fi
