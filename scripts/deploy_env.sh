#!/usr/bin/env bash
# Deploy a build to an environment (the AWS shape; Plan 2b of wahoobay/infra).
#
#   scripts/deploy_env.sh <staging|prod> <sha12>
#
# 1. promote.sh points the environment's image tags at the build (App Runner
#    auto-deploys the dashboard when its tag moves);
# 2. the four Lambdas (jobs, migrate, fetch, write) are updated to the
#    <env>-lambda images and waited for;
# 3. the migrate Lambda runs ({"job": "migrate"}); a FunctionError stops here,
#    before the dashboard rolls onto a schema it does not have;
# 4. App Runner's new operation is awaited: SUCCEEDED, or FAILED /
#    ROLLBACK_* fails the deploy (the previous revision keeps serving);
# 5. smoke: https://HOSTNAME/healthz 200, /login is the sign-in page,
#    /robots.txt answers, and the direct App Runner URL (SERVICE_URL, if set)
#    answers 403 -- only CloudFront may reach the service.
#
# Environment: FUNCTIONS (comma-separated Lambda names, from the stack's
# FunctionNames output), SERVICE_ARN (App Runner; the ServiceArn output),
# HOSTNAME (the environment's public hostname), ECR_REGISTRY, AWS_REGION,
# LOCATION_ID (default wahoobay), SERVICE_URL (optional), APPRUNNER_TIMEOUT_S
# (default 900), SLEEP_S (poll interval, default 15).
set -euo pipefail

env_name="${1:?usage: deploy_env.sh <staging|prod> <sha12>}"
sha="${2:?usage: deploy_env.sh <staging|prod> <sha12>}"
: "${FUNCTIONS:?FUNCTIONS (comma-separated Lambda names) is required}"
: "${SERVICE_ARN:?SERVICE_ARN (App Runner service) is required}"
: "${HOSTNAME:?HOSTNAME (the public hostname of the environment) is required}"
: "${ECR_REGISTRY:?ECR_REGISTRY is required}"
here="$(cd "$(dirname "$0")" && pwd)"
prefix="ai-${LOCATION_ID:-wahoobay}"
sleep_s="${SLEEP_S:-15}"

latest_operation() {  # prints: id type status
  aws apprunner list-operations --service-arn "$SERVICE_ARN" --max-results 1 \
    --query 'OperationSummaryList[0].[Id,Type,Status]' --output text 2>/dev/null || echo "none - -"
}

echo "== 1/5 promote ${sha} -> ${env_name}"
read -r before_id _ _ <<<"$(latest_operation)"
"$here/promote.sh" "$env_name" "$sha"

echo "== 2/5 lambdas"
IFS=, read -ra functions <<<"$FUNCTIONS"
migrate_fn=""
for fn in "${functions[@]}"; do
  case "$fn" in
    *-jobs|*-migrate) repo="dashboard" ;;
    *-fetch|*-write) repo="poller" ;;
    *) echo "::error::unknown function '$fn' (expected *-jobs, *-migrate, *-fetch or *-write)" >&2; exit 1 ;;
  esac
  [[ "$fn" == *-migrate ]] && migrate_fn="$fn"
  status="$(aws lambda update-function-code --function-name "$fn" \
    --image-uri "${ECR_REGISTRY}/${prefix}-${repo}:${env_name}-lambda" \
    --query 'LastUpdateStatus' --output text)"
  echo "${fn}: ${status}"
done
for fn in "${functions[@]}"; do
  aws lambda wait function-updated --function-name "$fn"
done
[ -n "$migrate_fn" ] || { echo "::error::no *-migrate function in FUNCTIONS" >&2; exit 1; }

echo "== 3/5 migrate"
out="$(mktemp)"
response="$(aws lambda invoke --function-name "$migrate_fn" --payload '{"job":"migrate"}' \
  --cli-binary-format raw-in-base64-out "$out")"
if grep -q FunctionError <<<"$response"; then
  echo "::error::migrate failed; the dashboard was not rolled. Lambda said: $(cat "$out")" >&2
  exit 1
fi
cat "$out"; echo

echo "== 4/5 app runner"
deadline=$(( $(date +%s) + ${APPRUNNER_TIMEOUT_S:-900} ))
while :; do
  read -r op_id op_type op_status <<<"$(latest_operation)"
  if [ -n "$op_id" ] && [ "$op_id" != "none" ] && [ "$op_id" != "$before_id" ]; then
    case "$op_status" in
      SUCCEEDED) echo "app runner: ${op_type} ${op_status}"; break ;;
      FAILED|ROLLBACK_SUCCEEDED|ROLLBACK_FAILED)
        echo "::error::app runner: ${op_type} ${op_status}; the previous revision keeps serving" >&2
        exit 1 ;;
      *) echo "app runner: ${op_type} ${op_status} ..." ;;
    esac
  else
    echo "app runner: waiting for the deployment to start ..."
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "::error::app runner did not finish within ${APPRUNNER_TIMEOUT_S:-900}s" >&2
    exit 1
  fi
  sleep "$sleep_s"
done

echo "== 5/5 smoke https://${HOSTNAME}"
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "https://${HOSTNAME}/healthz")"
[ "$code" = "200" ] || { echo "::error::/healthz answered ${code}" >&2; exit 1; }
curl -s --max-time 20 "https://${HOSTNAME}/login" | grep -q '<title>' || { echo "::error::/login is not the sign-in page" >&2; exit 1; }
curl -s --max-time 20 "https://${HOSTNAME}/robots.txt" | grep -q 'User-agent' || { echo "::error::/robots.txt did not answer" >&2; exit 1; }
if [ -n "${SERVICE_URL:-}" ]; then
  direct="${SERVICE_URL#https://}"
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "https://${direct%/}/login")"
  [ "$code" = "403" ] || { echo "::error::the direct App Runner URL answered ${code}; expected 403 (origin check)" >&2; exit 1; }
fi
echo "deployed ${sha} to ${env_name} (https://${HOSTNAME})"
