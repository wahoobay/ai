"""Seed an empty database with the sample dataset.

    python -m app.seed [--source file:///data/sample | s3://bucket/sample/] [--force]
    the `seed` job (python -m app.jobs seed; the jobs Lambda with {"job": "seed"}) from SAMPLE_URL

The sample (data/sample, made by scripts/make_sample_dataset.py) is five JSONL
tables plus frames. Every timestamp is shifted so the newest row is now, frames
are written under FRAMES_URL by key (saved_frames.image_path records it),
readings are tagged with this site's deployment, every row carries this site's
LOCATION_ID, and the materialized views are refreshed so the page shows it at
once. A database that already has detection_events is left alone unless forced.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from common.storage import frame_key, join, open_read, put
from psycopg.types.json import Jsonb

from .config import settings
from .frames import frames_url

REPO_SAMPLE = Path(__file__).resolve().parents[3] / "data" / "sample"
TABLES = ("detection_events", "saved_frames", "sensor_readings", "weather_readings", "alerts")
SENSOR_COLUMNS = (
    "water_temp_c",
    "ph",
    "do_pct",
    "chlorophyll_rfu",
    "phycoerythrin_rfu",
    "turbidity_fnu",
    "spcond_ms_cm",
)


def default_source() -> str:
    return os.environ.get("SAMPLE_URL", "").strip() or f"file://{REPO_SAMPLE}"


def _rows(source: str, table: str) -> list[dict[str, Any]]:
    text = open_read(join(source, f"{table}.jsonl")).decode()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _dt(value: str) -> datetime:
    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def seed(
    database_url: str,
    source: str,
    *,
    location_id: str,
    frames_url: str,
    deployment_uri: str = "sample",
    now: datetime | None = None,
    force: bool = False,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    data = {table: _rows(source, table) for table in TABLES}
    newest = max(
        _dt(r["ts"])
        for t in ("detection_events", "saved_frames", "sensor_readings", "weather_readings")
        for r in data[t]
    )
    shift = now - newest
    counts: dict[str, int] = {}
    with psycopg.connect(database_url) as conn:
        (existing,) = conn.execute("SELECT count(*) FROM detection_events").fetchone()
        if existing and not force:
            return {"skipped": "the database already has detection_events"}
        with conn.cursor() as cur:
            frames_payload = []
            for row in data["saved_frames"]:
                ts = _dt(row["ts"]) + shift
                key = frame_key(location_id, ts, row["frame_id"], row["source_name"])
                coco_key = key[: -len(".jpg")] + ".coco.json"
                put(
                    join(frames_url, key),
                    open_read(join(source, f"frames/{row['frame_file']}")),
                    "image/jpeg",
                )
                put(
                    join(frames_url, coco_key),
                    open_read(join(source, f"frames/{row['coco_file']}")),
                    "application/json",
                )
                frames_payload.append(
                    (
                        ts,
                        row["frame_id"],
                        row["source_name"],
                        row["reason"],
                        key,
                        coco_key,
                        row["num_fish"],
                        row.get("model_version"),
                        location_id,
                    )
                )
            cur.executemany(
                "INSERT INTO saved_frames (ts, frame_id, source_name, reason, image_path, coco_path, num_fish, model_version, location_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                frames_payload,
            )
            counts["saved_frames"] = len(frames_payload)
            cur.executemany(
                "INSERT INTO detection_events (ts, frame_id, source_name, det_conf, bbox, topk, best_name, best_species_id, "
                "best_accuracy, track_id, model_version, location_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        _dt(r["ts"]) + shift,
                        r["frame_id"],
                        r["source_name"],
                        r["det_conf"],
                        r["bbox"],
                        Jsonb(r["topk"]),
                        r.get("best_name"),
                        r.get("best_species_id"),
                        r.get("best_accuracy"),
                        r.get("track_id"),
                        r.get("model_version"),
                        location_id,
                    )
                    for r in data["detection_events"]
                ],
            )
            counts["detection_events"] = len(data["detection_events"])
            cur.executemany(
                f"INSERT INTO sensor_readings (ts, deployment_uri, {', '.join(SENSOR_COLUMNS)}, source, location_id) "
                f"VALUES (%s, %s, {', '.join(['%s'] * len(SENSOR_COLUMNS))}, 'synthetic', %s) "
                "ON CONFLICT (deployment_uri, ts) DO NOTHING",
                [
                    (
                        _dt(r["ts"]) + shift,
                        deployment_uri,
                        *(r.get(c) for c in SENSOR_COLUMNS),
                        location_id,
                    )
                    for r in data["sensor_readings"]
                ],
            )
            counts["sensor_readings"] = len(data["sensor_readings"])
            weather_cols = sorted({k for r in data["weather_readings"] for k in r} - {"ts"})
            cur.executemany(
                f"INSERT INTO weather_readings (ts, deployment_uri, {', '.join(weather_cols)}, source, location_id) "
                f"VALUES (%s, %s, {', '.join(['%s'] * len(weather_cols))}, 'synthetic', %s) "
                "ON CONFLICT (deployment_uri, ts) DO NOTHING",
                [
                    (
                        _dt(r["ts"]) + shift,
                        deployment_uri,
                        *(r.get(c) for c in weather_cols),
                        location_id,
                    )
                    for r in data["weather_readings"]
                ],
            )
            counts["weather_readings"] = len(data["weather_readings"])
            cur.executemany(
                "INSERT INTO alerts (name, severity, message, details, first_seen, last_seen, resolved_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (name) DO UPDATE SET "
                "severity = EXCLUDED.severity, message = EXCLUDED.message, details = EXCLUDED.details, "
                "first_seen = EXCLUDED.first_seen, last_seen = EXCLUDED.last_seen, resolved_at = EXCLUDED.resolved_at",
                [
                    (
                        r["name"],
                        r["severity"],
                        r["message"],
                        Jsonb(r.get("details") or {}),
                        _dt(r["first_seen"]) + shift,
                        _dt(r["last_seen"]) + shift,
                        (_dt(r["resolved_at"]) + shift) if r.get("resolved_at") else None,
                    )
                    for r in data["alerts"]
                ],
            )
            counts["alerts"] = len(data["alerts"])
            conn.commit()
            # Blocking refreshes: the seed runs on an idle database, and the
            # concurrent form cannot be the first fill of an empty view.
            cur.execute("REFRESH MATERIALIZED VIEW species_sightings_mat")
            cur.execute("REFRESH MATERIALIZED VIEW species_sightings_hourly_mat")
        conn.commit()
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--source", default=default_source(), help="file:// or s3:// directory holding the sample"
    )
    ap.add_argument("--force", action="store_true", help="seed even if detection_events has rows")
    args = ap.parse_args(argv)
    site = settings()
    out = seed(
        site.database_url,
        args.source,
        location_id=site.location_id,
        frames_url=frames_url(),
        deployment_uri=site.deployment_uri,
        force=args.force,
    )
    print(json.dumps(out, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
