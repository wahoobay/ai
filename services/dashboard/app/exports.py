"""CSV export helpers for the dashboard.

One async generator per resource. Each yields header + rows formatted with
Python's csv module so quoting/escaping is correct for arbitrary values
(e.g. species names with commas, notes with quotes).

All exports take a single ``hours`` window (and resource-specific filters)
to keep the UI simple. Server-side rendering means we never load the whole
result set into memory at once — psycopg streams rows from the cursor, we
write each one as it comes in.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from typing import AsyncIterator, Iterable, Optional

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def _csv_line(values: Iterable) -> str:
    """One properly-escaped CSV line. Uses io.StringIO to leverage stdlib quoting."""
    buf = io.StringIO()
    csv.writer(buf, lineterminator="").writerow([_format(v) for v in values])
    return buf.getvalue() + "\n"


def _format(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, (list, tuple)):
        return ",".join(map(str, v))         # bbox arrays etc. — flatten to "x1,y1,x2,y2"
    if isinstance(v, dict):
        return json.dumps(v, separators=(",", ":"), default=str)
    return str(v)


async def _stream(pool: AsyncConnectionPool, sql: str, params: tuple,
                  columns: list[str]) -> AsyncIterator[str]:
    yield _csv_line(columns)
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            while True:
                batch = await cur.fetchmany(1000)
                if not batch:
                    break
                for r in batch:
                    yield _csv_line(r[c] for c in columns)


# ---------------------------------------------------------------------------
# Per-resource generators
# ---------------------------------------------------------------------------


def export_events(pool: AsyncConnectionPool, hours: int,
                  species_id: Optional[str], min_accuracy: float):
    where = ["ts >= NOW() - (%s::int || ' hours')::interval",
             "best_accuracy >= %s"]
    params: list = [hours, min_accuracy]
    if species_id:
        where.append("best_species_id = %s")
        params.append(species_id)
    sql = f"""
        SELECT id, ts, frame_id, source_name,
               det_conf, bbox,
               best_name, best_species_id, best_accuracy,
               topk, image_path,
               model_version, detector_sha256, classifier_sha256,
               config_hash, pipeline_git_sha
          FROM detection_events
         WHERE {' AND '.join(where)}
         ORDER BY ts DESC
    """
    cols = ["id", "ts", "frame_id", "source_name",
            "det_conf", "bbox",
            "best_name", "best_species_id", "best_accuracy",
            "topk", "image_path",
            "model_version", "detector_sha256", "classifier_sha256",
            "config_hash", "pipeline_git_sha"]
    return _stream(pool, sql, tuple(params), cols)


def export_species_counts(pool: AsyncConnectionPool, hours: int):
    sql = """
        SELECT best_species_id AS species_id,
               best_name       AS name,
               count(*)        AS n,
               avg(best_accuracy)::real AS mean_accuracy,
               min(best_accuracy)::real AS min_accuracy,
               max(best_accuracy)::real AS max_accuracy,
               min(ts) AS first_seen,
               max(ts) AS last_seen
          FROM detection_events
         WHERE ts >= NOW() - (%s::int || ' hours')::interval
           AND best_species_id IS NOT NULL
         GROUP BY 1, 2
         ORDER BY n DESC
    """
    cols = ["species_id", "name", "n",
            "mean_accuracy", "min_accuracy", "max_accuracy",
            "first_seen", "last_seen"]
    return _stream(pool, sql, (hours,), cols)


def export_water_quality(pool: AsyncConnectionPool, hours: int, deployment: str):
    sql = """
        SELECT ts, deployment_uri, source,
               water_temp_c, ph, do_pct,
               chlorophyll_rfu, phycoerythrin_rfu,
               turbidity_fnu, no3_mg_l, spcond_ms_cm
          FROM sensor_readings
         WHERE deployment_uri = %s
           AND ts >= NOW() - (%s::int || ' hours')::interval
         ORDER BY ts ASC
    """
    cols = ["ts", "deployment_uri", "source",
            "water_temp_c", "ph", "do_pct",
            "chlorophyll_rfu", "phycoerythrin_rfu",
            "turbidity_fnu", "no3_mg_l", "spcond_ms_cm"]
    return _stream(pool, sql, (deployment, hours), cols)


def export_frame_stats(pool: AsyncConnectionPool, hours: int):
    sql = """
        SELECT ts, source_name, frame_id,
               mean_luma, mean_r, mean_g, mean_b, std_luma,
               num_detections, mean_det_conf,
               model_version, config_hash
          FROM frame_stats
         WHERE ts >= NOW() - (%s::int || ' hours')::interval
         ORDER BY ts ASC
    """
    cols = ["ts", "source_name", "frame_id",
            "mean_luma", "mean_r", "mean_g", "mean_b", "std_luma",
            "num_detections", "mean_det_conf",
            "model_version", "config_hash"]
    return _stream(pool, sql, (hours,), cols)


def export_saved_frames(pool: AsyncConnectionPool, hours: int):
    sql = """
        SELECT id, ts, frame_id, source_name, reason,
               num_fish, image_path, coco_path,
               model_version, config_hash, pipeline_git_sha
          FROM saved_frames
         WHERE ts >= NOW() - (%s::int || ' hours')::interval
         ORDER BY ts DESC
    """
    cols = ["id", "ts", "frame_id", "source_name", "reason",
            "num_fish", "image_path", "coco_path",
            "model_version", "config_hash", "pipeline_git_sha"]
    return _stream(pool, sql, (hours,), cols)


def export_corrections(pool: AsyncConnectionPool, hours: Optional[int],
                       reviewer: Optional[str]):
    where = []
    params: list = []
    if hours is not None:
        where.append("c.created_at >= NOW() - (%s::int || ' hours')::interval")
        params.append(hours)
    if reviewer:
        where.append("c.reviewer = %s")
        params.append(reviewer)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT c.id, c.created_at, c.event_id,
               c.corrected_name, c.corrected_species_id, c.not_a_fish,
               c.confidence, c.reviewer, c.notes,
               e.ts AS event_ts, e.source_name, e.frame_id,
               e.best_name AS original_name, e.best_species_id AS original_species_id,
               e.best_accuracy AS original_accuracy, e.bbox, e.image_path
          FROM detection_corrections c
          JOIN detection_events e ON e.id = c.event_id
          {where_sql}
         ORDER BY c.created_at DESC
    """
    cols = ["id", "created_at", "event_id",
            "corrected_name", "corrected_species_id", "not_a_fish",
            "confidence", "reviewer", "notes",
            "event_ts", "source_name", "frame_id",
            "original_name", "original_species_id",
            "original_accuracy", "bbox", "image_path"]
    return _stream(pool, sql, tuple(params), cols)


def export_alerts(pool: AsyncConnectionPool, include_resolved: bool):
    where = "" if include_resolved else "WHERE resolved_at IS NULL"
    sql = f"""
        SELECT id, name, severity, message, details,
               first_seen, last_seen, resolved_at,
               acknowledged_by, acknowledged_at
          FROM alerts
          {where}
         ORDER BY last_seen DESC
    """
    cols = ["id", "name", "severity", "message", "details",
            "first_seen", "last_seen", "resolved_at",
            "acknowledged_by", "acknowledged_at"]
    return _stream(pool, sql, tuple(), cols)


# ---------------------------------------------------------------------------
# Resource registry
# ---------------------------------------------------------------------------


KNOWN_RESOURCES = {
    "events", "species_counts", "water_quality",
    "frame_stats", "saved_frames", "corrections", "alerts",
}
