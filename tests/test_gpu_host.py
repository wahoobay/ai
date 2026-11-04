"""deploy/gpu-host: what runs on the AWS GPU host (Plan 4a). boot.sh turns the stack's
/etc/wahoobay/host.env plus three secrets into the worker's .env, logs in to ECR,
starts the compose stack and waits for the worker's /healthz; update.sh checks out a
ref and boots. Fake aws/docker/curl/git on PATH stand in for the machine."""

from __future__ import annotations

import hashlib
import http.client
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
GPU_HOST = REPO / "deploy" / "gpu-host"
FAKE_CAMERA_URL = "rtsp://u:p@cam/x"  # what the fake cameras secret returns for seahivecam

FAKE_AWS = rf"""#!/usr/bin/env bash
echo "aws $*" >> "$FAKE_LOG"
case "$1 $2" in
  "secretsmanager get-secret-value")
    id=""; while [ $# -gt 0 ]; do [ "$1" = "--secret-id" ] && id="$2"; shift; done
    case "$id" in
      arn:db) echo '{{"username":"ai","password":"pw","host":"db.local","port":5432,"dbname":"ai"}}' ;;
      */cameras) echo '{{"seahivecam":"{FAKE_CAMERA_URL}","pier":""}}' ;;
      arn:origin) echo 'origin-secret' ;;
      */stream-origin-verify) if [ "${{FAKE_STREAM_SECRET:-0}}" = "1" ]; then echo 'stream-secret'; else echo "ResourceNotFoundException" >&2; exit 254; fi ;;
      *) echo "unknown secret $id" >&2; exit 1 ;;
    esac ;;
  "ecr get-login-password") echo token ;;
  "s3 cp")  # a recording replay.conf names: FAKE_S3_MISSING=1 for an object that is not there
    [ "${{FAKE_S3_MISSING:-0}}" = "1" ] && {{ echo "fatal error: An error occurred (404) when calling the HeadObject operation" >&2; exit 1; }}
    echo "fake mp4" > "${{@: -1}}" ;;
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
  */hls/*/index.m3u8)
    for a in "$@"; do case "$a" in @*) echo "${a#@}" > "$FAKE_LOG.hdr"; cat "${a#@}" >> "$FAKE_LOG.hdrs" ;; esac; done
    # FAKE_HLS=1: the playlist; 302: MediaMTX's per-viewer cookie redirect (no CDN bearer); otherwise 404.
    case "${FAKE_HLS:-0}" in 1) code=200 ;; 302) code=302 ;; *) code=404 ;; esac
    write=0; fail=0
    for a in "$@"; do case "$a" in -w) write=1 ;; -f|--fail|-[!-]*f*) fail=1 ;; esac; done
    [ "$write" = 1 ] && printf '%s' "$code"
    [ "$fail" = 1 ] && [ "$code" -ge 400 ] && exit 22  # like curl: -f fails on >= 400 only, so a 302 exits 0
    exit 0 ;;
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
        # The tests boot the live camera unless they name a recording; the committed file has its own test.
        (checkout / "deploy" / "gpu-host" / "replay.conf").write_text("staging=\nprod=\n")
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
        "WB_DATA_DIR": str(tmp_path / "data"),
        "BOOT_HEALTH_TIMEOUT_S": "5",
        "BOOT_HEALTH_INTERVAL_S": "0",
    }
    return checkout, log, env


def _boot(tmp_path: Path, reuse: bool = False, replay_conf: str | None = None, **extra: str):
    checkout, log, env = _machine(tmp_path, reuse)
    if replay_conf is not None:
        (checkout / "deploy" / "gpu-host" / "replay.conf").write_text(replay_conf)
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
        f"SEAHIVE_SOURCE='{FAKE_CAMERA_URL}'" in text
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
    # One hash per bind-mounted file, each a label on its service: a changed file recreates its container
    # (a single-file bind mount keeps the old inode after git replaces the file).
    for var, name in (
        ("ENCODER_SCRIPT_SHA", "encoder.sh"),
        ("MEDIAMTX_CONFIG_SHA", "mediamtx.yml"),
        ("CADDYFILE_SHA", "Caddyfile"),
    ):
        assert env[var] == hashlib.sha256((GPU_HOST / name).read_bytes()).hexdigest()[:12], var
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


def test_boot_derives_mediamtxs_cdn_bearer_from_the_origin_secret_and_never_prints_it(tmp_path):
    """MediaMTX (>= 1.21.1) gives each HLS viewer its own session (a cookie and a redirect) unless the request
    carries its CDN bearer; CloudFront forwards no cookie or query string, so Caddy adds the bearer. It is
    derived on the host from the origin secret (nothing new in Secrets Manager, the origin secret never reaches
    MediaMTX) and is stable across boots, so `compose up -d` does not recreate mediamtx and caddy every time."""
    for extra, origin in (({}, b"origin-secret"), ({"FAKE_STREAM_SECRET": "1"}, b"stream-secret")):
        proc, env_file, log = _boot(tmp_path / origin.decode(), FAKE_HLS="1", **extra)
        assert proc.returncode == 0, proc.stderr
        bearer = hashlib.sha256(origin).hexdigest()
        text = env_file.read_text()
        assert f"HLS_CDN_SECRET='{bearer}'" in text
        assert text.index("ORIGIN_VERIFY_SECRET=") < text.index("HLS_CDN_SECRET=")  # next to it
        assert bearer not in proc.stdout + proc.stderr
        assert bearer not in log.read_text()  # never a command argument
        assert bearer not in Path(f"{log}.hdrs").read_text()  # Caddy adds it, not boot


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


def _find_handler(node, name: str) -> dict:
    """The first handler called `name` anywhere in an adapted Caddy JSON route."""
    if isinstance(node, dict):
        if node.get("handler") == name:
            return node
        node = list(node.values())
    if isinstance(node, list):
        for child in node:
            found = _find_handler(child, name)
            if found:
                return found
    return {}


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
    assert 'header_up Authorization "Bearer {$HLS_CDN_SECRET}"' in caddy  # MediaMTX's CDN mode
    assert "header_up -Cookie" in caddy  # the dashboard's session cookie never reaches MediaMTX
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
                "-e",
                "HLS_CDN_SECRET=cdn",
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
        proxy = _find_handler(hls_route, "reverse_proxy")
        assert proxy["headers"]["request"]["set"]["Authorization"] == ["Bearer cdn"]
        assert proxy["headers"]["request"]["delete"] == ["Cookie"]
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
    assert "jrottenberg/ffmpeg:7.1.2-nvidia2404" in text
    assert "NVIDIA_DRIVER_CAPABILITIES: compute,video,utility" in text
    assert "capabilities: [gpu, video]" in text and "encoder:" in text
    assert "./encoder.sh:/encoder.sh:ro" in text and "awslogs-stream: encoder" in text
    # A changed bind-mounted file recreates its container (the hashes come from boot.sh's .env).
    assert "wahoobay.encoder-script: ${ENCODER_SCRIPT_SHA}" in text
    assert (
        "wahoobay.config: ${MEDIAMTX_CONFIG_SHA}" in text
        and "wahoobay.config: ${CADDYFILE_SHA}" in text
    )
    if shutil.which("docker"):
        shutil.copytree(GPU_HOST, tmp_path / "gpu-host")
        (tmp_path / "gpu-host" / ".env").write_text(
            "WB_LOG_GROUP=g\nWB_REGION=us-east-1\nENCODER_SCRIPT_SHA=abc\nMEDIAMTX_CONFIG_SHA=abc\nCADDYFILE_SHA=abc\n"
        )
        proc = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(tmp_path / "gpu-host" / "docker-compose.yml"),
                "config",
                "-q",
            ],
            check=True,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "WB_ECR_REGISTRY": "r",
                "WB_IMAGE_TAG": "staging",
                "WB_LOG_GROUP": "g",
                "WB_REGION": "us-east-1",
                "SEAHIVE_SOURCE": "rtsp://x",
                "REPLAY_FILE": "r.mp4",
                "HLS_CDN_SECRET": "cdn",
                "ORIGIN_VERIFY_SECRET": "ov",
                "ENCODER_SCRIPT_SHA": "abc",
                "MEDIAMTX_CONFIG_SHA": "abc",
                "CADDYFILE_SHA": "abc",
            },
            timeout=120,
        )
        assert (
            "is not set" not in proc.stderr
        ), proc.stderr  # every ${...} the file uses is accounted for


def test_the_systemd_unit_boots_the_host_after_docker():
    unit = (GPU_HOST / "wahoobay-gpu-host.service").read_text()
    assert "After=network-online.target docker.service" in unit
    assert (
        "ExecStart=/opt/wahoobay/deploy/gpu-host/boot.sh" in unit
        and "WantedBy=multi-user.target" in unit
    )


def test_mediamtx_pulls_the_camera_once_and_serves_plain_hls():
    cfg = yaml.safe_load((GPU_HOST / "mediamtx.yml").read_text())
    assert (
        cfg["rtsp"] is True and cfg["rtspAddress"] == ":8554" and cfg["rtspTransports"] == ["tcp"]
    )
    assert cfg["hls"] is True and cfg["hlsAddress"] == ":8888"
    assert (
        cfg["hlsVariant"] == "fmp4" and cfg["hlsSegmentDuration"] == "2s"
    )  # the cost knob (README)
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


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker is not on the PATH")
def test_mediamtx_serves_hls_as_a_cdn_session_only_with_the_bearer():
    """MediaMTX 1.21.3 with this mediamtx.yml: with the CDN bearer a playlist request proceeds (404: the
    camera is unreachable here) with no cookie and no redirect; without it, the per-viewer cookie redirect
    (302) that CloudFront cannot carry. Pins the mode Caddy relies on."""
    port = _free_port()
    name = f"wb-test-mediamtx-cdn-{os.getpid()}-{port}"

    def status(headers: dict[str, str]) -> int:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)  # never follows redirects
        try:
            conn.request("GET", "/seahive/index.m3u8", headers=headers)
            return conn.getresponse().status
        finally:
            conn.close()

    try:
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "-v",
                f"{GPU_HOST / 'mediamtx.yml'}:/mediamtx.yml:ro",
                "-e",
                "MTX_PATHS_SEAHIVE_SOURCE=rtsp://127.0.0.1:1/x",
                "-e",
                "MTX_HLSCDNSECRET=cdn",
                "-p",
                f"127.0.0.1:{port}:8888",
                "bluenviron/mediamtx:1.21.3",
            ],
            capture_output=True,
            text=True,
            timeout=300,  # the first run pulls the image
            check=True,
        )
        deadline = time.monotonic() + 30
        while True:  # Docker's port proxy accepts before MediaMTX listens: wait for an HTTP answer
            try:
                with_bearer = status({"Authorization": "Bearer cdn"})
                break
            except (OSError, http.client.HTTPException):
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)
        assert with_bearer == 404  # the CDN session proceeded; the path has no source
        assert status({}) == 302  # per-viewer session: the cookie-check redirect
        assert status({"Authorization": "Bearer wrong"}) == 302
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)


def test_no_file_on_the_host_carries_the_camera_url():
    for name in (
        "mediamtx.yml",
        "docker-compose.yml",
        "Caddyfile",
        "encoder.sh",
        "boot.sh",
        "README.md",
        "replay.conf",
    ):
        path = GPU_HOST / name
        if path.exists():
            assert "rtsp://" not in path.read_text().replace("rtsp://mediamtx:8554", ""), name


def test_compose_runs_mediamtx_on_the_internal_network_with_the_camera_from_env():
    text = (GPU_HOST / "docker-compose.yml").read_text()
    assert "bluenviron/mediamtx:1.21.3" in text
    assert "MTX_PATHS_SEAHIVE_SOURCE: ${SEAHIVE_SOURCE}" in text
    assert (
        '"8554:' not in text and '"8888:' not in text
    )  # reached only by the worker, the encoder and Caddy
    assert "./mediamtx.yml:/mediamtx.yml:ro" in text
    services = yaml.safe_load(text)["services"]
    assert services["mediamtx"]["environment"] == {
        "MTX_PATHS_SEAHIVE_SOURCE": "${SEAHIVE_SOURCE}",
        "MTX_HLSCDNSECRET": "${HLS_CDN_SECRET}",  # CDN mode: the bearer Caddy adds on /hls/*
    }
    assert services["mediamtx"]["labels"] == {
        "wahoobay.config": "${MEDIAMTX_CONFIG_SHA}"
    }  # a changed file recreates it
    assert "hlsCDNSecret" not in yaml.safe_load((GPU_HOST / "mediamtx.yml").read_text())
    caddy = services["caddy"]
    assert "env_file" not in caddy  # .env also holds the camera URL and the database URL
    assert caddy["environment"] == {
        "ORIGIN_VERIFY_SECRET": "${ORIGIN_VERIFY_SECRET}",
        "HLS_CDN_SECRET": "${HLS_CDN_SECRET}",
    }
    assert caddy["labels"] == {"wahoobay.config": "${CADDYFILE_SHA}"}
    encoder = services["encoder"]
    assert "env_file" not in encoder
    secrets = {"SEAHIVE_SOURCE", "HLS_CDN_SECRET", "ORIGIN_VERIFY_SECRET", "DATABASE_URL"}
    assert not secrets & set(encoder["environment"])
    assert encoder["environment"] == {"NVIDIA_DRIVER_CAPABILITIES": "compute,video,utility"}
    assert encoder["labels"] == {
        "wahoobay.encoder-script": "${ENCODER_SCRIPT_SHA}"
    }  # from boot.sh's .env


def test_boot_points_the_worker_at_mediamtx_and_reads_only_from_it(tmp_path):
    proc, env_file, _log = _boot(tmp_path)
    assert proc.returncode == 0, proc.stderr
    env = env_file.read_text()
    assert "VIDEO_SOURCE='rtsp://mediamtx:8554/seahive'" in env
    assert f"SEAHIVE_SOURCE='{FAKE_CAMERA_URL}'" in env  # consumed only by compose, for MediaMTX
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


def test_boot_decides_hls_readiness_on_the_status_and_skips_the_boxes_without_the_camera(tmp_path):
    """Without the CDN bearer MediaMTX answers a playlist with a 302 to a cookie check, which `curl -sf` takes
    for success: readiness is a 200 and nothing else. With no camera there is nothing to box, so the boxed
    variant is not waited for, keeping a boot during a camera outage well inside the deploy's 900 s."""
    proc, _env, log = _boot(tmp_path, FAKE_HLS="302")
    assert proc.returncode == 0, proc.stderr
    assert "hls: seahive not yet after " in proc.stdout and "hls: seahive ready" not in proc.stdout
    assert "-w %{http_code}" in log.read_text()
    proc, _env, log = _boot(tmp_path, reuse=True)  # 404: no camera, so seahive has no source
    assert proc.returncode == 0, proc.stderr
    assert "hls: seahive not yet after " in proc.stdout
    assert "hls: seahive_boxes skipped: seahive not ready" in proc.stdout
    assert "/hls/seahive_boxes/" not in log.read_text()  # not asked for at all


