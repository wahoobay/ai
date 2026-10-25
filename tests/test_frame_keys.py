"""The worker writes frames under FRAMES_URL by key and records the key, not a path
(Plan 2a, Task 6). cv2 is stood in for: the encoder is not what is under test."""

from __future__ import annotations

import json
import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np

REPO = Path(__file__).resolve().parents[1]


def _stub_cv2(monkeypatch):
    cv2 = types.ModuleType("cv2")
    cv2.IMWRITE_JPEG_QUALITY = 1
    cv2.imencode = lambda ext, img, params=None: (
        True,
        np.frombuffer(b"JPEG" + bytes(img.shape[:2]), dtype=np.uint8),
    )
    monkeypatch.setitem(sys.modules, "cv2", cv2)


def test_saver_writes_by_key_under_frames_url(tmp_path, monkeypatch):
    _stub_cv2(monkeypatch)
    from services.worker.app.persistence import ImageSaver, SaveJob

    cfg = SimpleNamespace(
        frames_url=f"file://{tmp_path}/frames",
        location_id="reef_b",
        jpeg_quality=80,
        save_min_track_gap_seconds=1.0,
    )
    saver = ImageSaver(cfg)
    ts = datetime(2026, 10, 24, 15, 4, 5, tzinfo=UTC)
    img_key, ann_key, coco_key = saver.keys(ts, 42, "seahivecam")
    assert img_key == "reef_b/2026/10/24/15/20261024T150405Z_f0000000042_seahivecam.jpg"
    assert ann_key == img_key.replace(".jpg", ".annotated.jpg")
    assert coco_key == img_key.replace(".jpg", ".coco.json")
    job = SaveJob(
        frame_bgr=np.zeros((4, 6, 3), np.uint8),
        annotated_bgr=np.zeros((4, 6, 3), np.uint8),
        img_key=img_key,
        ann_key=ann_key,
        coco_key=coco_key,
        coco={"images": [{"file_name": "20261024T150405Z_f0000000042_seahivecam.jpg"}]},
        reason="timelapse",
        frame_id=42,
        ts=ts,
        source_name="seahivecam",
        n_fish=0,
    )
    saver.write(job)
    root = tmp_path / "frames"
    assert (root / img_key).read_bytes().startswith(b"JPEG")
    assert (root / ann_key).read_bytes().startswith(b"JPEG")
    assert json.loads((root / coco_key).read_text())["images"][0]["file_name"].endswith(".jpg")


def test_the_database_gets_the_key_not_a_path():
    source = (REPO / "services" / "worker" / "app" / "inference_proc.py").read_text()
    assert (
        "str(job.img_path)" not in source and "job.img_key" in source and "job.coco_key" in source
    )
