# syntax=docker/dockerfile:1.7
#
# Worker container: detector + classifier + persistence + RTSP ingest.
# Runs on the host's GPU (`docker run --gpus`); the FrameTap thread reads
# the source while a separate inference subprocess does inference.
#
# Models are baked in (`COPY data/models/...`) so the image is
# self-contained for production deployment. For dev, override by
# bind-mounting host `data/models` over `/data/models` — the contents
# of the baked layer are masked.
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# python3.11 + ffmpeg (RTSP/H.264 decode) + libGL/glib (opencv-headless
# still pulls a few system libs even though it's GUI-less).
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.11 python3.11-venv python3-pip \
        libgl1 libglib2.0-0 \
        ffmpeg \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.11 /usr/local/bin/python \
    && ln -sf /usr/bin/python3.11 /usr/local/bin/python3

WORKDIR /app

# Single pip install: requirements.txt declares its own --extra-index-url
# for the CUDA 12.4 torch wheels.
COPY services/worker/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# Fishial model artifacts (detector + classifier bundle + the vendored
# inference.py the worker loads into sys.path at runtime).
COPY data/models/detector_v26       /data/models/detector_v26
COPY data/models/classifier_v0_10_2 /data/models/classifier_v0_10_2

# Application code last so dev rebuilds skip the slow layers above.
COPY services/worker/app /app/app

# Sensible runtime defaults; everything is overridable via env or .env.
ENV PYTHONPATH=/app \
    WORKER_HTTP_PORT=8081 \
    DETECTOR_PATH=/data/models/detector_v26/model.pt \
    CLASSIFIER_PATH=/data/models/classifier_v0_10_2/model.pt \
    CLASSIFIER_INFERENCE_MODULE_DIR=/data/models/classifier_v0_10_2 \
    EVENTS_LOG_DIR=/data/logs/events \
    FRAMES_DIR=/data/frames

EXPOSE 8081

# Long start-period: cold start downloads + JIT-compiles ~10 s of CUDA
# kernels before /healthz responds.
HEALTHCHECK --interval=15s --timeout=5s --start-period=120s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8081/healthz || exit 1

CMD ["python", "-m", "app.main"]
