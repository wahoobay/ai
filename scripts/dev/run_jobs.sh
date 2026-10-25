#!/usr/bin/env bash
# The periodic work the dashboard used to do in-process -- the materialized-view
# refresh and the alert rules -- as a loop of `python -m app.jobs all`, every
# JOBS_INTERVAL_S (default 60). On AWS the jobs Lambda does this on a schedule.
set -euo pipefail
cd "$(dirname "$0")/../.."

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

export DATABASE_URL="${DATABASE_URL:-postgresql://wahoobay:wahoobay@127.0.0.1:5432/wahoobay}"
export SENSESTREAM_DEPLOYMENT_URI="${SENSESTREAM_DEPLOYMENT_URI:-wahoo_2}"
export WORKER_URL="${WORKER_URL:-http://127.0.0.1:8081}"
export PYTHONPATH="$(pwd)/services/dashboard:${PYTHONPATH:-}"
INTERVAL="${JOBS_INTERVAL_S:-60}"

while true; do
  python -m app.jobs all || echo "jobs: a job failed (see above); retrying in ${INTERVAL}s" >&2
  sleep "$INTERVAL"
done
