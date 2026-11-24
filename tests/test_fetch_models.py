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


def test_a_fetch_records_its_version(tmp_path):
    from fetch_models import fetch_model

    src, dest = tmp_path / "src", tmp_path / "models"
    _publish(src, "classifier_v2", "1", {"model.pt": b"one"})
    fetch_model(f"file://{src}", dest, "classifier_v2", "1")
    assert (dest / "classifier_v2" / ".version").read_text() == "1"


def test_a_model_of_another_version_is_replaced(tmp_path):
    """A new version pin must reach the GPU host, whose /data/models outlives every deploy
    (2026-11-22: the renamed class list, dgx-2026-10-24-r1, would otherwise never load)."""
    from fetch_models import fetch_model

    src, dest = tmp_path / "src", tmp_path / "models"
    _publish(src, "classifier_v2", "1", {"model.pt": b"one"})
    _publish(src, "classifier_v2", "2", {"model.pt": b"two", "classes.json": b"[]"})
    fetch_model(f"file://{src}", dest, "classifier_v2", "1")
    assert fetch_model(f"file://{src}", dest, "classifier_v2", "2") == 2
    assert (dest / "classifier_v2" / "model.pt").read_bytes() == b"two"
    assert (dest / "classifier_v2" / ".version").read_text() == "2"


def test_a_model_directory_without_a_version_file_is_left_alone(tmp_path):
    """A bind mount (the DGX, a laptop) or a fetch from before .version existed: never replaced."""
    from fetch_models import fetch_model

    src, dest = tmp_path / "src", tmp_path / "models"
    _publish(src, "classifier_v2", "2", {"model.pt": b"two"})
    (dest / "classifier_v2").mkdir(parents=True)
    (dest / "classifier_v2" / "model.pt").write_bytes(b"mounted")
    assert fetch_model(f"file://{src}", dest, "classifier_v2", "2") == 0
    assert (dest / "classifier_v2" / "model.pt").read_bytes() == b"mounted"


def test_a_failed_fetch_of_a_new_version_keeps_the_old_one(tmp_path):
    import pytest
    from fetch_models import fetch_model

    src, dest = tmp_path / "src", tmp_path / "models"
    _publish(src, "classifier_v2", "1", {"model.pt": b"one"})
    _publish(src, "classifier_v2", "2", {"model.pt": b"two"})
    (src / "classifier_v2" / "2" / "model.pt").write_bytes(b"tampered")
    fetch_model(f"file://{src}", dest, "classifier_v2", "1")
    with pytest.raises(ValueError):
        fetch_model(f"file://{src}", dest, "classifier_v2", "2")
    assert (dest / "classifier_v2" / "model.pt").read_bytes() == b"one"
    assert (dest / "classifier_v2" / ".version").read_text() == "1"


def test_the_classifier_default_is_the_first_fine_tuned_bundle():
    """2026-11-23: the live weights fine-tuned on the AWS training machine with the reviewers'
    verified labels (job ai-wahoobay-staging-clf-20261123T225038Z); revert = dgx-2026-10-24-r1."""
    from fetch_models import versions

    assert versions("")["classifier_v2"] == "aws-2026-11-23"


def test_a_swap_leaves_no_old_directory_behind(tmp_path):
    """The old version is renamed aside before the new one moves in, so the model directory is
    always one whole version, even if the process dies mid-swap; the aside copy is then removed."""
    from fetch_models import fetch_model

    src, dest = tmp_path / "src", tmp_path / "models"
    _publish(src, "classifier_v2", "1", {"model.pt": b"one"})
    _publish(src, "classifier_v2", "2", {"model.pt": b"two"})
    fetch_model(f"file://{src}", dest, "classifier_v2", "1")
    fetch_model(f"file://{src}", dest, "classifier_v2", "2")
    assert sorted(p.name for p in dest.iterdir()) == ["classifier_v2"]


def test_a_leftover_from_an_interrupted_swap_is_cleared(tmp_path):
    from fetch_models import fetch_model

    src, dest = tmp_path / "src", tmp_path / "models"
    _publish(src, "classifier_v2", "1", {"model.pt": b"one"})
    leftover = dest / "classifier_v2.old"
    leftover.mkdir(parents=True)
    (leftover / "model.pt").write_bytes(b"stale")
    fetch_model(f"file://{src}", dest, "classifier_v2", "1")
    assert not leftover.exists()
    assert (dest / "classifier_v2" / "model.pt").read_bytes() == b"one"


def test_a_failed_new_version_falls_back_to_the_one_on_disk(tmp_path, monkeypatch, capsys):
    """The worker's entrypoint stops on a failed fetch; with a complete earlier version on disk the
    camera keeps running on it instead, and says so."""
    from fetch_models import fetch_model, main

    src, dest = tmp_path / "src", tmp_path / "models"
    _publish(src, "classifier_v2", "1", {"model.pt": b"one"})
    _publish(src, "classifier_v2", "2", {"model.pt": b"two"})
    (src / "classifier_v2" / "2" / "model.pt").write_bytes(b"tampered")
    fetch_model(f"file://{src}", dest, "classifier_v2", "1")
    monkeypatch.setenv("MODEL_VERSIONS", "classifier_v2=2")
    args = ["--models-url", f"file://{src}", "--dest", str(dest), "--only", "classifier_v2"]
    assert main(args) == 0
    assert "keeping 1" in capsys.readouterr().err
    assert (dest / "classifier_v2" / "model.pt").read_bytes() == b"one"


def test_a_failed_fetch_with_nothing_on_disk_still_fails(tmp_path, monkeypatch):
    from fetch_models import main

    src, dest = tmp_path / "src", tmp_path / "models"
    _publish(src, "classifier_v2", "2", {"model.pt": b"two"})
    (src / "classifier_v2" / "2" / "model.pt").write_bytes(b"tampered")
    monkeypatch.setenv("MODEL_VERSIONS", "classifier_v2=2")
    args = ["--models-url", f"file://{src}", "--dest", str(dest), "--only", "classifier_v2"]
    assert main(args) == 1
