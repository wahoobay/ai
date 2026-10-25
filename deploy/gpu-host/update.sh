#!/usr/bin/env bash
# Move the GPU host to a commit and boot it: what scripts/deploy_env.sh runs on the host through
# SSM Run Command after promoting the images (`update.sh <sha12>`). Idempotent; the checkout is
# /opt/wahoobay unless WB_CHECKOUT says otherwise (tests).
set -euo pipefail
ref="${1:?usage: update.sh <git ref>}"
checkout="${WB_CHECKOUT:-/opt/wahoobay}"
cd "$checkout"
if [ "$(git rev-parse --is-shallow-repository 2>/dev/null)" = "true" ]; then
  git fetch --unshallow -q origin   # older commits (a rollback) must be reachable
else
  git fetch -q origin
fi
git checkout -q "$ref"
exec "$checkout/deploy/gpu-host/boot.sh"
