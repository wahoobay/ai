#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."

# Ensure the wahoobay conda env is active even when exec'd from a disowned shell.
if ! command -v python >/dev/null 2>&1 || ! python -c "import app" 2>/dev/null; then
  CONDA_SH="${CONDA_SH:-/raid/scratch/dzimmerman2021/miniconda3/etc/profile.d/conda.sh}"
  if [[ -f "$CONDA_SH" ]]; then
    # shellcheck disable=SC1090
    source "$CONDA_SH"
    conda activate wahoobay
  fi
fi

export DATABASE_URL="${DATABASE_URL:-postgresql://wahoobay:wahoobay@127.0.0.1:5432/wahoobay}"
export SENSESTREAM_BASE_URL="${SENSESTREAM_BASE_URL:-https://api.sensestream.org}"
export SENSESTREAM_DEPLOYMENT_URI="${SENSESTREAM_DEPLOYMENT_URI:-wahoo_2}"
export SENSESTREAM_POLL_INTERVAL_S="${SENSESTREAM_POLL_INTERVAL_S:-300}"
export POLLER_HTTP_PORT="${POLLER_HTTP_PORT:-8082}"

# SENSESTREAM_AUTH_TOKEN left unset on purpose: service runs in "stub" mode
# (deployment probe only) until we have real credentials.

export PYTHONPATH="$(pwd)/services/sensestream_poller:${PYTHONPATH:-}"
exec python -m app.main
