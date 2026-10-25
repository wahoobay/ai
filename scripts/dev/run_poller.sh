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

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

export DATABASE_URL="${DATABASE_URL:-postgresql://wahoobay:wahoobay@127.0.0.1:5432/wahoobay}"
export WAHOOBAY_API_URL="${WAHOOBAY_API_URL:-https://api.wahoobay.net}"
export WAHOOBAY_API_ENABLED="${WAHOOBAY_API_ENABLED:-1}"
export SENSESTREAM_BASE_URL="${SENSESTREAM_BASE_URL:-https://api.sensestream.org}"
export SENSESTREAM_DEPLOYMENT_URI="${SENSESTREAM_DEPLOYMENT_URI:-wahoo_2}"
# The DGX demo keeps its synthetic last-resort fallback; the code's own default is off.
export SENSESTREAM_SYNTHETIC="${SENSESTREAM_SYNTHETIC:-true}"
export SENSESTREAM_POLL_INTERVAL_S="${SENSESTREAM_POLL_INTERVAL_S:-300}"
export POLLER_HTTP_PORT="${POLLER_HTTP_PORT:-8082}"

# Data-source order (first available wins):
#   1. Wahoo Bay public API (api.wahoobay.net) — no auth needed; default ON.
#   2. Direct SenseStream observations (needs SENSESTREAM_AUTH_TOKEN).
#   3. Synthetic stub generator (last-resort fallback).
# SENSESTREAM_AUTH_TOKEN is intentionally left unset — the public Wahoo Bay API
# already gives us live sonde + weather data without a credential.

export PYTHONPATH="$(pwd)/services/sensestream_poller:${PYTHONPATH:-}"
exec python -m app.main
