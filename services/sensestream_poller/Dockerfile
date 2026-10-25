# syntax=docker/dockerfile:1.7
#
# Poller image, two targets from one base:
#   service (default)  the local loop: fetch then write in one process, plus a
#                      small FastAPI /status for the DGX and compose.
#   lambda             the same code behind the Lambda runtime interface client:
#                      app.lambda_entry.fetch_handler (sources -> SQS) and
#                      write_handler (SQS -> database). Built with
#                      `--target lambda`, tagged <env>-lambda.
FROM python:3.11-slim AS base

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

# Synthetic generator the poller imports for SENSESTREAM_SYNTHETIC=true. Lives
# at /scripts/ so the relative-path discovery in main.py
# (Path(__file__).resolve().parents[3] / "scripts") finds it.
COPY scripts/gen_synthetic_sensor_data.py /scripts/gen_synthetic_sensor_data.py

ENV PYTHONPATH=/app

# ---- lambda: fetch + write ---------------------------------------------------
FROM base AS lambda
RUN pip install awslambdaric==4.2.2 boto3==1.43.118
ENTRYPOINT ["python", "-m", "awslambdaric"]
CMD ["app.lambda_entry.fetch_handler"]

# ---- service (default target) ----------------------------------------------
FROM base AS service
ENV POLLER_HTTP_HOST=0.0.0.0 \
    POLLER_HTTP_PORT=8082
EXPOSE 8082

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8082/healthz || exit 1

CMD ["python", "-m", "app.main"]
