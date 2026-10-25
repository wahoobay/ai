"""scripts/fetch_models.py brings a worker's weights in from MODELS_URL. On the GPU host
/data/models is a persistent volume, so a download interrupted by a deploy must not
leave a half-filled model directory that every later start treats as complete."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))


def _publish(src: Path, model: str, version: str, files: dict[str, bytes]) -> None:
    d = src / model / version
    d.mkdir(parents=True)
    lines = []
    for rel, data in files.items():
        (d / rel).write_bytes(data)
        lines.append(f"{hashlib.sha256(data).hexdigest()}  {rel}")
    (d / "SHA256SUMS").write_text("\n".join(lines) + "\n")


def test_a_fetch_lands_whole_or_not_at_all(tmp_path):
    from fetch_models import fetch_model

    src, dest = tmp_path / "src", tmp_path / "models"
    _publish(src, "detector_v26", "1", {"model.pt": b"weights", "classes.json": b"[]"})
    # A previous start died mid-download: the staging directory is still there.
    leftover = dest / "detector_v26.partial"
    leftover.mkdir(parents=True)
    (leftover / "model.pt").write_bytes(b"half")
    written = fetch_model(f"file://{src}", dest, "detector_v26", "1")
    assert written == 2
    assert (dest / "detector_v26" / "model.pt").read_bytes() == b"weights"
    assert not leftover.exists(), "the staging directory is renamed into place, never left behind"


def test_a_complete_model_is_left_alone(tmp_path):
    from fetch_models import fetch_model

    src, dest = tmp_path / "src", tmp_path / "models"
    _publish(src, "detector_v26", "1", {"model.pt": b"weights"})
    assert fetch_model(f"file://{src}", dest, "detector_v26", "1") == 1
    (src / "detector_v26" / "1" / "model.pt").write_bytes(b"changed upstream")
    assert fetch_model(f"file://{src}", dest, "detector_v26", "1") == 0
    assert (dest / "detector_v26" / "model.pt").read_bytes() == b"weights"
