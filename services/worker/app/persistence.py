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
import os
import threading
from dataclasses import asdict
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Iterable, List, Optional

import cv2
import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)


class EventLog:
    """Append-only JSONL per UTC day."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._current_day: Optional[date] = None
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
        now = datetime.now(timezone.utc)
        with self._lock:
            self._rotate(now)
            self._fh.write(json.dumps(record, separators=(",", ":")) + "\n")

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None


class PgWriter:
    def __init__(self, dsn: str, provenance: Optional[object] = None) -> None:
        self._pool = ConnectionPool(dsn, min_size=1, max_size=4, open=True)
        self._prov = provenance  # app.provenance.Provenance or None

    def _prov_tuple(self) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]:
        p = self._prov
        if p is None:
            return (None, None, None, None, None)
        return (p.model_version, p.detector_sha256, p.classifier_sha256, p.config_hash, p.pipeline_git_sha)

    def record_detections(
        self,
        ts: datetime,
        frame_id: int,
        source_name: str,
        detections: Iterable,
        track_ids: Optional[list] = None,
        image_path: Optional[str] = None,
    ) -> None:
        mv, dsha, csha, cfghash, gitsha = self._prov_tuple()
        detections = list(detections)
        if track_ids is None:
            track_ids = [None] * len(detections)
        rows = []
        for d, tid in zip(detections, track_ids):
            best = d.best
            rows.append((
                ts, frame_id, source_name,
                d.det_conf, list(d.bbox),
                Jsonb([
                    {"name": p.name, "species_id": p.species_id, "accuracy": p.accuracy}
                    for p in d.topk
                ]),
                best.name if best else None,
                best.species_id if best else None,
                best.accuracy if best else None,
                image_path,
                mv, dsha, csha, cfghash, gitsha,
                tid,
            ))
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
                (ts, frame_id, source_name, reason, image_path, coco_path, num_fish,
                 mv, cfghash, gitsha),
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


class ImageSaver:
    """Handles timelapse / per-detection / interesting-only frame dumps with COCO sidecars."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.root = Path(cfg.frames_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self._last_timelapse = 0.0
        self._last_any_save_ts = 0.0
        self._species_seen_today: set[str] = set()
        self._today: Optional[date] = None
        self._annotation_id = 0
        # COCO category ids are assigned lazily as species are seen.
        self._category_ids: dict[str, int] = {}

    def _reset_today(self, ts: datetime) -> None:
        d = ts.astimezone(timezone.utc).date()
        if d != self._today:
            self._today = d
            self._species_seen_today = set()

    def _reason(self, now_mono: float, ts: datetime, detections: list) -> Optional[str]:
        self._reset_today(ts)
        cfg = self.cfg

        if cfg.save_timelapse_seconds > 0 and (now_mono - self._last_timelapse) >= cfg.save_timelapse_seconds:
            return "timelapse"

        if not detections:
            return None

        if cfg.save_per_detection:
            return "detection"

        if cfg.save_interesting_only:
            best_confs = [d.best.accuracy for d in detections if d.best is not None]
            max_conf = max(best_confs) if best_confs else 0.0

            # (a) new species today
            new_species = []
            for d in detections:
                sid = d.best.species_id if d.best else None
                if sid and sid not in self._species_seen_today:
                    new_species.append(sid)
            if new_species:
                return "interesting:new_species"

            # (b) high-confidence detection
            if max_conf >= cfg.save_interesting_min_conf:
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
    ) -> Optional[tuple[str, str, str]]:
        """Return (reason, image_path, coco_path) if saved, else None."""
        reason = self._reason(now_mono, ts, detections)
        if reason is None:
            return None

        for d in detections:
            sid = d.best.species_id if d.best else None
            if sid:
                self._species_seen_today.add(sid)

        if reason == "timelapse":
            self._last_timelapse = now_mono
        self._last_any_save_ts = now_mono

        day_dir = self.root / ts.strftime("%Y/%m/%d/%H")
        day_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{ts.strftime('%Y%m%dT%H%M%SZ')}_f{frame_id:010d}_{source_name}"
        img_path = day_dir / f"{stem}.jpg"
        ann_path = day_dir / f"{stem}.annotated.jpg"
        coco_path = day_dir / f"{stem}.coco.json"

        cv2.imwrite(str(img_path), frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), self.cfg.jpeg_quality])
        cv2.imwrite(str(ann_path), annotated_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), self.cfg.jpeg_quality])

        h, w = frame_bgr.shape[:2]
        coco = self._build_coco(
            image_path=img_path.name,
            width=w, height=h,
            frame_id=frame_id,
            ts=ts,
            source_name=source_name,
            reason=reason,
            detections=detections,
        )
        coco_path.write_text(json.dumps(coco, indent=2))

        return reason, str(img_path), str(coco_path)

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
            "images": [{
                "id": frame_id,
                "file_name": image_path,
                "width": width,
                "height": height,
                "date_captured": ts.isoformat(),
            }],
            "annotations": annotations,
            "categories": categories,
        }

    def _ensure_category(self, best) -> int:
        name = best.name if best else "unknown"
        if name not in self._category_ids:
            self._category_ids[name] = len(self._category_ids) + 1
        return self._category_ids[name]
