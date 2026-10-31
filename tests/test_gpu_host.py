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
FAKE_CAMERA_URL = "rtsp://u:p@cam/x"  # what the fake cameras secret returns for seahivecam

FAKE_AWS = r"""#!/usr/bin/env bash
echo "aws $*" >> "$FAKE_LOG"
case "$1 $2" in
  "secretsmanager get-secret-value")
    id=""; while [ $# -gt 0 ]; do [ "$1" = "--secret-id" ] && id="$2"; shift; done
    case "$id" in
      arn:db) echo '{"username":"ai","password":"pw","host":"db.local","port":5432,"dbname":"ai"}' ;;
      */cameras) echo '{"seahivecam":"rtsp://u:p@cam/x","pier":""}' ;;  # FAKE_CAMERA_URL
      arn:origin) echo 'origin-secret' ;;
      */stream-origin-verify) if [ "${FAKE_STREAM_SECRET:-0}" = "1" ]; then echo 'stream-secret'; else echo "ResourceNotFoundException" >&2; exit 254; fi ;;
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
url="${@: -1}"
case "$url" in
  */hls/*/index.m3u8) for a in "$@"; do case "$a" in @*) echo "${a#@}" > "$FAKE_LOG.hdr"; cat "${a#@}" >> "$FAKE_LOG.hdrs" ;; esac; done; [ "${FAKE_HLS:-0}" = "1" ] && exit 0; exit 22 ;;
  */readyz) [ "${FAKE_READYZ_FAILS:-0}" != "1" ] && echo '{"ready": true, "frame_id": 42}'; [ "${FAKE_READYZ_FAILS:-0}" != "1" ] ;;
  *)
    [ "${FAKE_CURL_FAILS:-0}" = "1" ] && exit 7
    n=$(cat "$FAKE_LOG.curl" 2>/dev/null || echo 0); echo $((n + 1)) > "$FAKE_LOG.curl"
    [ "$n" -ge 1 ] ;;  # the worker answers on the second probe
esac
"""
FAKE_GIT = r"""#!/usr/bin/env bash
echo "git $*" >> "$FAKE_LOG"
if [ "$1 $2" = "rev-parse --is-shallow-repository" ]; then echo "${FAKE_GIT_SHALLOW:-false}"; fi
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


def _update(tmp_path: Path, ref: str, **extra: str):
    checkout, log, env = _machine(tmp_path)
    proc = subprocess.run(
        ["bash", str(checkout / "deploy" / "gpu-host" / "update.sh"), ref],
        capture_output=True,
        text=True,
        env={**env, **extra},
        timeout=60,
    )
    return proc, log


def test_boot_writes_the_env_the_worker_needs_and_prints_no_secret(tmp_path):
    proc, env_file, log = _boot(tmp_path)
    assert proc.returncode == 0, proc.stderr
    text = env_file.read_text()
    assert (
        f"CAMERA_SEAHIVE_URL='{FAKE_CAMERA_URL}'" in text
    )  # single-quoted: compose must not interpolate a $ in a password
    env = dict(
        (k, v.strip("'"))
        for k, v in (
            line.split("=", 1)
            for line in text.splitlines()
            if "=" in line and not line.startswith("#")
        )
    )
    assert env["DATABASE_URL"] == "postgresql://ai:pw@db.local:5432/ai"
    assert (
        env["AWS_REGION"] == "us-east-1" and env["AWS_DEFAULT_REGION"] == "us-east-1"
    )  # boto3 inside the container
    assert (
        env["CLASSIFIER_V2_COMPILE"] == "false"
    )  # the cuda image has no compiler for triton; Phase 0 measures without it
    assert (
        env["VIDEO_SOURCE"] == "rtsp://mediamtx:8554/seahive"
        and env["SOURCE_LABEL_PRIMARY"] == "seahivecam"
    )
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
    assert "pw" not in out and FAKE_CAMERA_URL not in out and "origin-secret" not in out
    calls = log.read_text()
    assert "docker login" in calls and "compose pull" in calls and "compose up -d" in calls
    assert calls.index("docker login") < calls.index("compose pull") < calls.index("compose up -d")
    assert "camera: streaming" in proc.stdout  # /readyz answered


def test_boot_reports_a_camera_that_sends_no_frames_but_still_succeeds(tmp_path):
    proc, _, log = _boot(tmp_path, FAKE_READYZ_FAILS="1", BOOT_READY_TIMEOUT_S="1")
    assert proc.returncode == 0, proc.stderr
    assert "camera: no frame yet" in proc.stdout and "docker compose logs worker" in proc.stdout
    assert "readyz" in log.read_text()


def test_boot_prefers_the_stream_origins_own_secret_when_it_exists(tmp_path):
    """The stream origin got its own header secret (/ai/<location>/<env>/stream-origin-verify) after the first
    hosts booted with the dashboard's origin secret in host.env. boot.sh looks the new one up by name, so an
    existing host follows the change on its next boot.sh run, and falls back to host.env's ARN before it exists."""
    proc, env_file, log = _boot(tmp_path, FAKE_STREAM_SECRET="1")
    assert proc.returncode == 0, proc.stderr
    assert "ORIGIN_VERIFY_SECRET='stream-secret'" in env_file.read_text()
    assert "/ai/wahoobay/staging/stream-origin-verify" in log.read_text()
    assert "stream-secret" not in proc.stdout + proc.stderr


