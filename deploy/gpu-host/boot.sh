#!/usr/bin/env bash
# Boot, or re-boot, the GPU host's stack. Run by the systemd unit at every boot and by update.sh
# after a deploy; safe to run again at any time.
#
#   1. read /etc/wahoobay/host.env, written by the infra stack's user data (WB_* facts: environment,
#      location, where the database secret, the cameras secret, the buckets and the registry are);
#   2. read the three secrets with the instance role and write the worker's .env next to this file
#      (mode 600, rewritten every time, never printed);
#   3. log in to ECR, pull the environment's worker image, `docker compose up -d`;
#   4. wait for the worker's /healthz (the first start fetches weights and compiles the classifier);
#   5. report whether the camera streams and whether MediaMTX serves its HLS playlists.
# The camera is pulled once, by MediaMTX; the worker reads rtsp://mediamtx:8554/seahive and nothing else.
# When replay.conf names a recording for this environment, the recording plays there instead (README "Replay").
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
# MediaMTX's HLS CDN bearer (hlsCDNSecret): Caddy adds it on /hls/*, so MediaMTX serves one shared, cacheable
# session per path instead of per-viewer cookie sessions CloudFront cannot carry. Derived here from the origin
# secret (nothing new in Secrets Manager; the origin secret itself never reaches MediaMTX), stable across boots.
# printf is a builtin: the secret is never a command argument.
hls_cdn_secret="$(printf '%s' "$origin_secret" | sha256sum | cut -c1-64)"

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

# Replay (README "Replay"): replay.conf may name, for this environment, a recording in the models bucket. MediaMTX's
# seahive path then takes a publisher instead of pulling the camera, the replay service plays the recording there,
# looped, and the worker draws its detections without recording them. A recording is never replaced under its
# name (a new one gets a new key), so a copy already on the host is the one named. Fetched before anything else
# changes: a missing recording fails the boot with the running stack as it was.
replay_key="$(sed -n "s/^${WB_ENV}=//p" replay.conf 2>/dev/null | tail -n 1 | tr -d '[:space:]')"
replay_dir="${WB_DATA_DIR:-/opt/wahoobay-data}/replay"
if [ -n "$replay_key" ]; then
  replay_file="$(basename "$replay_key")"
  if [ ! -s "$replay_dir/$replay_file" ]; then
    mkdir -p "$replay_dir"
    part="$replay_dir/.$replay_file.part"
    if ! aws s3 cp --only-show-errors "s3://${WB_MODELS_BUCKET}/${replay_key}" "$part"; then
      rm -f "$part"
      echo "replay.conf names s3://${WB_MODELS_BUCKET}/${replay_key} for ${WB_ENV}, which could not be fetched; upload it (README \"Replay\") or empty the line" >&2
      exit 1
    fi
    mv -f "$part" "$replay_dir/$replay_file"
  fi
  seahive_source=publisher compose_profiles=replay persist_enabled=false source_word=recording
else
  replay_file="" seahive_source="$video_source" compose_profiles="" persist_enabled=true source_word=camera
fi

