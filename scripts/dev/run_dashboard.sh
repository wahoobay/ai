#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."

if ! command -v python >/dev/null 2>&1; then
  CONDA_SH="${CONDA_SH:-/raid/scratch/dzimmerman2021/miniconda3/etc/profile.d/conda.sh}"
  if [[ -f "$CONDA_SH" ]]; then
    # shellcheck disable=SC1090
    source "$CONDA_SH"
    conda activate wahoobay
  fi
fi

export WORKER_URL="${WORKER_URL:-http://127.0.0.1:8081}"
export DATABASE_URL="${DATABASE_URL:-postgresql://wahoobay:wahoobay@127.0.0.1:5432/wahoobay}"
export DASHBOARD_PORT="${DASHBOARD_PORT:-18080}"

export PYTHONPATH="$(pwd)/services/dashboard:${PYTHONPATH:-}"
exec python -m app.main
