# syntax=docker/dockerfile:1.7
#
# Dashboard container: FastAPI + Jinja2 templates + Postgres pool +
# matview refresher + SLO checker. Stateless beyond the in-memory
# stale-while-revalidate cache.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY services/dashboard/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY services/dashboard/app /app/app

# Static sidecar served by /api/export/camera_metadata.json. Ships with
# the image so the dashboard works without bind-mounts.
COPY data/camera_metadata.json /data/camera_metadata.json

ENV PYTHONPATH=/app \
    DASHBOARD_HOST=0.0.0.0 \
    DASHBOARD_PORT=18080
EXPOSE 18080

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:18080/healthz || exit 1

CMD ["python", "-m", "app.main"]
