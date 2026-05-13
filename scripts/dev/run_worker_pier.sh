#!/usr/bin/env bash
# Run a second worker dedicated to the City of Pompano Beach pier cam.
# Reads the same .env as the primary SEAHIVECAM worker, then overrides
# the camera-specific values:
#
#   VIDEO_SOURCE         → pier-cam RTSP
#   SOURCE_LABEL_PRIMARY → pier_cam   (every detection_event ends up
#                                      tagged source_name='pier_cam')
#   WORKER_HTTP_PORT     → 8083       (so it doesn't fight :8081)
#   CUDA_VISIBLE_DEVICES → a different GPU than the SEAHIVECAM worker
#
# The dashboard's /api/stream_pier.mjpeg, /api/snapshot_pier.jpg etc.
# proxy to this worker. Run both with:
#
#   ./scripts/dev/run_worker.sh      &  # SEAHIVECAM on :8081
#   ./scripts/dev/run_worker_pier.sh &  # pier cam   on :8083
#
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

# Overrides for the pier-cam process. Each *_PIER env var in .env
# becomes the runtime value of its non-_PIER counterpart for this
# process only.
export VIDEO_SOURCE="${VIDEO_SOURCE_PIER:?VIDEO_SOURCE_PIER not set in .env}"
export SOURCE_LABEL_PRIMARY="${SOURCE_LABEL_PRIMARY_PIER:-pier_cam}"
export WORKER_HTTP_PORT="${WORKER_HTTP_PORT_PIER:-8083}"
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES_PIER:-${CUDA_VISIBLE_DEVICES:-0}}"

# Defaults shared with the primary worker.
export DETECTOR_PATH="${DETECTOR_PATH:-$(pwd)/data/models/detector_v26/model.pt}"
export CLASSIFIER_PATH="${CLASSIFIER_PATH:-$(pwd)/data/models/classifier_v0_10_2/model.pt}"
export CLASSIFIER_INFERENCE_MODULE_DIR="${CLASSIFIER_INFERENCE_MODULE_DIR:-$(pwd)/data/models/classifier_v0_10_2}"
export EVENTS_LOG_DIR="${EVENTS_LOG_DIR:-$(pwd)/logs/events}"
export FRAMES_DIR="${FRAMES_DIR:-$(pwd)/frames}"
export DATABASE_URL="${DATABASE_URL:-postgresql://wahoobay:wahoobay@127.0.0.1:5432/wahoobay}"
export DEVICE="${DEVICE:-cuda:0}"

# The pier cam doesn't have a meaningful "fallback" — when it's dark,
# the dashboard will fall over to the SEAHIVECAM feed via the camera
# toggle. Suppress autoswitch entirely on this worker.
unset FALLBACK_VIDEO_SOURCE

export PYTHONPATH="$(pwd)/services/worker:${PYTHONPATH:-}"
exec python -m app.main
