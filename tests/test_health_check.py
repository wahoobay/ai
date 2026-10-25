"""scripts/dev/health_check.sh keeps the DGX stack up from a five-minute cron entry:
heartbeat, start what is missing through start_all.sh, pause on a MAINTENANCE file,
never kill anything. Fake `ss`, `pgrep`, `curl` and `flock` on PATH and a fake
start_all.sh in a scratch checkout stand in for the machine."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "dev" / "health_check.sh"

FAKES = {
    "ss": '#!/usr/bin/env bash\nfor p in ${FAKE_PORTS:-}; do echo "LISTEN 0 128 127.0.0.1:$p 0.0.0.0:*"; done\n',
    "pgrep": '#!/usr/bin/env bash\necho "pgrep $*" >> "$FAKE_LOG"\n[ "${FAKE_JOBS:-1}" = "1" ]\n',
    "curl": '#!/usr/bin/env bash\necho "curl $*" >> "$FAKE_LOG"\nif [ "${FAKE_HEALTHZ_OK:-1}" = "1" ]; then echo \'{"ok":true}\'; else exit 22; fi\n',
    "flock": '#!/usr/bin/env bash\n[ "${FAKE_LOCK_BUSY:-0}" != "1" ]\n',
}
# Records the call and whether the lock descriptor leaked into it (it must not: a
# service that inherits fd 9 holds the lock for as long as it lives).
FAKE_START_ALL = """#!/usr/bin/env bash
if { true >&9; } 2>/dev/null; then echo "start_all fd9-open" >> "$FAKE_LOG"; else echo "start_all fd9-closed" >> "$FAKE_LOG"; fi
"""
ALL_PORTS = "5432 8081 8083 18080 8082"


def _bash() -> str:
    for candidate in (
        "/opt/homebrew/bin/bash",
        "/usr/local/bin/bash",
        shutil.which("bash") or "bash",
    ):
        if Path(candidate).exists() or shutil.which(candidate):
            out = subprocess.run(
                [candidate, "-c", "echo ${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}"],
                capture_output=True,
                text=True,
            ).stdout.strip()
            major, minor = (int(x) for x in out.split("."))
            if (major, minor) >= (4, 4):
                return candidate
    pytest.skip("the health check needs bash >= 4.4 (empty arrays under set -u)")


def _run(
    tmp_path: Path, *, ports: str = ALL_PORTS, maintenance: bool = False, **extra: str
) -> tuple[subprocess.CompletedProcess, Path, Path]:
    checkout = tmp_path / "checkout"
    (checkout / "scripts" / "dev").mkdir(parents=True)
    (checkout / "logs" / "app").mkdir(parents=True)
    shutil.copy(SCRIPT, checkout / "scripts" / "dev" / "health_check.sh")
    start_all = checkout / "scripts" / "dev" / "start_all.sh"
    start_all.write_text(FAKE_START_ALL)
    start_all.chmod(0o755)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in FAKES.items():
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    if maintenance:
        (checkout / "logs" / "app" / "MAINTENANCE").touch()
    log = tmp_path / "calls.log"
    log.write_text("")
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "FAKE_LOG": str(log),
        "FAKE_PORTS": ports,
        **extra,
    }
    proc = subprocess.run(
        [_bash(), str(checkout / "scripts" / "dev" / "health_check.sh")],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    return proc, log, checkout / "logs" / "app"


def test_quiet_when_everything_is_up_but_leaves_a_heartbeat(tmp_path):
    proc, log, logs = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "" and proc.stderr == ""
    assert (logs / "health_check.last").read_text().strip()
    assert "start_all" not in log.read_text()


def test_starts_what_is_missing_without_leaking_the_lock(tmp_path):
    proc, log, _ = _run(tmp_path, ports="5432 8081 8083 8082")
    assert proc.returncode == 0, proc.stderr
    assert "down: dashboard:18080 -> start_all.sh" in proc.stdout
    assert "start_all.sh: ok" in proc.stdout
    assert "start_all fd9-closed" in log.read_text()


def test_a_missing_jobs_loop_counts_as_down(tmp_path):
    proc, log, _ = _run(tmp_path, FAKE_JOBS="0")
    assert "down: run_jobs -> start_all.sh" in proc.stdout
    assert "pgrep -x -f" in log.read_text()  # anchored: a shell mentioning the path is not the loop


def test_maintenance_pauses_restarts_but_keeps_the_heartbeat(tmp_path):
    proc, log, logs = _run(tmp_path, ports="", maintenance=True)
    assert proc.returncode == 0 and proc.stdout == ""
    assert (logs / "health_check.last").exists()
    assert "start_all" not in log.read_text()


def test_a_held_lock_is_logged_not_silent(tmp_path):
    proc, log, logs = _run(tmp_path, ports="", FAKE_LOCK_BUSY="1")
    assert proc.returncode == 0
    assert "skipped: another run holds logs/app/health_check.lock" in proc.stdout
    assert not (logs / "health_check.last").exists()
    assert "start_all" not in log.read_text()


def test_a_failing_healthz_is_reported_not_restarted(tmp_path):
    proc, log, _ = _run(tmp_path, FAKE_HEALTHZ_OK="0")
    assert proc.returncode == 0, proc.stderr
    assert "dashboard listens on :18080 but /healthz failed; not restarting it" in proc.stdout
    assert "start_all" not in log.read_text()