def test_boot_prunes_dangling_images_before_the_pull_and_after_the_start(tmp_path):
    """Every deploy pulls a ~14 GB worker image and left the previous one dangling: after four deploys the
    100 GB root disk was 96% full and the next pull failed, taking the SSM document worker down with it
    (2026-10-27). Dangling images are pruned before the pull (room for the new image next to the running
    one) and again after the start (the image just replaced)."""
    proc, _env, log = _boot(tmp_path)
    assert proc.returncode == 0, proc.stderr
    calls = log.read_text().splitlines()
    prunes = [i for i, c in enumerate(calls) if c == "docker image prune -f"]
    pull = next(i for i, c in enumerate(calls) if c.startswith("docker compose pull"))
    up = next(i for i, c in enumerate(calls) if c.startswith("docker compose up -d"))
    assert len(prunes) == 2 and prunes[0] < pull < up < prunes[1]


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
    assert "git fetch -q origin" in calls and "git checkout -q abc123def456" in calls
    assert calls.index("git checkout") < calls.index("compose up -d")
    assert "--unshallow" not in calls  # a full clone needs no deepening


def test_update_deepens_a_shallow_clone_so_older_commits_can_be_checked_out(tmp_path):
    proc, log = _update(tmp_path, "abc123def456", FAKE_GIT_SHALLOW="true")
    assert proc.returncode == 0, proc.stderr
    calls = log.read_text()
    assert "git fetch --unshallow" in calls and calls.index("--unshallow") < calls.index(
        "git checkout"
    )


def test_caddy_verifies_the_origin_header_on_the_stream_route_and_never_logs_it():
    """The review found the first version proxied /stream/* before checking the header (Caddy
    orders directives globally) and logged the header's value on every 502. The header is now
    part of the stream matcher itself, stripped before the proxy, and filtered from the logs."""
    caddy = (GPU_HOST / "Caddyfile").read_text()
    assert ":8080" in caddy and "header X-Origin-Verify {$ORIGIN_VERIFY_SECRET}" in caddy
    assert "request_header -X-Origin-Verify" in caddy and "X-Origin-Verify delete" in caddy
    assert (
        "reverse_proxy worker:8081" in caddy
        and "flush_interval -1" in caddy
        and "respond 403" in caddy
    )
    assert "path /hls/*" in caddy and "reverse_proxy mediamtx:8888" in caddy
    if shutil.which("docker"):
        import json

        adapted = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{GPU_HOST / 'Caddyfile'}:/etc/caddy/Caddyfile:ro",
                "-e",
                "ORIGIN_VERIFY_SECRET=s3cret",
                "caddy:2.8.4",
                "caddy",
                "adapt",
                "--config",
                "/etc/caddy/Caddyfile",
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=True,
        ).stdout
        servers = json.loads(adapted)["apps"]["http"]["servers"]
        stream_route = next(
            r for srv in servers.values() for r in srv["routes"] if "/stream/*" in json.dumps(r)
        )
        matcher = json.dumps(stream_route["match"])
        assert (
            "X-Origin-Verify" in matcher and "s3cret" in matcher
        )  # the header is part of the stream match itself
        hls_route = next(
            r for srv in servers.values() for r in srv["routes"] if "/hls/*" in json.dumps(r)
        )
        hls_matcher = json.dumps(hls_route["match"])
        assert "X-Origin-Verify" in hls_matcher and "s3cret" in hls_matcher
        assert "mediamtx:8888" in json.dumps(hls_route)
        compact = json.dumps(servers).replace(" ", "")
        assert '"delete":["X-Origin-Verify"]' in compact  # stripped before the worker sees it
        assert '"filter":"delete"' in json.dumps(json.loads(adapted)).replace(
            " ", ""
        )  # and filtered from the logs


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
                "CAMERA_SEAHIVE_URL": "rtsp://x",
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


