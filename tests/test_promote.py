"""scripts/promote.sh <env> <sha12> re-tags the six images in ECR so an environment
points at a build (Plan 2b, Task 3): sha-<12> -> <env>, -lambda and -cpu alike.
A fake `aws` on PATH records the calls; nothing reaches AWS."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "promote.sh"
FAKE_AWS = r"""#!/usr/bin/env bash
# Records every call. batch-get-image answers with a manifest; put-image obeys FAKE_PUT_MODE.
echo "$*" >> "$FAKE_AWS_LOG"
case "$2" in
  batch-get-image) printf '{"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json", "from": "%s"}\n' "$*" ;;
  put-image)
    if [ "${FAKE_PUT_MODE:-ok}" = "exists" ]; then
      echo "An error occurred (ImageAlreadyExistsException) when calling the PutImage operation: Image with digest 'sha256:abc' and tag 'staging' already exists in the repository" >&2
      exit 254
    fi
    echo '{"image": {}}' ;;
  *) echo "unexpected: aws $*" >&2; exit 1 ;;
esac
"""


def _run(
    tmp_path: Path, env_name: str, sha: str, mode: str = "ok"
) -> tuple[subprocess.CompletedProcess, list[str]]:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "aws").write_text(FAKE_AWS)
    (bindir / "aws").chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("")
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "FAKE_AWS_LOG": str(log),
        "FAKE_PUT_MODE": mode,
        "AWS_REGION": "us-east-1",
        "LOCATION_ID": "wahoobay",
    }
    r = subprocess.run(
        ["bash", str(SCRIPT), env_name, sha], env=env, capture_output=True, text=True
    )
    return r, log.read_text().splitlines()


def _tag_pairs(calls: list[str]) -> set[tuple[str, str]]:
    pairs = set()
    for call in calls:
        if not call.startswith("ecr put-image"):
            continue
        parts = call.split()
        pairs.add(
            (parts[parts.index("--repository-name") + 1], parts[parts.index("--image-tag") + 1])
        )
    return pairs


def test_promote_retags_the_six_images(tmp_path):
    r, calls = _run(tmp_path, "staging", "abcdef123456")
    assert r.returncode == 0, r.stderr
    assert _tag_pairs(calls) == {
        ("ai-wahoobay-dashboard", "staging"),
        ("ai-wahoobay-dashboard", "staging-lambda"),
        ("ai-wahoobay-poller", "staging"),
        ("ai-wahoobay-poller", "staging-lambda"),
        ("ai-wahoobay-worker", "staging"),
        ("ai-wahoobay-worker", "staging-cpu"),
    }
    gets = [c for c in calls if c.startswith("ecr batch-get-image")]
    assert len(gets) == 6
    assert any("imageTag=sha-abcdef123456-lambda" in c for c in gets)
    assert any("imageTag=sha-abcdef123456-cpu" in c for c in gets)
    assert any("imageTag=sha-abcdef123456 " in c + " " for c in gets)


def test_promote_is_idempotent_when_the_tag_already_points_there(tmp_path):
    r, calls = _run(tmp_path, "prod", "abcdef123456", mode="exists")
    assert r.returncode == 0, r.stderr
    assert len(_tag_pairs(calls)) == 6 and ("ai-wahoobay-worker", "prod-cpu") in _tag_pairs(calls)


def test_promote_refuses_a_bad_environment_or_sha(tmp_path):
    r, calls = _run(tmp_path, "qa", "abcdef123456")
    assert r.returncode != 0 and not calls
    r, calls = _run(tmp_path, "staging", "abc")
    assert r.returncode != 0 and not calls
