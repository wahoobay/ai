#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
PGDATA="$(pwd)/pgdata"
if pg_ctl -D "$PGDATA" status >/dev/null 2>&1; then
  pg_ctl -D "$PGDATA" stop -m fast
else
  echo "postgres not running"
fi
