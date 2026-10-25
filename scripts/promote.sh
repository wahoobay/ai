#!/usr/bin/env bash
# Point an environment at a build: re-tag the six images in ECR.
#
#   scripts/promote.sh <staging|prod> <sha12>
#
# sha-<12> -> <env>, sha-<12>-lambda -> <env>-lambda, sha-<12>-cpu -> <env>-cpu,
# for ai-<location>-dashboard, -poller and -worker (LOCATION_ID, default wahoobay).
# Idempotent: a tag that already points at that manifest is reported, not an
# error. Needs ecr:BatchGetImage + ecr:PutImage on the three repositories (the
# environment's deploy role) and AWS_REGION. App Runner auto-deploys when the
# <env> tag moves; the Lambdas are updated by scripts/deploy_env.sh.
set -euo pipefail

env_name="${1:?usage: promote.sh <staging|prod> <sha12>}"
sha="${2:?usage: promote.sh <staging|prod> <sha12>}"
case "$env_name" in
  staging|prod) ;;
  *) echo "::error::environment must be staging or prod, not '$env_name'" >&2; exit 2 ;;
esac
if ! [[ "$sha" =~ ^[0-9a-f]{12}$ ]]; then
  echo "::error::sha must be the 12 hex characters of the sha-<12> tag CI pushed, not '$sha'" >&2
  exit 2
fi
prefix="ai-${LOCATION_ID:-wahoobay}"

retag() {  # repository suffix
  local repo="$1" suffix="$2" from to manifest out
  from="sha-${sha}${suffix}"
  to="${env_name}${suffix}"
  manifest="$(aws ecr batch-get-image --repository-name "$repo" --image-ids "imageTag=${from}" \
              --query 'images[0].imageManifest' --output text)"
  if [ -z "$manifest" ] || [ "$manifest" = "None" ]; then
    echo "::error::${repo}:${from} is not in ECR (did CI push this build?)" >&2
    return 1
  fi
  if out="$(aws ecr put-image --repository-name "$repo" --image-tag "$to" --image-manifest "$manifest" 2>&1)"; then
    echo "${repo}:${to} -> ${from}"
  elif grep -q ImageAlreadyExistsException <<<"$out"; then
    echo "${repo}:${to} already -> ${from}"
  else
    echo "$out" >&2
    return 1
  fi
}

for pair in "dashboard:" "dashboard:-lambda" "poller:" "poller:-lambda" "worker:" "worker:-cpu"; do
  retag "${prefix}-${pair%%:*}" "${pair#*:}"
done
