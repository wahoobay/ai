# syntax=docker/dockerfile:1.7
#
# Worker container: detector + classifier + persistence + RTSP ingest.
# Runs on the host's GPU (`docker run --gpus`); the FrameTap thread reads
# the source while a separate inference subprocess does inference.
#
# No weights in the image: the entrypoint fetches them from MODELS_URL
# (file:///data/models, s3://... on AWS) into /data/models at start, unless
# they are already there -- a bind-mounted host data/models wins.
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# python3.11 + ffmpeg (RTSP/H.264 decode) + libGL/glib (opencv-headless
# still pulls a few system libs even though it's GUI-less). We don't
# install `python3-pip` from apt — that binds pip to the distro's
# default Python (3.10 on Ubuntu 22.04), which would silently install
# cp310 wheels and miss recent releases that only ship cp311+ wheels
# (e.g., scipy 1.17.x). Ubuntu also disables ensurepip on the system
# Python, so we bootstrap pip via the official get-pip.py installer
# bound explicitly to the python3.11 interpreter.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.11 python3.11-venv \
        libgl1 libglib2.0-0 \
        ffmpeg \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.11 /usr/local/bin/python \
    && ln -sf /usr/bin/python3.11 /usr/local/bin/python3 \
    && curl -sSL https://bootstrap.pypa.io/get-pip.py | python3.11 \
    && python3.11 -m pip install --no-cache-dir --upgrade pip

WORKDIR /app

# Always invoke pip via `python -m pip` so it uses our 3.11 interpreter
# (and finds cp311 wheels). requirements.txt declares its own
# --extra-index-url for the CUDA 12.4 torch wheels.
COPY services/worker/requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt

# Application code last so dev rebuilds skip the slow layers above. Both
# classifier backends (v2, the DINOv2 default; fishial, the legacy bundle) are
# fetched at start, so reverting stays a single env-var flip.
COPY services/common /app/common
COPY scripts/fetch_models.py /app/fetch_models.py
COPY services/worker/entrypoint.sh /app/entrypoint.sh
COPY services/worker/app /app/app

# Sensible runtime defaults; everything is overridable via env or .env.
# CLASSIFIER_BACKEND=v2 is the new default; set to "fishial" to roll back.
ENV PYTHONPATH=/app \
    WORKER_HTTP_PORT=8081 \
    CLASSIFIER_BACKEND=v2 \
    DETECTOR_PATH=/data/models/detector_v26/model.pt \
    CLASSIFIER_PATH=/data/models/classifier_v0_10_2/model.pt \
    CLASSIFIER_INFERENCE_MODULE_DIR=/data/models/classifier_v0_10_2 \
    CLASSIFIER_V2_CHECKPOINT=/data/models/classifier_v2/checkpoint_best.pt \
    CLASSIFIER_V2_CLASSES_JSON=/data/models/classifier_v2/classes.json \
    EVENTS_LOG_DIR=/data/logs/events \
    FRAMES_URL=file:///data/frames \
    MODELS_URL=file:///data/models \
    MODELS_DIR=/data/models

EXPOSE 8081

# Long start-period: cold start downloads + JIT-compiles ~10 s of CUDA
# kernels before /healthz responds.
HEALTHCHECK --interval=15s --timeout=5s --start-period=120s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8081/healthz || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "-m", "app.main"]
