#!/usr/bin/env bash
# Move the GPU host to a commit and boot it: what scripts/deploy_env.sh runs on the host through
# SSM Run Command after promoting the images (`update.sh <sha12>`). Idempotent; the checkout is
# /opt/wahoobay unless WB_CHECKOUT says otherwise (tests).
set -euo pipefail
ref="${1:?usage: update.sh <git ref>}"
checkout="${WB_CHECKOUT:-/opt/wahoobay}"
cd "$checkout"
git fetch -q origin
git checkout -q "$ref"
exec "$checkout/deploy/gpu-host/boot.sh"
