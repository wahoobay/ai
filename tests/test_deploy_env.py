"""scripts/deploy_env.sh <env> <sha12> deploys a build to an environment (Plan 2b,
Task 3): promote the tags, update the four Lambdas, run the migrations, wait for
App Runner's auto-deploy, smoke the hostname. Fake `aws` and `curl` on PATH record
the calls; nothing reaches AWS or the network."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "deploy_env.sh"
FAKE_AWS = r"""#!/usr/bin/env bash
echo "$*" >> "$FAKE_AWS_LOG"
case "$1 $2" in
  "ecr batch-get-image") echo '{"schemaVersion": 2}' ;;
  "ecr put-image") echo '{}' ;;
  "lambda update-function-code") echo "InProgress" ;;
  "lambda wait") ;;
  "lambda invoke")
    out="${@: -1}"
    if [ "${FAKE_MIGRATE_FAIL:-0}" = "1" ]; then
      echo '{"errorMessage": "relation does not exist"}' > "$out"
      echo '{"StatusCode": 200, "FunctionError": "Unhandled"}'
    else
      echo '{"job": "migrate", "ok": true, "detail": {"revision": "0003"}}' > "$out"
      echo '{"StatusCode": 200}'
    fi ;;
  "ssm get-parameter")
    if [ "${FAKE_NO_GPU_PARAM:-0}" = "1" ]; then echo "An error occurred (ParameterNotFound) when calling the GetParameter operation" >&2; exit 254; fi
    echo i-0param ;;
  "ssm send-command") echo cmd-1 ;;
  "ssm get-command-invocation")
    n=$(cat "$FAKE_SSM_COUNT" 2>/dev/null || echo 0); echo $((n + 1)) > "$FAKE_SSM_COUNT"
    if [ "$n" -eq 0 ]; then echo InProgress; else echo "${FAKE_SSM_END:-Success}"; fi ;;
  "apprunner list-operations")
    n=$(cat "$FAKE_OPS_COUNT" 2>/dev/null || echo 0); echo $((n + 1)) > "$FAKE_OPS_COUNT"
    # 1st call (before the promote): the previous deploy; then the new one appears and finishes.
    if [ "$n" -eq 0 ]; then echo -e "op-old\tUPDATE_SERVICE\tSUCCEEDED"
    elif [ "$n" -eq 1 ]; then echo -e "op-old\tUPDATE_SERVICE\tSUCCEEDED"
    elif [ "$n" -eq 2 ]; then echo -e "op-new\tDEPLOY_SERVICE\tIN_PROGRESS"
    else echo -e "op-new\tDEPLOY_SERVICE\t${FAKE_APPRUNNER_END:-SUCCEEDED}"; fi ;;
  *) echo "unexpected: aws $*" >&2; exit 1 ;;
esac
"""
FAKE_CURL = r"""#!/usr/bin/env bash
echo "curl $*" >> "$FAKE_AWS_LOG"
url="${@: -1}"
if [[ "$*" == *'%{http_code}'* ]]; then
  if [[ "$url" == *awsapprunner* ]]; then echo -n 403; else echo -n 200; fi