def _fake_bin(tmp_path: Path, ffmpeg: str) -> Path:
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "ffmpeg").write_text(ffmpeg)
    (fake / "ffmpeg").chmod(0o755)
    (fake / "sleep").write_text('#!/bin/sh\nprintf "%s\\n" "$1" >> "$SLEEP_LOG"\n')
    (fake / "sleep").chmod(0o755)
    return fake


def _encoder_env(**extra: str) -> dict[str, str]:
    # conftest points WORKER_URL at a dead port for every test; the encoder's defaults are what is under test
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("WORKER_URL", "MEDIAMTX_RTSP", "ENCODER_FPS")
    }
    return {**env, **extra}


def test_the_encoder_publishes_the_annotated_stream_through_nvenc(tmp_path):
    """One encode on the L4 for every viewer (spec §2): the worker's boxed MJPEG in, h264 out to MediaMTX."""
    script = GPU_HOST / "encoder.sh"
    subprocess.run(["sh", "-n", str(script)], check=True)
    fake = _fake_bin(tmp_path, '#!/bin/sh\nprintf "%s\\n" "$@" > "$FFMPEG_ARGS"\nexit 0\n')
    args_file = tmp_path / "args"
    proc = subprocess.run(
        ["sh", str(script)],
        env=_encoder_env(
            PATH=f"{fake}:{os.environ['PATH']}",
            FFMPEG_ARGS=str(args_file),
            SLEEP_LOG=str(tmp_path / "sleeps"),
            ENCODER_ONCE="1",
        ),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    args = args_file.read_text().split("\n")
    assert "http://worker:8081/stream.mjpeg" in args
    assert "rtsp://mediamtx:8554/seahive_boxes" in args
    assert "h264_nvenc" in args and "tcp" in args[args.index("-rtsp_transport") + 1]
    assert args[args.index("-b:v") + 1] == "3000k"
    assert "-an" in args and args[args.index("-pix_fmt") + 1] == "yuv420p"
    # Timing (bring-up 2026-10-31): the MJPEG demuxer's nominal 25 fps survives decode and encode whatever the
    # arrival rate, so the frames are stamped from the wall clock in the filter graph and output at a constant
    # rate, with a real IDR every second of stream time: one-second segments in real time.
    assert (
        args[args.index("-vf") + 1] == "settb=AVTB,setpts='(RTCTIME-RTCSTART)/(TB*1000000)'"
    )  # microseconds, so TB first
    assert "-use_wallclock_as_timestamps" not in args
    assert args[args.index("-fps_mode") + 1] == "cfr" and args[args.index("-r") + 1] == "15"
    assert args[args.index("-g") + 1] == "15" and args[args.index("-forced-idr") + 1] == "1"
    assert args[args.index("-force_key_frames") + 1] == "expr:gte(t,n_forced*1)"
    assert "-bf" in args and args[args.index("-bf") + 1] == "0"
    # The output rate is a knob; the GOP follows it so segments stay one second.
    subprocess.run(
        ["sh", str(script)],
        env=_encoder_env(
            PATH=f"{fake}:{os.environ['PATH']}",
            FFMPEG_ARGS=str(args_file),
            SLEEP_LOG=str(tmp_path / "sleeps"),
            ENCODER_ONCE="1",
            ENCODER_FPS="12",
        ),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    args = args_file.read_text().split("\n")
    assert args[args.index("-r") + 1] == "12" and args[args.index("-g") + 1] == "12"


def test_the_encoder_restarts_ffmpeg_when_the_input_ends(tmp_path):
    """A camera gap or a worker restart ends ffmpeg's input (exit 1); the loop waits 5 s and tries again."""
    fake = _fake_bin(tmp_path, "#!/bin/sh\nexit 1\n")
    sleep_log = tmp_path / "sleeps"
    proc = subprocess.run(
        ["sh", str(GPU_HOST / "encoder.sh")],
        env=_encoder_env(
            PATH=f"{fake}:{os.environ['PATH']}",
            SLEEP_LOG=str(sleep_log),
            ENCODER_ONCE="1",
        ),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr  # a failed run is not the script's failure
    assert sleep_log.read_text().split() == [
        "5"
    ]  # the pause before the next attempt, even after a failure


def _env_of(env_file: Path) -> dict[str, str]:
    return dict(
        (k, v.strip("'"))
        for k, v in (
            line.split("=", 1)
            for line in env_file.read_text().splitlines()
            if "=" in line and not line.startswith("#")
        )
    )


REPLAY_STAGING = "staging=replay/seahive-2026-09-01.mp4\nprod=\n"


def test_boot_plays_the_recording_replay_conf_names_in_the_cameras_place(tmp_path):
    """replay.conf names, per environment, a recording in the models bucket. boot.sh fetches it to the host,
    MediaMTX's seahive path takes a publisher instead of pulling the camera, the replay service (compose profile
    `replay`) plays it there, and the worker draws its detections without recording them."""
    proc, env_file, log = _boot(tmp_path, replay_conf=REPLAY_STAGING)
    assert proc.returncode == 0, proc.stderr
    env = _env_of(env_file)
    assert env["SEAHIVE_SOURCE"] == "publisher" and env["COMPOSE_PROFILES"] == "replay"
    assert env["REPLAY_FILE"] == "seahive-2026-09-01.mp4" and env["PERSIST_ENABLED"] == "false"
    assert (
        env["VIDEO_SOURCE"] == "rtsp://mediamtx:8554/seahive"
    )  # the worker reads the same path either way
    assert env["SOURCE_LABEL_PRIMARY"] == "seahivecam"  # the camera's no-fish zones still apply
    assert FAKE_CAMERA_URL not in env_file.read_text()  # nothing pulls the camera
    calls = log.read_text()
    assert "aws s3 cp --only-show-errors s3://models-bucket/replay/seahive-2026-09-01.mp4" in calls
    assert (tmp_path / "data" / "replay" / "seahive-2026-09-01.mp4").read_text() == "fake mp4\n"
    assert calls.index("s3 cp") < calls.index("compose up -d")
    assert "compose rm" not in calls
    assert "replay: seahive plays seahive-2026-09-01.mp4" in proc.stdout
    assert "recording: streaming" in proc.stdout and "camera: streaming" not in proc.stdout
    # A recording is never replaced under its name: the copy on the host is used from then on.
    proc, _env, log = _boot(tmp_path, reuse=True, replay_conf=REPLAY_STAGING)
    assert proc.returncode == 0 and "s3 cp" not in log.read_text()


def test_boot_back_on_the_camera_removes_the_recordings_player(tmp_path):
    """Compose leaves a running service of an inactive profile alone, even with --remove-orphans (checked
    against Compose 2.40): with no recording named, boot.sh removes the replay service itself."""
    proc, env_file, log = _boot(tmp_path)
    assert proc.returncode == 0, proc.stderr
    env = _env_of(env_file)
    assert env["SEAHIVE_SOURCE"] == FAKE_CAMERA_URL and env["COMPOSE_PROFILES"] == ""
    assert env["REPLAY_FILE"] == "" and env["PERSIST_ENABLED"] == "true"
    calls = log.read_text()
    assert "s3 cp" not in calls
    assert calls.index("compose up -d") < calls.index("docker compose rm -sf replay")


def test_boot_reads_only_its_own_environments_line(tmp_path):
    proc, env_file, log = _boot(
        tmp_path, replay_conf="staging=\nprod=replay/seahive-2026-09-01.mp4\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert _env_of(env_file)["SEAHIVE_SOURCE"] == FAKE_CAMERA_URL and "s3 cp" not in log.read_text()


def test_boot_stops_before_touching_the_stack_when_the_recording_is_missing(tmp_path):
    proc, _env, log = _boot(tmp_path, replay_conf=REPLAY_STAGING, FAKE_S3_MISSING="1")
    assert proc.returncode == 1
    assert (
        "s3://models-bucket/replay/seahive-2026-09-01.mp4" in proc.stderr
        and "replay.conf" in proc.stderr
    )
    calls = log.read_text()
    assert (
        "compose pull" not in calls and "compose up" not in calls
    )  # the running stack is left as it is
    assert not list(
        (tmp_path / "data" / "replay").iterdir()
    )  # no partial file left to be taken for the recording


def test_the_committed_replay_conf_names_recordings_in_the_models_bucket():
    lines = [
        line
        for line in (GPU_HOST / "replay.conf").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    entries = dict(line.split("=", 1) for line in lines)
    assert set(entries) == {"staging", "prod"} and len(entries) == len(lines)
    for env, key in entries.items():
        assert key == "" or (
            key.startswith("replay/") and key.endswith(".mp4") and " " not in key
        ), env
    assert entries["prod"] == ""  # production shows the camera


def test_compose_plays_a_recording_into_the_seahive_path_only_with_the_replay_profile():
    services = yaml.safe_load((GPU_HOST / "docker-compose.yml").read_text())["services"]
    replay = services["replay"]
    assert replay["profiles"] == ["replay"]
    assert (
        replay["image"] == "jrottenberg/ffmpeg:7.1.2-ubuntu2404"
    )  # no GPU: the stream is copied, not encoded
    args = replay["command"]
    assert args[args.index("-i") + 1] == "/replay/${REPLAY_FILE}"
    assert "-re" in args and args[args.index("-stream_loop") + 1] == "-1"  # its own pace, looped
    assert args[args.index("-c") + 1] == "copy" and args[-1] == "rtsp://mediamtx:8554/seahive"
    assert args[args.index("-rtsp_transport") + 1] == "tcp"
    assert replay["volumes"] == ["/opt/wahoobay-data/replay:/replay:ro"]
    assert "env_file" not in replay and "environment" not in replay  # no secret reaches it
    assert replay["logging"]["options"]["awslogs-stream"] == "replay"
    assert all("profiles" not in s for name, s in services.items() if name != "replay")


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker is not on the PATH")
def test_the_replay_service_keeps_mediamtxs_seahive_playlist_live_past_the_end_of_the_file(
    tmp_path,
):
    """The real images: a four-second recording at the camera's shape (720p, 15 fps, an IDR every second), the
    replay service's own arguments from the compose file, MediaMTX with this mediamtx.yml and a publisher source.
    The seahive playlist answers, and keeps advancing after the file has ended twice over: the loop holds."""
    replay = yaml.safe_load((GPU_HOST / "docker-compose.yml").read_text())["services"]["replay"]
    image = replay["image"]
    rec = tmp_path / "replay"
    rec.mkdir()
    subprocess.run(
        ["docker", "run", "--rm", "--platform", "linux/amd64", "-v", f"{rec}:/out", image]
        + [
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1280x720:rate=15",
        ]
        + [
            "-t",
            "4",
            "-c:v",
            "libx264",
            "-g",
            "15",
            "-bf",
            "0",
            "-pix_fmt",
            "yuv420p",
            "/out/rec.mp4",
        ],
        check=True,
        capture_output=True,
        timeout=600,
    )
    port = _free_port()
    tag = f"{os.getpid()}-{port}"
    net, mtx, player = (
        f"wb-test-replay-{tag}",
        f"wb-test-replay-mtx-{tag}",
        f"wb-test-replay-ff-{tag}",
    )
    args = [a.replace("${REPLAY_FILE}", "rec.mp4") for a in replay["command"]]

    def get(path: str) -> tuple[int, str]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request("GET", path, headers={"Authorization": "Bearer cdn"})
            r = conn.getresponse()
            return r.status, r.read().decode(errors="replace")
        finally:
            conn.close()

    def playlist() -> tuple[int, str]:  # the multivariant playlist names the media playlist
        status, body = get("/seahive/index.m3u8")
        media = [line for line in body.splitlines() if line and not line.startswith("#")]
        return get(f"/seahive/{media[-1]}") if status == 200 and media else (status, body)

    try:
        subprocess.run(
            ["docker", "network", "create", net], check=True, capture_output=True, timeout=60
        )
        subprocess.run(
            ["docker", "run", "-d", "--name", mtx, "--network", net, "--network-alias", "mediamtx"]
            + [
                "-v",
                f"{GPU_HOST / 'mediamtx.yml'}:/mediamtx.yml:ro",
                "-e",
                "MTX_PATHS_SEAHIVE_SOURCE=publisher",
            ]
            + [
                "-e",
                "MTX_HLSCDNSECRET=cdn",
                "-p",
                f"127.0.0.1:{port}:8888",
                "bluenviron/mediamtx:1.21.3",
            ],
            check=True,
            capture_output=True,
            timeout=300,
        )
        subprocess.run(
            ["docker", "run", "-d", "--name", player, "--network", net, "--platform", "linux/amd64"]
            + ["-v", f"{rec}:/replay:ro", image]
            + args,
            check=True,
            capture_output=True,
            timeout=300,
        )
        sequences = []
        deadline = time.monotonic() + 60
        while len(sequences) < 2 and time.monotonic() < deadline:
            try:
                status, body = playlist()
            except (OSError, http.client.HTTPException):
                status, body = 0, ""
            segments = [line for line in body.splitlines() if line.endswith(".mp4")]
            if status == 200 and segments:
                sequences.append(segments[-1])  # the newest segment
                if len(sequences) == 1:
                    time.sleep(10)  # 2.5 times the recording's length
                    continue
            time.sleep(0.5)
        logs = subprocess.run(["docker", "logs", player], capture_output=True, text=True).stderr
        assert len(sequences) == 2, f"no seahive playlist; replay said: {logs[-800:]}"
        assert sequences[1] != sequences[0], (
            sequences,
            logs[-800:],
        )  # new segments after the file ended
        running = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", player],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert running == "true", logs[-800:]
    finally:
        subprocess.run(["docker", "rm", "-f", player, mtx], capture_output=True, timeout=60)
        subprocess.run(["docker", "network", "rm", net], capture_output=True, timeout=60)
