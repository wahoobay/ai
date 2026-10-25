"""deploy/gpu-host: what runs on the AWS GPU host (Plan 4a). boot.sh turns the stack's
/etc/wahoobay/host.env plus three secrets into the worker's .env, logs in to ECR,
starts the compose stack and waits for the worker's /healthz; update.sh checks out a
ref and boots. Fake aws/docker/curl/git on PATH stand in for the machine."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GPU_HOST = REPO / "deploy" / "gpu-host"

FAKE_AWS = r"""#!/usr/bin/env bash
echo "aws $*" >> "$FAKE_LOG"
case "$1 $2" in
  "secretsmanager get-secret-value")
    id=""; while [ $# -gt 0 ]; do [ "$1" = "--secret-id" ] && id="$2"; shift; done
    case "$id" in
      arn:db) echo '{"username":"ai","password":"pw","host":"db.local","port":5432,"dbname":"ai"}' ;;
      */cameras) echo '{"seahivecam":"rtsp://u:p@cam/x","pier":""}' ;;
      arn:origin) echo 'origin-secret' ;;
      *) echo "unknown secret $id" >&2; exit 1 ;;
    esac ;;
  "ecr get-login-password") echo token ;;
  *) echo "unexpected: aws $*" >&2; exit 1 ;;
esac
"""
FAKE_DOCKER = r"""#!/usr/bin/env bash
echo "docker $*" >> "$FAKE_LOG"
if [ "$1" = "login" ]; then cat >/dev/null; fi
"""
FAKE_CURL = r"""#!/usr/bin/env bash
echo "curl $*" >> "$FAKE_LOG"
[ "${FAKE_CURL_FAILS:-0}" = "1" ] && exit 7
n=$(cat "$FAKE_LOG.curl" 2>/dev/null || echo 0); echo $((n + 1)) > "$FAKE_LOG.curl"
[ "$n" -ge 1 ]  # the worker answers on the second probe
"""
FAKE_GIT = r"""#!/usr/bin/env bash
echo "git $*" >> "$FAKE_LOG"
"""
HOST_ENV = """WB_ENV=staging
WB_LOCATION_ID=wahoobay
WB_TZ=America/New_York
WB_DEPLOYMENT_URI=wahoo_2
WB_REGION=us-east-1
WB_DB_SECRET_ARN=arn:db
WB_CAMERAS_SECRET=/ai/wahoobay/staging/cameras
WB_ORIGIN_SECRET_ARN=arn:origin
WB_FRAMES_BUCKET=frames-bucket
WB_MODELS_BUCKET=models-bucket
WB_ECR_REGISTRY=123.dkr.ecr.us-east-1.amazonaws.com
WB_LOG_GROUP=/ai/wahoobay/staging/gpu-host
WB_AI_REF=main
"""


def _machine(tmp_path: Path, reuse: bool = False) -> tuple[Path, Path, dict[str, str]]:
    checkout = tmp_path / "checkout"
    if not reuse:
        shutil.copytree(GPU_HOST, checkout / "deploy" / "gpu-host")
        bindir = tmp_path / "bin"
        bindir.mkdir()
        for name, body in (
            ("aws", FAKE_AWS),
            ("docker", FAKE_DOCKER),
            ("curl", FAKE_CURL),
            ("git", FAKE_GIT),
        ):
            (bindir / name).write_text(body)
            (bindir / name).chmod(0o755)
        (tmp_path / "host.env").write_text(HOST_ENV)
    log = tmp_path / "calls.log"
    log.write_text("")
    for stale in tmp_path.glob("calls.log.*"):
        stale.unlink()
    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "FAKE_LOG": str(log),
        "WB_HOST_ENV": str(tmp_path / "host.env"),
        "WB_CHECKOUT": str(checkout),
        "BOOT_HEALTH_TIMEOUT_S": "5",
        "BOOT_HEALTH_INTERVAL_S": "0",
    }
    return checkout, log, env


def _boot(tmp_path: Path, reuse: bool = False, **extra: str):
    checkout, log, env = _machine(tmp_path, reuse)
    proc = subprocess.run(
        ["bash", str(checkout / "deploy" / "gpu-host" / "boot.sh")],
        capture_output=True,
        text=True,
        env={**env, **extra},
        timeout=60,
    )
    return proc, checkout / "deploy" / "gpu-host" / ".env", log


def _update(tmp_path: Path, ref: str):
    checkout, log, env = _machine(tmp_path)
    proc = subprocess.run(
        ["bash", str(checkout / "deploy" / "gpu-host" / "update.sh"), ref],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    return proc, log


def test_boot_writes_the_env_the_worker_needs_and_prints_no_secret(tmp_path):
    proc, env_file, log = _boot(tmp_path)
    assert proc.returncode == 0, proc.stderr
    env = dict(line.split("=", 1) for line in env_file.read_text().splitlines() if "=" in line)
    assert env["DATABASE_URL"] == "postgresql://ai:pw@db.local:5432/ai"
    assert env["VIDEO_SOURCE"] == "rtsp://u:p@cam/x" and env["SOURCE_LABEL_PRIMARY"] == "seahivecam"
    assert env["FRAMES_URL"] == "s3://frames-bucket/" and env["MODELS_URL"] == "s3://models-bucket/"
    assert (
        env["DEVICE"] == "cuda:0"
        and env["WORKER_HTTP_PORT"] == "8081"
        and env["LOCATION_ID"] == "wahoobay"
    )
    assert env["TZ"] == "America/New_York" and env["SENSESTREAM_DEPLOYMENT_URI"] == "wahoo_2"
    assert env["ORIGIN_VERIFY_SECRET"] == "origin-secret" and env["WB_IMAGE_TAG"] == "staging"
    assert oct(env_file.stat().st_mode)[-3:] == "600"
    out = proc.stdout + proc.stderr
    assert "pw" not in out and "rtsp://u:p" not in out and "origin-secret" not in out
    calls = log.read_text()
    assert "docker login" in calls and "compose pull" in calls and "compose up -d" in calls
    assert calls.index("docker login") < calls.index("compose pull") < calls.index("compose up -d")


def test_boot_fails_when_the_worker_never_becomes_healthy(tmp_path):
    proc, _, _ = _boot(tmp_path, FAKE_CURL_FAILS="1", BOOT_HEALTH_TIMEOUT_S="1")
    assert proc.returncode == 1 and "healthz" in proc.stdout + proc.stderr


def test_boot_is_idempotent(tmp_path):
    first, env_file, _ = _boot(tmp_path)
    second, env_file2, _ = _boot(tmp_path, reuse=True)
    assert first.returncode == 0 and second.returncode == 0
    assert env_file2.read_text() == env_file.read_text()


def test_update_checks_out_the_ref_then_boots(tmp_path):
    proc, log = _update(tmp_path, "abc123def456")
    assert proc.returncode == 0, proc.stderr
    calls = log.read_text()
    assert "git fetch" in calls and "git checkout -q abc123def456" in calls
    assert calls.index("git checkout") < calls.index("compose up -d")


def test_caddy_verifies_the_origin_header_and_streams():
    caddy = (GPU_HOST / "Caddyfile").read_text()
    assert (
        ":8080" in caddy
        and "X-Origin-Verify {$ORIGIN_VERIFY_SECRET}" in caddy
        and "respond @bad 403" in caddy
    )
    assert (
        "handle_path /stream/*" in caddy
        and "reverse_proxy worker:8081" in caddy
        and "flush_interval -1" in caddy
    )


def test_compose_runs_the_built_worker_image_on_the_gpu_with_cloudwatch_logs(tmp_path):
    text = (GPU_HOST / "docker-compose.yml").read_text()
    assert "${WB_ECR_REGISTRY}/ai-wahoobay-worker:${WB_IMAGE_TAG}" in text
    assert "capabilities: [gpu]" in text and "awslogs" in text and "restart: unless-stopped" in text
    assert "caddy:2" in text and '"8080:8080"' in text and "8081" in text
    if shutil.which("docker"):
        shutil.copytree(GPU_HOST, tmp_path / "gpu-host")
        (tmp_path / "gpu-host" / ".env").write_text("WB_LOG_GROUP=g\nWB_REGION=us-east-1\n")
        subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(tmp_path / "gpu-host" / "docker-compose.yml"),
                "config",
                "-q",
            ],
            check=True,
            env={
                **os.environ,
                "WB_ECR_REGISTRY": "r",
                "WB_IMAGE_TAG": "staging",
                "WB_LOG_GROUP": "g",
                "WB_REGION": "us-east-1",
            },
            timeout=120,
        )


def test_the_systemd_unit_boots_the_host_after_docker():
    unit = (GPU_HOST / "wahoobay-gpu-host.service").read_text()
    assert "After=network-online.target docker.service" in unit
    assert (
        "ExecStart=/opt/wahoobay/deploy/gpu-host/boot.sh" in unit
        and "WantedBy=multi-user.target" in unit
    )
