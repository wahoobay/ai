"""PERSIST_ENABLED=false: the worker draws its detections on the live stream and records nothing (no database
rows, no event log, no saved frames). The GPU host turns it off while MediaMTX plays a recording in the camera's
place (deploy/gpu-host/replay.conf): a recording's fish are not today's fish. The inference loop runs here
in-process with a fake classifier and spies for the three writers."""

from __future__ import annotations

import queue
import sys
import threading
import types
from datetime import UTC, datetime
from types import SimpleNamespace

import numpy as np


def _stub_cv2(monkeypatch):
    cv2 = types.ModuleType("cv2")
    cv2.IMWRITE_JPEG_QUALITY = 1
    cv2.INTER_AREA = 3
    cv2.imencode = lambda ext, img, params=None: (True, np.frombuffer(b"JPEG", dtype=np.uint8))
    cv2.resize = lambda img, size, interpolation=None: np.zeros((size[1], size[0], 3), np.uint8)
    monkeypatch.setitem(sys.modules, "cv2", cv2)


def _run(monkeypatch, persist_enabled: bool) -> dict:
    _stub_cv2(monkeypatch)
    from services.worker.app import inference_proc, overlay, persistence, provenance
    from services.worker.app.pipeline_types import FishDetection, Prediction

    calls: dict = {"pg": [], "event_log": [], "maybe_save": []}

    class FakePg:
        def __init__(self, *a, **k):
            calls["pg"].append("connect")

        def record_detections(self, *a, **k):
            calls["pg"].append("detections")

        def record_frame_stats(self, batch):
            calls["pg"].append("frame_stats")

        def record_saved_frame(self, *a, **k):
            calls["pg"].append("saved_frame")

        def close(self):
            pass

    class FakeEventLog:
        def __init__(self, directory):
            pass

        def write(self, rec):
            calls["event_log"].append(rec)

        def close(self):
            pass

    class FakeSaver:
        def __init__(self, cfg):
            pass

        def maybe_save(self, **k):
            calls["maybe_save"].append(k["frame_id"])
            return None

        def write(self, job):
            pass

    class FakePipeline:
        def __init__(self, cfg):
            pass

        def process_frame(self, frame_bgr):
            return [FishDetection((1, 2, 30, 40), 0.9, [Prediction("Sergeant major", "sp1", 0.8)])]

    monkeypatch.setattr(persistence, "PgWriter", FakePg)
    monkeypatch.setattr(persistence, "EventLog", FakeEventLog)
    monkeypatch.setattr(persistence, "ImageSaver", FakeSaver)
    monkeypatch.setattr(provenance, "compute", lambda cfg: None)
    monkeypatch.setattr(overlay, "annotate", lambda frame, display: frame.copy())
    fake_v2 = types.ModuleType("services.worker.app.classifier_v2")
    fake_v2.ClassifierV2Pipeline = FakePipeline
    monkeypatch.setitem(sys.modules, "services.worker.app.classifier_v2", fake_v2)
    fake_tracker = types.ModuleType("services.worker.app.tracker")  # the real one imports torch
    fake_tracker.DetectionSmoother = object
    monkeypatch.setitem(sys.modules, "services.worker.app.tracker", fake_tracker)

    cfg = SimpleNamespace(
        classifier_backend="v2",
        log_level="INFO",
        device="cpu",
        events_log_dir="/nonexistent",
        database_url="postgresql://unused",
        location_id="wahoobay",
        tracker_enabled=False,
        jpeg_quality=80,
        frame_stats_every_n_frames=1,
        persist_enabled=persist_enabled,
    )
    in_q: queue.Queue = queue.Queue()
    out_q: queue.Queue = queue.Queue()
    for n in range(1, 4):
        item = inference_proc.FrameIn(n, None, (720, 1280, 3), datetime.now(UTC), "seahivecam")
        item.frame_bgr = np.zeros((720, 1280, 3), np.uint8)
        in_q.put(item)
    in_q.put(inference_proc.SHUTDOWN)
    inference_proc.run_inference_worker(cfg, in_q, out_q, threading.Event())
    calls["out"] = [out_q.get_nowait() for _ in range(out_q.qsize())]
    return calls


def test_the_worker_records_its_detections_by_default(monkeypatch):
    calls = _run(monkeypatch, persist_enabled=True)  # the control: the spies see the writes
    assert (
        calls["pg"][0] == "connect" and "detections" in calls["pg"] and "frame_stats" in calls["pg"]
    )
    assert len(calls["event_log"]) == 3 and calls["maybe_save"] == [1, 2, 3]
    assert [o.n_detections for o in calls["out"]] == [1, 1, 1]


def test_with_persistence_off_the_detections_are_drawn_and_nothing_is_recorded(monkeypatch):
    calls = _run(monkeypatch, persist_enabled=False)
    assert calls["pg"] == []  # never even connects
    assert calls["event_log"] == [] and calls["maybe_save"] == []
    out = calls["out"]
    assert [o.n_detections for o in out] == [1, 1, 1] and all(o.annotated_jpeg for o in out)
    assert out[0].detections_summary[0]["best_name"] == "Sergeant major"  # still on the live stream


def test_persistence_is_on_unless_turned_off(monkeypatch):
    from services.worker.app.config import Config

    monkeypatch.setenv("DATABASE_URL", "postgresql://u@h/d")
    monkeypatch.delenv("PERSIST_ENABLED", raising=False)
    assert Config.from_env().persist_enabled is True
    monkeypatch.setenv("PERSIST_ENABLED", "false")
    assert Config.from_env().persist_enabled is False
