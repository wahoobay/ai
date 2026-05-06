# syntax=docker/dockerfile:1.7
#
# SenseStream poller container: small FastAPI service that ingests
# water-quality readings into Postgres. In stub mode (no auth token)
# it imports scripts/gen_synthetic_sensor_data.py to emit deterministic
# synthetic readings so downstream charts stay populated.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY services/sensestream_poller/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY services/sensestream_poller/app /app/app

# Synthetic generator the poller imports in stub mode. Lives at
# /scripts/ so the relative-path discovery in main.py
# (Path(__file__).resolve().parents[3] / "scripts") finds it.
COPY scripts/gen_synthetic_sensor_data.py /scripts/gen_synthetic_sensor_data.py

ENV PYTHONPATH=/app \
    POLLER_HTTP_HOST=0.0.0.0 \
    POLLER_HTTP_PORT=8082
EXPOSE 8082

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8082/healthz || exit 1

CMD ["python", "-m", "app.main"]
