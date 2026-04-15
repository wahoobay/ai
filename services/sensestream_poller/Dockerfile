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

ENV POLLER_HTTP_PORT=8082
EXPOSE 8082

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8082/healthz || exit 1

CMD ["python", "-m", "app.main"]