def test_mediamtx_pulls_the_camera_once_and_serves_plain_hls():
    import yaml

    cfg = yaml.safe_load((GPU_HOST / "mediamtx.yml").read_text())
    assert (
        cfg["rtsp"] is True and cfg["rtspAddress"] == ":8554" and cfg["rtspTransports"] == ["tcp"]
    )
    assert cfg["hls"] is True and cfg["hlsAddress"] == ":8888"
    assert cfg["hlsVariant"] == "fmp4" and cfg["hlsSegmentDuration"] == "1s"
    assert cfg["hlsSegmentCount"] == 7
    assert (
        cfg["hlsAlwaysRemux"] is True
    )  # the muxers run without viewers: the first viewer and boot's check start at once
    assert cfg["api"] is False and cfg["metrics"] is False and cfg["rtmp"] is False
    assert cfg["webrtc"] is False and cfg["moq"] is False
    assert cfg["pathDefaults"]["rtspTransport"] == "tcp"
    assert cfg["pathDefaults"]["sourceOnDemand"] is False
    assert set(cfg["paths"]) == {"seahive", "seahive_boxes"}
    assert "source" not in (
        cfg["paths"]["seahive"] or {}
    )  # the camera URL arrives as MTX_PATHS_SEAHIVE_SOURCE, never in the file


def test_no_file_on_the_host_carries_the_camera_url():
    for name in (
        "mediamtx.yml",
        "docker-compose.yml",
        "Caddyfile",
        "encoder.sh",
        "boot.sh",
        "README.md",
    ):
        path = GPU_HOST / name
        if path.exists():
            assert "rtsp://" not in path.read_text().replace("rtsp://mediamtx:8554", ""), name


def test_compose_runs_mediamtx_on_the_internal_network_with_the_camera_from_env():
    text = (GPU_HOST / "docker-compose.yml").read_text()
    assert "bluenviron/mediamtx:1.21.3" in text
    assert "MTX_PATHS_SEAHIVE_SOURCE: ${CAMERA_SEAHIVE_URL}" in text
    assert (
        '"8554:' not in text and '"8888:' not in text
    )  # reached only by the worker, the encoder and Caddy
    assert "./mediamtx.yml:/mediamtx.yml:ro" in text
    import yaml

    caddy = yaml.safe_load(text)["services"]["caddy"]
    assert "env_file" not in caddy  # .env also holds the camera URL and the database URL
    assert caddy["environment"] == {"ORIGIN_VERIFY_SECRET": "${ORIGIN_VERIFY_SECRET}"}


def test_boot_points_the_worker_at_mediamtx_and_reads_only_from_it(tmp_path):
    proc, env_file, _log = _boot(tmp_path)
    assert proc.returncode == 0, proc.stderr
    env = env_file.read_text()
    assert "VIDEO_SOURCE='rtsp://mediamtx:8554/seahive'" in env
    assert (
        f"CAMERA_SEAHIVE_URL='{FAKE_CAMERA_URL}'" in env
    )  # consumed only by compose, for MediaMTX
    assert "FALLBACK_VIDEO_SOURCE=''" in env  # the worker never opens a second pull of the camera
    assert env.count(FAKE_CAMERA_URL) == 1


def test_boot_reports_the_hls_playlists_without_failing_or_printing_the_secret(tmp_path):
    proc, _env, _log = _boot(tmp_path, FAKE_HLS="1")
    assert proc.returncode == 0 and "hls: seahive ready" in proc.stdout
    assert "hls: seahive_boxes ready" in proc.stdout
    out = proc.stdout + proc.stderr
    assert "origin-secret" not in out and "stream-secret" not in out
    proc, _env, log = _boot(tmp_path, reuse=True)
    assert proc.returncode == 0 and "hls: seahive not yet" in proc.stdout
    assert "origin-secret" not in log.read_text()  # the header goes in a file, never an argument
    hdr = Path(Path(f"{log}.hdr").read_text().strip())
    assert not hdr.exists()  # removed once the report is done
