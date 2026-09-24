#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."

# Activate the project env unless it already is. Testing only that *a*
# python exists is not enough: a login shell with conda's base env on
# PATH has one, without uvicorn or any other project dependency.
if [[ "${CONDA_DEFAULT_ENV:-}" != wahoobay ]]; then
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

export WORKER_URL="${WORKER_URL:-http://127.0.0.1:8081}"
export DATABASE_URL="${DATABASE_URL:-postgresql://wahoobay:wahoobay@127.0.0.1:5432/wahoobay}"
export DASHBOARD_PORT="${DASHBOARD_PORT:-18080}"

export PYTHONPATH="$(pwd)/services/dashboard:${PYTHONPATH:-}"
exec python -m app.main
