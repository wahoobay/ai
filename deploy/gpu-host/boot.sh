#!/usr/bin/env bash
# Boot, or re-boot, the GPU host's stack. Run by the systemd unit at every boot and by update.sh
# after a deploy; safe to run again at any time.
#
#   1. read /etc/wahoobay/host.env, written by the infra stack's user data (WB_* facts: environment,
#      location, where the database secret, the cameras secret, the buckets and the registry are);
#   2. read the three secrets with the instance role and write the worker's .env next to this file
#      (mode 600, rewritten every time, never printed);
#   3. log in to ECR, pull the environment's worker image, `docker compose up -d`;
#   4. wait for the worker's /healthz (the first start fetches weights and compiles the classifier).
set -euo pipefail

HOST_ENV="${WB_HOST_ENV:-/etc/wahoobay/host.env}"
# shellcheck disable=SC1090
. "$HOST_ENV"
: "${WB_ENV:?}" "${WB_LOCATION_ID:?}" "${WB_REGION:?}" "${WB_DB_SECRET_ARN:?}" "${WB_CAMERAS_SECRET:?}"
: "${WB_ORIGIN_SECRET_ARN:?}" "${WB_FRAMES_BUCKET:?}" "${WB_MODELS_BUCKET:?}" "${WB_ECR_REGISTRY:?}"
WB_TZ="${WB_TZ:-UTC}"
WB_DEPLOYMENT_URI="${WB_DEPLOYMENT_URI:-}"
WB_LOG_GROUP="${WB_LOG_GROUP:-/ai/${WB_LOCATION_ID}/${WB_ENV}/gpu-host}"

cd "$(dirname "$0")"

secret() {
  aws secretsmanager get-secret-value --region "$WB_REGION" --secret-id "$1" --query SecretString --output text
}

db_json="$(secret "$WB_DB_SECRET_ARN")"
cameras_json="$(secret "$WB_CAMERAS_SECRET")"
# The stream origin's own header secret, by name (an existing host keeps its first host.env, so the name is
# derived here); before it exists, the ARN host.env names.
stream_secret_name="${WB_STREAM_SECRET_NAME:-/ai/${WB_LOCATION_ID}/${WB_ENV}/stream-origin-verify}"
if ! origin_secret="$(secret "$stream_secret_name" 2>/dev/null)"; then
  origin_secret="$(secret "$WB_ORIGIN_SECRET_ARN")"
fi

database_url="$(DB_JSON="$db_json" python3 -c '
import json, os, urllib.parse
d = json.loads(os.environ["DB_JSON"])
print("postgresql://%s:%s@%s:%s/%s" % (d["username"], urllib.parse.quote(d["password"], safe=""), d["host"], d.get("port", 5432), d.get("dbname", "ai")))
')"
video_source="$(CAM_JSON="$cameras_json" python3 -c 'import json, os; print(json.loads(os.environ["CAM_JSON"]).get("seahivecam", ""))')"
if [ -z "$video_source" ]; then
  echo "the cameras secret ($WB_CAMERAS_SECRET) has no seahivecam URL; fill it (README) and run this again" >&2
  exit 1
fi

# Single-quoted values: compose interpolates $ in unquoted .env values, and a camera password may contain one.
# (A single quote inside a value must already be percent-encoded, as RTSP URLs require.)
q() { printf "'%s'" "$1"; }
umask 077
tmp="$(mktemp .env.XXXXXX)"
cat > "$tmp" <<ENVEOF
# Written by boot.sh from /etc/wahoobay/host.env and the environment's secrets. Do not edit: rewritten at every boot.
LOCATION_ID=$(q "$WB_LOCATION_ID")
TZ=$(q "$WB_TZ")
SENSESTREAM_DEPLOYMENT_URI=$(q "$WB_DEPLOYMENT_URI")
DATABASE_URL=$(q "$database_url")
VIDEO_SOURCE=$(q "$video_source")
SOURCE_LABEL_PRIMARY='seahivecam'
FALLBACK_VIDEO_SOURCE=''
FRAMES_URL=$(q "s3://${WB_FRAMES_BUCKET}/")
MODELS_URL=$(q "s3://${WB_MODELS_BUCKET}/")
# boto3 inside the container: the region, and credentials from the instance role (IMDSv2, hop limit 2).
AWS_REGION=$(q "$WB_REGION")
AWS_DEFAULT_REGION=$(q "$WB_REGION")
DEVICE='cuda:0'
# The cuda image has no C compiler, which triton needs to compile the classifier; Phase 0 measures without it.
CLASSIFIER_V2_COMPILE='false'
WORKER_HTTP_HOST='0.0.0.0'
WORKER_HTTP_PORT='8081'
ORIGIN_VERIFY_SECRET=$(q "$origin_secret")
WB_IMAGE_TAG=$(q "$WB_ENV")
WB_ECR_REGISTRY=$(q "$WB_ECR_REGISTRY")
WB_LOG_GROUP=$(q "$WB_LOG_GROUP")
WB_REGION=$(q "$WB_REGION")
ENVEOF
mv -f "$tmp" .env
chmod 600 .env
echo "wrote .env for ${WB_LOCATION_ID}/${WB_ENV}"

aws ecr get-login-password --region "$WB_REGION" | docker login --username AWS --password-stdin "$WB_ECR_REGISTRY" >/dev/null
# Each deploy pulls a ~14 GB worker image and left the previous one dangling; four deploys filled the root disk
# and the fifth pull failed (2026-10-27). Prune before the pull (room for the new image next to the running one)
# and after the start (the image just replaced). Dangling only: tagged images and the running containers stay.
docker image prune -f >/dev/null
docker compose pull -q
docker compose up -d --remove-orphans
docker image prune -f >/dev/null

timeout_s="${BOOT_HEALTH_TIMEOUT_S:-600}"
interval_s="${BOOT_HEALTH_INTERVAL_S:-10}"
step=$(( interval_s > 0 ? interval_s : 1 ))
attempts=$(( timeout_s / step ))
[ "$attempts" -ge 1 ] || attempts=1
n=0
until curl -sf -m 5 http://127.0.0.1:8081/healthz >/dev/null 2>&1; do
  n=$((n + 1))
  if [ "$n" -ge "$attempts" ]; then
    echo "worker did not answer /healthz within ${timeout_s}s; see: docker compose logs worker" >&2
    exit 1
  fi
  sleep "$interval_s"
done
echo "worker healthy"

# /healthz says the process is up; /readyz says a frame has arrived. A camera that is unreachable or refuses
# its credentials is not a boot failure (the worker keeps retrying), but it must be visible here.
ready_timeout_s="${BOOT_READY_TIMEOUT_S:-60}"
ready_attempts=$(( ready_timeout_s / step ))
[ "$ready_attempts" -ge 1 ] || ready_attempts=1
n=0
while :; do
  if body="$(curl -sf -m 5 http://127.0.0.1:8081/readyz 2>/dev/null)"; then
    echo "camera: streaming (${body})"
    break
  fi
  n=$((n + 1))
  if [ "$n" -ge "$ready_attempts" ]; then
    echo "camera: no frame yet after ${ready_timeout_s}s; the worker keeps retrying the source. See: docker compose logs worker"
    break
  fi
  sleep "$interval_s"
done
