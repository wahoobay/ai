"""Persistence: Postgres events + JSONL event log + COCO-format image saves.

Image saving has three independent, tunable modes — enable any combination:
  1. timelapse:        every N seconds regardless of detections
  2. per_detection:    whenever one or more fish are detected
  3. interesting_only: on (a) new species seen today, (b) confidence >= threshold,
                      (c) first detection after a quiet period
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import cv2
from common.storage import frame_key, join, put
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)


class EventLog:
    """Append-only JSONL per UTC day."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._current_day: date | None = None
        self._fh = None

    def _rotate(self, now: datetime) -> None:
        today = now.date()
        if today == self._current_day and self._fh is not None:
            return
        if self._fh is not None:
            self._fh.close()
        path = self.directory / f"events-{today.isoformat()}.jsonl"
        self._fh = path.open("a", buffering=1, encoding="utf-8")
        self._current_day = today

    def write(self, record: dict) -> None:
        now = datetime.now(UTC)
        with self._lock:
            self._rotate(now)
            self._fh.write(json.dumps(record, separators=(",", ":")) + "\n")

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None


class PgWriter:
    def __init__(self, dsn: str, provenance: object | None = None) -> None:
        self._pool = ConnectionPool(dsn, min_size=1, max_size=4, open=True)
        self._prov = provenance  # app.provenance.Provenance or None

    def _prov_tuple(self) -> tuple[str | None, str | None, str | None, str | None, str | None]:
        p = self._prov
        if p is None:
            return (None, None, None, None, None)
        return (
            p.model_version,
            p.detector_sha256,
            p.classifier_sha256,
            p.config_hash,
            p.pipeline_git_sha,
        )

    def record_detections(
        self,
        ts: datetime,
        frame_id: int,
        source_name: str,
        detections: Iterable,
        track_ids: list | None = None,
        image_path: str | None = None,
    ) -> None:
        mv, dsha, csha, cfghash, gitsha = self._prov_tuple()
        detections = list(detections)
        if track_ids is None:
            track_ids = [None] * len(detections)
        rows = []
        for d, tid in zip(detections, track_ids, strict=False):
            best = d.best
            rows.append(
                (
                    ts,
                    frame_id,
                    source_name,
                    d.det_conf,
                    list(d.bbox),
                    Jsonb(
                        [
                            {"name": p.name, "species_id": p.species_id, "accuracy": p.accuracy}
                            for p in d.topk
                        ]
                    ),
                    best.name if best else None,
                    best.species_id if best else None,
                    best.accuracy if best else None,
                    image_path,
                    mv,
                    dsha,
                    csha,
                    cfghash,
                    gitsha,
                    tid,
                )
            )
        if not rows:
            return
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO detection_events
                (ts, frame_id, source_name, det_conf, bbox, topk,
                 best_name, best_species_id, best_accuracy, image_path,
                 model_version, detector_sha256, classifier_sha256,
                 config_hash, pipeline_git_sha,
                 track_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, %s,%s,%s,%s,%s, %s)
                """,
                rows,
            )

    def record_saved_frame(
        self,
        ts: datetime,
        frame_id: int,
        source_name: str,
        reason: str,
        image_path: str,
        coco_path: str,
        num_fish: int,
    ) -> None:
        mv, _dsha, _csha, cfghash, gitsha = self._prov_tuple()
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO saved_frames
                (ts, frame_id, source_name, reason, image_path, coco_path, num_fish,
                 model_version, config_hash, pipeline_git_sha)
                VALUES (%s,%s,%s,%s,%s,%s,%s, %s,%s,%s)
                """,
                (
                    ts,
                    frame_id,
                    source_name,
                    reason,
                    image_path,
                    coco_path,
                    num_fish,
                    mv,
                    cfghash,
                    gitsha,
                ),
            )

    def record_ptz_state(
        self,
        ts: datetime,
        source_name: str,
        pan_deg: float | None,
        tilt_deg: float | None,
        zoom: float | None,
        raw: dict | None,
        poll_method: str,
    ) -> None:
        _mv, _dsha, _csha, cfghash, _gitsha = self._prov_tuple()
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ptz_states
                (ts, source_name, pan_deg, tilt_deg, zoom, raw, poll_method, config_hash)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (ts, source_name, pan_deg, tilt_deg, zoom, Jsonb(raw or {}), poll_method, cfghash),
            )

    def record_frame_stats(self, rows: list[tuple]) -> None:
        if not rows:
            return
        mv, _dsha, _csha, cfghash, _gitsha = self._prov_tuple()
        enriched = [(*r, mv, cfghash) for r in rows]
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO frame_stats
                (ts, source_name, frame_id, mean_luma, mean_r, mean_g, mean_b,
                 std_luma, num_detections, mean_det_conf,
                 model_version, config_hash)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, %s,%s)
                """,
                enriched,
            )

    def close(self) -> None:
        self._pool.close()


@dataclass
class SaveJob:
    """Everything the async writer needs to actually persist a frame
    (encode JPEGs + write COCO sidecar). Built synchronously from the
    inference loop; consumed by a background thread so disk I/O doesn't
    stall inference. Holds the BGR ndarrays by reference — they live
    until the writer is done with them, no copy taken."""

    frame_bgr: object  # np.ndarray, kept lightly typed to avoid import here
    annotated_bgr: object
    img_key: str  # under FRAMES_URL; what saved_frames.image_path records
    ann_key: str
    coco_key: str
    coco: dict
    reason: str
    frame_id: int
    ts: datetime
    source_name: str
    n_fish: int


class ImageSaver:
    """Handles timelapse / per-detection / interesting-only frame dumps with COCO sidecars."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        # Frames go to FRAMES_URL (file:///data/frames locally, s3://... on AWS)
        # under <location>/YYYY/MM/DD/HH/<name>; the database records the key.
        self.frames_url = cfg.frames_url
        self.location_id = cfg.location_id
        self._last_timelapse = 0.0
        self._last_any_save_ts = 0.0
        self._species_seen_today: set[str] = set()
        self._today: date | None = None
        self._annotation_id = 0
        # COCO category ids are assigned lazily as species are seen.
        self._category_ids: dict[str, int] = {}
        # Per-track save throttle. A fish lingering in front of the camera
        # used to trigger a save on every frame it was detected (~30/sec);
        # we now save at most once per track per save_min_track_gap_seconds.
        # Keyed by track_id; entries older than 2× gap are GC'd periodically
        # so the dict doesn't grow unbounded for the lifetime of the process.
        self._last_track_save_mono: dict[int, float] = {}
        self._track_gc_next = 0.0

    def keys(self, ts: datetime, frame_id: int, source_name: str) -> tuple[str, str, str]:
        """Keys under FRAMES_URL for a frame, its annotated copy and its COCO sidecar."""
        img = frame_key(self.location_id, ts, frame_id, source_name)
        stem = img[: -len(".jpg")]
        return img, f"{stem}.annotated.jpg", f"{stem}.coco.json"

    def _reset_today(self, ts: datetime) -> None:
        d = ts.astimezone(UTC).date()
        if d != self._today:
            self._today = d
            self._species_seen_today = set()

    def _reason(
        self,
        now_mono: float,
        ts: datetime,
        detections: list,
        track_ids: list | None = None,
    ) -> str | None:
        self._reset_today(ts)
        cfg = self.cfg

        if (
            cfg.save_timelapse_seconds > 0
            and (now_mono - self._last_timelapse) >= cfg.save_timelapse_seconds
        ):
            return "timelapse"

        if not detections:
            return None

        if cfg.save_per_detection:
            return "detection"

        if cfg.save_interesting_only:
            n = len(detections)
            tids = track_ids if track_ids is not None else [None] * n
            gap = max(0.0, float(cfg.save_min_track_gap_seconds))

            # (a) new species today — always saves, regardless of track gap.
            #     New-species frames are rare and high-value for the fine-tune
            #     dataset, so we don't throttle them.
            for d in detections:
                sid = d.best.species_id if d.best else None
                if sid and sid not in self._species_seen_today:
                    return "interesting:new_species"

            # (b) high-confidence detection, throttled per track. We save the
            #     frame if at least one detection passes the confidence
            #     threshold AND its track hasn't been saved in the past
            #     `gap` seconds. An untracked detection (tid is None) always
            #     counts as fresh — those are first sightings of new tracks.
            for d, tid in zip(detections, tids, strict=False):
                conf = d.best.accuracy if d.best is not None else 0.0
                if conf < cfg.save_interesting_min_conf:
                    continue
                if tid is None:
                    return "interesting:high_conf"
                last = self._last_track_save_mono.get(int(tid))
                if last is None or (now_mono - last) >= gap:
                    return "interesting:high_conf"

            # (c) first detection after quiet period
            if (now_mono - self._last_any_save_ts) >= cfg.save_interesting_quiet_seconds:
                return "interesting:after_quiet"

        return None

    def maybe_save(
        self,
        frame_bgr,
        annotated_bgr,
        detections: list,
        frame_id: int,
        ts: datetime,
        source_name: str,
        now_mono: float,
        track_ids: list | None = None,
    ) -> SaveJob | None:
        """Decide whether to save and, if so, return a `SaveJob` ready for
        a background writer to materialise. Synchronous work only — state
        updates (`_last_timelapse`, `_species_seen_today`, etc.), path
        construction, COCO dict building. The disk-bound part (encode +
        write JPEGs, write COCO JSON) happens in `write()`, which the
        caller hands to a worker thread so the inference loop never blocks
        on disk."""
        reason = self._reason(now_mono, ts, detections, track_ids)
        if reason is None:
            return None

        for d in detections:
            sid = d.best.species_id if d.best else None
            if sid:
                self._species_seen_today.add(sid)

        if reason == "timelapse":
            self._last_timelapse = now_mono
        self._last_any_save_ts = now_mono

        # Stamp the per-track save timestamps so the next ~gap seconds of
        # this same fish don't trigger another save.
        if track_ids is not None:
            for tid in track_ids:
                if tid is not None:
                    self._last_track_save_mono[int(tid)] = now_mono
            # Periodically GC stale tracks so the dict stays small even on
            # a long-running process. Anything older than 2× the gap can be
            # dropped — a track that hasn't shown up in that long is gone.
            if now_mono >= self._track_gc_next:
                gap = max(1.0, float(self.cfg.save_min_track_gap_seconds))
                cutoff = now_mono - 2 * gap - 10.0
                self._last_track_save_mono = {
                    tid: t for tid, t in self._last_track_save_mono.items() if t >= cutoff
                }
                self._track_gc_next = now_mono + 30.0

        img_key, ann_key, coco_key = self.keys(ts, frame_id, source_name)

        h, w = frame_bgr.shape[:2]
        coco = self._build_coco(
            image_path=img_key.rsplit("/", 1)[-1],
            width=w,
            height=h,
            frame_id=frame_id,
            ts=ts,
            source_name=source_name,
            reason=reason,
            detections=detections,
        )
        return SaveJob(
            frame_bgr=frame_bgr,
            annotated_bgr=annotated_bgr,
            img_key=img_key,
            ann_key=ann_key,
            coco_key=coco_key,
            coco=coco,
            reason=reason,
            frame_id=frame_id,
            ts=ts,
            source_name=source_name,
            n_fish=len(detections),
        )

    def write(self, job: SaveJob) -> None:
        """Materialise a SaveJob under FRAMES_URL. Called from a worker thread."""
        q = [int(cv2.IMWRITE_JPEG_QUALITY), self.cfg.jpeg_quality]
        for key, image in ((job.img_key, job.frame_bgr), (job.ann_key, job.annotated_bgr)):
            ok, buf = cv2.imencode(".jpg", image, q)
            if not ok:
                raise RuntimeError(f"JPEG encode failed for {key}")
            put(join(self.frames_url, key), buf.tobytes(), "image/jpeg")
        put(
            join(self.frames_url, job.coco_key),
            json.dumps(job.coco, indent=2).encode(),
            "application/json",
        )

    def _build_coco(
        self,
        image_path: str,
        width: int,
        height: int,
        frame_id: int,
        ts: datetime,
        source_name: str,
        reason: str,
        detections: list,
    ) -> dict:
        annotations = []
        for d in detections:
            self._annotation_id += 1
            x1, y1, x2, y2 = d.bbox
            bw, bh = max(0, x2 - x1), max(0, y2 - y1)
            best = d.best
            cat_id = self._ensure_category(best)
            ann = {
                "id": self._annotation_id,
                "image_id": frame_id,
                "category_id": cat_id,
                "bbox": [x1, y1, bw, bh],  # COCO is [x,y,w,h]
                "area": bw * bh,
                "iscrowd": 0,
                "score": d.det_conf,
                "topk": [
                    {"name": p.name, "species_id": p.species_id, "accuracy": p.accuracy}
                    for p in d.topk
                ],
            }
            annotations.append(ann)

        categories = [
            {"id": cid, "name": name, "supercategory": "fish"}
            for name, cid in self._category_ids.items()
        ]

        return {
            "info": {
                "description": "Wahoo Bay fish ID — auto-generated",
                "source_name": source_name,
                "frame_id": frame_id,
                "save_reason": reason,
                "date_captured": ts.isoformat(),
            },
            "images": [
                {
                    "id": frame_id,
                    "file_name": image_path,
                    "width": width,
                    "height": height,
                    "date_captured": ts.isoformat(),
                }
            ],
            "annotations": annotations,
            "categories": categories,
        }

    def _ensure_category(self, best) -> int:
        name = best.name if best else "unknown"
        if name not in self._category_ids:
            self._category_ids[name] = len(self._category_ids) + 1
        return self._category_ids[name]
