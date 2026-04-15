FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.11 python3.11-venv python3-pip \
        libgl1 libglib2.0-0 \
        ffmpeg \
        ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.11 /usr/local/bin/python \
    && ln -sf /usr/bin/python3.11 /usr/local/bin/python3

WORKDIR /app

# PyTorch with CUDA 12.4
RUN pip install --index-url https://download.pytorch.org/whl/cu124 \
        "torch==2.4.1" "torchvision==0.19.1"

COPY services/worker/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# Bake Fishial model artifacts + inference module into the image.
# Expected layout under data/models (see scripts/fetch_models.sh):
#   detector_v26/{model.pt,inference.py,info.json}
#   classifier_v0_10_2/{model.pt,inference.py,info.json,requirements.txt}
COPY data/models/detector_v26       /data/models/detector_v26
COPY data/models/classifier_v0_10_2 /data/models/classifier_v0_10_2

COPY services/worker/app /app/app

ENV WORKER_HTTP_PORT=8081
EXPOSE 8081

HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8081/healthz || exit 1

CMD ["python", "-m", "app.main"]