elif [[ "$url" == */login ]]; then echo "<html><title>Sign in</title></html>"
elif [[ "$url" == */robots.txt ]]; then printf 'User-agent: *\nDisallow: /\n'
else echo ""; fi
"""


def _run(tmp_path: Path, env_name: str = "staging", **extra: str):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name, body in (("aws", FAKE_AWS), ("curl", FAKE_CURL)):
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("")
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "FAKE_AWS_LOG": str(log),
        "FAKE_OPS_COUNT": str(tmp_path / "ops"),
        "FAKE_SSM_COUNT": str(tmp_path / "ssm"),
        "AWS_REGION": "us-east-1",
        "LOCATION_ID": "wahoobay",
        "ECR_REGISTRY": "226594054743.dkr.ecr.us-east-1.amazonaws.com",
        "FUNCTIONS": f"ai-wahoobay-{env_name}-jobs,ai-wahoobay-{env_name}-migrate,ai-wahoobay-{env_name}-fetch,ai-wahoobay-{env_name}-write",
        "SERVICE_ARN": "arn:aws:apprunner:us-east-1:226594054743:service/x/y",
        "HOSTNAME": f"{env_name}-ai.wahoobay.net",
        "SERVICE_URL": "abc.us-east-1.awsapprunner.com",
        "SLEEP_S": "0",
        **extra,
    }
    r = subprocess.run(
        ["bash", str(SCRIPT), env_name, "abcdef123456"], env=env, capture_output=True, text=True
    )
    return r, log.read_text().splitlines()


def test_a_deploy_promotes_updates_migrates_waits_and_smokes(tmp_path):
    r, calls = _run(tmp_path)
    assert r.returncode == 0, r.stderr + r.stdout
    assert len([c for c in calls if c.startswith("ecr put-image")]) == 6
    updates = [c for c in calls if c.startswith("lambda update-function-code")]
    uris = {
        c.split("--function-name ")[1].split()[0]: c.split("--image-uri ")[1].split()[0]
        for c in updates
    }
    assert uris == {
        "ai-wahoobay-staging-jobs": "226594054743.dkr.ecr.us-east-1.amazonaws.com/ai-wahoobay-dashboard:staging-lambda",
        "ai-wahoobay-staging-migrate": "226594054743.dkr.ecr.us-east-1.amazonaws.com/ai-wahoobay-dashboard:staging-lambda",
        "ai-wahoobay-staging-fetch": "226594054743.dkr.ecr.us-east-1.amazonaws.com/ai-wahoobay-poller:staging-lambda",
        "ai-wahoobay-staging-write": "226594054743.dkr.ecr.us-east-1.amazonaws.com/ai-wahoobay-poller:staging-lambda",
    }
    assert len([c for c in calls if c.startswith("lambda wait function-updated")]) == 4
    invokes = [c for c in calls if c.startswith("lambda invoke")]
    assert (
        len(invokes) == 1
        and "ai-wahoobay-staging-migrate" in invokes[0]
        and '{"job":"migrate"}' in invokes[0]
    )
    # The App Runner wait outlasted the previous operation and saw the new one succeed.
    assert len([c for c in calls if c.startswith("apprunner list-operations")]) >= 4
    curls = [c for c in calls if c.startswith("curl")]
    assert any("staging-ai.wahoobay.net/healthz" in c for c in curls)
    assert any("staging-ai.wahoobay.net/login" in c for c in curls)
    assert any("staging-ai.wahoobay.net/robots.txt" in c for c in curls)
    assert any(
        "awsapprunner.com" in c for c in curls
    )  # the direct URL must answer 403 (origin check)


def test_a_failing_migration_stops_the_deploy_before_the_dashboard_rolls(tmp_path):
    r, calls = _run(tmp_path, FAKE_MIGRATE_FAIL="1")
    assert r.returncode != 0 and "migrate" in (r.stderr + r.stdout)
    assert (
        len([c for c in calls if c.startswith("apprunner list-operations")]) <= 1
    )  # only the "before" lookup
    assert not any(c.startswith("curl") for c in calls)


def test_an_app_runner_rollback_fails_the_deploy(tmp_path):
    r, calls = _run(tmp_path, FAKE_APPRUNNER_END="ROLLBACK_SUCCEEDED")
    assert r.returncode != 0 and "ROLLBACK_SUCCEEDED" in (r.stderr + r.stdout)
    assert not any(c.startswith("curl") for c in calls)


def test_gpu_host_is_updated_to_the_sha_after_the_migration_and_before_the_dashboard_wait(tmp_path):
    r, calls = _run(tmp_path, GPU_INSTANCE_ID="i-0abc")
    assert r.returncode == 0, r.stderr + r.stdout
    sends = [c for c in calls if c.startswith("ssm send-command")]
    assert (
        len(sends) == 1
        and "--instance-ids i-0abc" in sends[0]
        and "update.sh abcdef123456" in sends[0]
    )
    first_ssm = next(i for i, c in enumerate(calls) if c.startswith("ssm send-command"))
    invoke = next(i for i, c in enumerate(calls) if c.startswith("lambda invoke"))
    first_curl = next(i for i, c in enumerate(calls) if c.startswith("curl"))
    assert (
        invoke < first_ssm < first_curl
    )  # the schema first, then the host's worker, then the dashboard is awaited
    assert (
        len([c for c in calls if c.startswith("ssm get-command-invocation")]) >= 2
    )  # InProgress, then Success
    assert "== 4/6 gpu host" in r.stdout
    assert not any(
        c.startswith("ssm get-parameter") for c in calls
    )  # the variable names the host; no lookup


def test_the_gpu_host_is_found_through_its_parameter_when_no_variable_names_it(tmp_path):
    r, calls = _run(tmp_path)
    assert r.returncode == 0, r.stderr + r.stdout
    params = [c for c in calls if c.startswith("ssm get-parameter")]
    assert len(params) == 1 and "/ai/wahoobay/staging/gpu-instance-id" in params[0]
    assert any("--instance-ids i-0param" in c for c in calls if c.startswith("ssm send-command"))


def test_no_gpu_host_means_the_step_is_skipped_not_failed(tmp_path):
    r, calls = _run(tmp_path, FAKE_NO_GPU_PARAM="1")
    assert r.returncode == 0, r.stderr + r.stdout
    assert not any(c.startswith("ssm send-command") for c in calls) and "no GPU host" in r.stdout


def test_a_failed_host_update_fails_the_deploy_before_the_dashboard_is_awaited(tmp_path):
    r, calls = _run(tmp_path, GPU_INSTANCE_ID="i-0abc", FAKE_SSM_END="Failed")
    assert r.returncode != 0 and "Failed" in (r.stderr + r.stdout)
    assert any(c.startswith("lambda invoke") for c in calls)  # the schema was migrated first
    assert not any(c.startswith("curl") for c in calls)
