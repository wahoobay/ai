# syntax=docker/dockerfile:1.7
#
# Dashboard image, two targets from one base:
#   service (default)  FastAPI + Jinja2 templates + Postgres pool; stateless per
#                      request. App Runner runs it on port 8080.
#   lambda             the same code behind the Lambda runtime interface client:
#                      app.lambda_entry.handler runs the jobs (matview refresh,
#                      alert rules) and the migrations. Built with
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

COPY services/dashboard/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY services/common /app/common
COPY services/dashboard/app /app/app

# Schema migrations: `python -m app.migrate` (and the migrate Lambda) run them.
COPY db /app/db

# Catalogues the page and the review tool read: species sheet, celebrities, taxonomy.
COPY assets /app/assets

# Static sidecar served by /api/export/camera_metadata.json. Ships with
# the image so the dashboard works without bind-mounts.
COPY data/camera_metadata.json /data/camera_metadata.json

ENV PYTHONPATH=/app

# ---- lambda: jobs + migrate -------------------------------------------------
FROM base AS lambda
RUN pip install awslambdaric==4.2.2
ENTRYPOINT ["python", "-m", "awslambdaric"]
CMD ["app.lambda_entry.handler"]

# ---- service (default target) ----------------------------------------------
FROM base AS service
ENV DASHBOARD_HOST=0.0.0.0 \
    DASHBOARD_PORT=8080
EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

CMD ["python", "-m", "app.main"]