# Single-quoted values: compose interpolates $ in unquoted .env values, and a camera password may contain one.
# (A single quote inside a value must already be percent-encoded, as RTSP URLs require.)
q() { printf "'%s'" "$1"; }
# One hash per bind-mounted file, each a label on its service (compose recreates a container when its label
# changes). Without them a changed file never reaches the running container: git replaces the file, the mount
# keeps the old inode, and a running loop has parsed its script anyway (the bring-up's two-second segments did not
# apply until MediaMTX was recreated by hand). Computed here, not inside the heredoc, so a missing file is fatal.
encoder_script_sha="$(sha256sum encoder.sh | cut -c1-12)"
mediamtx_config_sha="$(sha256sum mediamtx.yml | cut -c1-12)"
caddyfile_sha="$(sha256sum Caddyfile | cut -c1-12)"
umask 077
tmp="$(mktemp .env.XXXXXX)"
cat > "$tmp" <<ENVEOF
# Written by boot.sh from /etc/wahoobay/host.env and the environment's secrets. Do not edit: rewritten at every boot.
LOCATION_ID=$(q "$WB_LOCATION_ID")
TZ=$(q "$WB_TZ")
SENSESTREAM_DEPLOYMENT_URI=$(q "$WB_DEPLOYMENT_URI")
DATABASE_URL=$(q "$database_url")
# What MediaMTX's seahive path plays: the camera, or `publisher` while the replay service plays REPLAY_FILE.
SEAHIVE_SOURCE=$(q "$seahive_source")
REPLAY_FILE=$(q "$replay_file")
COMPOSE_PROFILES=$(q "$compose_profiles")
PERSIST_ENABLED=$(q "$persist_enabled")
VIDEO_SOURCE='rtsp://mediamtx:8554/seahive'
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
HLS_CDN_SECRET=$(q "$hls_cdn_secret")
WB_IMAGE_TAG=$(q "$WB_ENV")
WB_ECR_REGISTRY=$(q "$WB_ECR_REGISTRY")
WB_LOG_GROUP=$(q "$WB_LOG_GROUP")
WB_REGION=$(q "$WB_REGION")
ENCODER_SCRIPT_SHA=$(q "$encoder_script_sha")
MEDIAMTX_CONFIG_SHA=$(q "$mediamtx_config_sha")
CADDYFILE_SHA=$(q "$caddyfile_sha")
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
# Back on the camera: compose leaves a running service of an inactive profile alone, even with --remove-orphans.
[ -n "$replay_key" ] || docker compose rm -sf replay >/dev/null
docker image prune -f >/dev/null
[ -z "$replay_key" ] || echo "replay: seahive plays ${replay_file} (looped) in the camera's place; the worker records nothing"

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
    echo "${source_word}: streaming (${body})"
    break
  fi
  n=$((n + 1))
  if [ "$n" -ge "$ready_attempts" ]; then
    echo "${source_word}: no frame yet after ${ready_timeout_s}s; the worker keeps retrying the source. See: docker compose logs worker"
    break
  fi
  sleep "$interval_s"
done

# MediaMTX serves HLS through Caddy: ask for each playlist the way CloudFront does. The header goes in a mode-600
# file (curl -H @file), so the secret is never a command argument. Ready means a 200: without the CDN bearer
# MediaMTX answers 302 (its per-viewer cookie check), which `curl -f` would take for success. Informational,
# never fatal; each failed attempt costs up to 5 s plus the interval, so the message reports the time it took.
hls_timeout_s="${BOOT_HLS_TIMEOUT_S:-60}"
hls_attempts=$(( hls_timeout_s / step ))
[ "$hls_attempts" -ge 1 ] || hls_attempts=1
hdr="$(mktemp)"
trap 'rm -f "$hdr"' EXIT   # an interrupted boot must not leave the secret behind
chmod 600 "$hdr"
printf 'X-Origin-Verify: %s\n' "$origin_secret" > "$hdr"
hls_ready() {  # $1: the MediaMTX path; prints the report line, true once the playlist answers 200
  local path="$1" n=0 code started="$SECONDS"
  while :; do
    code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' -H @"$hdr" "http://127.0.0.1:8080/hls/${path}/index.m3u8" 2>/dev/null)" || true
    if [ "$code" = 200 ]; then
      echo "hls: ${path} ready"
      return 0
    fi
    n=$((n + 1))
    if [ "$n" -ge "$hls_attempts" ]; then
      echo "hls: ${path} not yet after $(( SECONDS - started ))s; see: docker compose logs mediamtx encoder"
      return 1
    fi
    sleep "$interval_s"
  done
}
# No camera, nothing to box: the boxed variant is not waited for, which keeps a boot during a camera outage
# (up to 600 s for /healthz, then the camera's and seahive's waits) well inside the deploy's 900 s.
if hls_ready seahive; then
  hls_ready seahive_boxes || true
else
  echo "hls: seahive_boxes skipped: seahive not ready"
fi
rm -f "$hdr"
