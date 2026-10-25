"""Compose profiles (Plan 2a, Task 7): `web` is the laptop stack with no GPU and no
AWS; the worker only comes with `gpu` (the CUDA image) or `cpu` (the CPU build)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed")


def _services(*profiles: str, **extra_env: str) -> dict:
    cmd = ["docker", "compose"]
    for profile in profiles:
        cmd += ["--profile", profile]
    cmd += ["config", "--format", "json"]
    env = {**os.environ, "COMPOSE_PROJECT_NAME": "wahoobay-test", **extra_env}
    env.pop("COMPOSE_FILE", None)
    env.pop("WORKER_URL", None) if "WORKER_URL" not in extra_env else None
    r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout).get("services") or {}


def test_web_profile_has_no_gpu_services():
    web = _services("web")
    assert set(web) == {"postgres", "migrate", "dashboard", "sensestream-poller", "jobs"}
    assert all("deploy" not in service for service in web.values())
    assert "worker" not in _services()
    # No worker in this profile: the dashboard must not dial one (the embed shows instead),
    # and the periodic work has a runner.
    assert web["dashboard"]["environment"]["WORKER_URL"] == ""
    assert "app.jobs all" in " ".join(web["jobs"]["command"])


def test_the_worker_comes_with_gpu_or_cpu_and_answers_as_worker():
    gpu = _services("web", "gpu")
    devices = gpu["worker"]["deploy"]["resources"]["reservations"]["devices"]
    assert devices[0]["driver"] == "nvidia" and "worker-cpu" not in gpu
    cpu = _services("web", "cpu")
    worker = cpu["worker-cpu"]
    assert worker["build"]["target"] == "cpu"
    assert worker["environment"]["DEVICE"] == "cpu"
    assert worker["environment"]["VIDEO_SOURCE"] == "file:///data/test_videos"
    assert "deploy" not in worker and "worker" not in cpu
    # A laptop has no bucket access: the CPU worker runs the public Fishial classifier.
    assert worker["environment"]["CLASSIFIER_BACKEND"] == "fishial"
    # The dashboard dials http://worker:8081 in both worker profiles (make up-gpu / up-cpu set it).
    assert "worker" in worker["networks"]["default"]["aliases"]
    with_worker = _services("web", "cpu", WORKER_URL="http://worker:8081")
    assert with_worker["dashboard"]["environment"]["WORKER_URL"] == "http://worker:8081"
