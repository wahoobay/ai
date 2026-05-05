#!/usr/bin/env bash
# Run the worker directly (no docker) using local paths.
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

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

export VIDEO_SOURCE="${VIDEO_SOURCE:-file://$(pwd)/data/test_videos}"
export DETECTOR_PATH="${DETECTOR_PATH:-$(pwd)/data/models/detector_v26/model.pt}"
export CLASSIFIER_PATH="${CLASSIFIER_PATH:-$(pwd)/data/models/classifier_v0_10_2/model.pt}"
export CLASSIFIER_INFERENCE_MODULE_DIR="${CLASSIFIER_INFERENCE_MODULE_DIR:-$(pwd)/data/models/classifier_v0_10_2}"
export EVENTS_LOG_DIR="${EVENTS_LOG_DIR:-$(pwd)/logs/events}"
export FRAMES_DIR="${FRAMES_DIR:-$(pwd)/frames}"
export DATABASE_URL="${DATABASE_URL:-postgresql://wahoobay:wahoobay@127.0.0.1:5432/wahoobay}"
export DEVICE="${DEVICE:-cuda:0}"
export WORKER_HTTP_PORT="${WORKER_HTTP_PORT:-8081}"

export PYTHONPATH="$(pwd)/services/worker:${PYTHONPATH:-}"
exec python -m app.main
