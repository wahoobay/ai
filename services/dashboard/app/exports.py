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


async def _fetch_all(pool: AsyncConnectionPool, sql: str, params: tuple,
                     columns: list[str]) -> list[dict]:
    """Used by the Parquet path — needs all rows materialised before write."""
    rows: list[dict] = []
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            while True:
                batch = await cur.fetchmany(2000)
                if not batch:
                    break
                rows.extend(batch)
    return rows


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
# Marine-biology friendly: one row per persistent fish track (sighting).
# ---------------------------------------------------------------------------


def export_sightings(pool: AsyncConnectionPool, hours: int, min_frames: int):
    sql = """
        SELECT s.track_id,
               s.source_name,
               s.species_id,
               s.name,
               s.frame_count,
               EXTRACT(EPOCH FROM (s.last_seen - s.first_seen))::real AS duration_s,
               s.first_seen,
               s.last_seen,
               s.mean_accuracy,
               s.peak_accuracy,
               (
                 SELECT jsonb_agg(jsonb_build_object(
                            'name', name, 'species_id', species_id,
                            'frames', frames, 'mean_accuracy', mean_accuracy
                        ))
                   FROM (
                       SELECT best_name AS name,
                              best_species_id AS species_id,
                              count(*) AS frames,
                              avg(best_accuracy)::real AS mean_accuracy
                         FROM detection_events
                        WHERE track_id = s.track_id
                          AND best_species_id IS NOT NULL
                        GROUP BY 1, 2
                        ORDER BY count(*) DESC
                        LIMIT 3
                   ) t
               ) AS top3_species
          FROM species_sightings s
         WHERE s.last_seen >= NOW() - (%s::int || ' hours')::interval
           AND s.frame_count >= %s
         ORDER BY s.first_seen DESC
    """
    cols = ["track_id", "source_name", "species_id", "name",
            "frame_count", "duration_s",
            "first_seen", "last_seen",
            "mean_accuracy", "peak_accuracy",
            "top3_species"]
    return _stream(pool, sql, (hours, min_frames), cols)


# ---------------------------------------------------------------------------
# Time-aligned hourly rollup of detections × water quality. One row per
# (hour, source, species). Water-quality columns are the deployment's hourly
# means so a single CSV gives biologists everything for "did fish counts
# change with turbidity / DO / temperature?" with no joins required.
# ---------------------------------------------------------------------------


def export_hourly_summary(pool: AsyncConnectionPool, hours: int, deployment: str):
    sql = """
        WITH detection_buckets AS (
            SELECT date_trunc('hour', ts) AS hour,
                   source_name,
                   best_species_id  AS species_id,
                   best_name        AS name,
                   count(*)::int    AS event_count,
                   count(DISTINCT track_id) FILTER (WHERE track_id IS NOT NULL)::int AS sighting_count,
                   avg(best_accuracy)::real AS mean_accuracy
              FROM detection_events
             WHERE ts >= NOW() - (%s::int || ' hours')::interval
               AND best_species_id IS NOT NULL
             GROUP BY 1, 2, 3, 4
        ),
        wq_buckets AS (
            SELECT date_trunc('hour', ts) AS hour,
                   avg(water_temp_c)::real      AS water_temp_c,
                   avg(ph)::real                AS ph,
                   avg(do_pct)::real            AS do_pct,
                   avg(chlorophyll_rfu)::real   AS chlorophyll_rfu,
                   avg(phycoerythrin_rfu)::real AS phycoerythrin_rfu,
                   avg(turbidity_fnu)::real     AS turbidity_fnu,
                   avg(no3_mg_l)::real          AS no3_mg_l,
                   avg(spcond_ms_cm)::real      AS spcond_ms_cm
              FROM sensor_readings
             WHERE deployment_uri = %s
               AND ts >= NOW() - (%s::int || ' hours')::interval
             GROUP BY 1
        )
        SELECT db.hour,
               db.source_name, db.species_id, db.name,
               db.event_count, db.sighting_count, db.mean_accuracy,
               wq.water_temp_c, wq.ph, wq.do_pct,
               wq.chlorophyll_rfu, wq.phycoerythrin_rfu, wq.turbidity_fnu,
               wq.no3_mg_l, wq.spcond_ms_cm
          FROM detection_buckets db
          LEFT JOIN wq_buckets wq USING (hour)
         ORDER BY db.hour ASC, db.event_count DESC
    """
    cols = ["hour", "source_name", "species_id", "name",
            "event_count", "sighting_count", "mean_accuracy",
            "water_temp_c", "ph", "do_pct",
            "chlorophyll_rfu", "phycoerythrin_rfu", "turbidity_fnu",
            "no3_mg_l", "spcond_ms_cm"]
    return _stream(pool, sql, (hours, deployment, hours), cols)


# ---------------------------------------------------------------------------
# Per-detection rows sorted track-then-time, suitable for trajectory plotting.
# ---------------------------------------------------------------------------


def export_tracks_timeline(pool: AsyncConnectionPool, hours: int, min_frames: int):
    sql = """
        WITH eligible_tracks AS (
            SELECT track_id
              FROM detection_events
             WHERE ts >= NOW() - (%s::int || ' hours')::interval
               AND track_id IS NOT NULL
             GROUP BY 1
            HAVING count(*) >= %s
        )
        SELECT e.track_id,
               row_number() OVER (PARTITION BY e.track_id ORDER BY e.ts ASC) AS step,
               e.id AS event_id,
               e.ts,
               e.frame_id,
               e.source_name,
               e.bbox,
               ((e.bbox[1] + e.bbox[3])::real) / 2.0 AS bbox_cx,
               ((e.bbox[2] + e.bbox[4])::real) / 2.0 AS bbox_cy,
               (e.bbox[3] - e.bbox[1])::real          AS bbox_w,
               (e.bbox[4] - e.bbox[2])::real          AS bbox_h,
               e.det_conf,
               e.best_name, e.best_species_id, e.best_accuracy,
               e.image_path
          FROM detection_events e
          JOIN eligible_tracks t USING (track_id)
         WHERE e.ts >= NOW() - (%s::int || ' hours')::interval
         ORDER BY e.track_id, e.ts ASC
    """
    cols = ["track_id", "step", "event_id", "ts", "frame_id", "source_name",
            "bbox", "bbox_cx", "bbox_cy", "bbox_w", "bbox_h",
            "det_conf", "best_name", "best_species_id", "best_accuracy",
            "image_path"]
    return _stream(pool, sql, (hours, min_frames, hours), cols)


# ---------------------------------------------------------------------------
# Flat top-K — every (event, candidate, rank). Enables direct construction of
# confusion matrices, calibration curves, etc.
# ---------------------------------------------------------------------------


def export_topk_long(pool: AsyncConnectionPool, hours: int, min_accuracy: float):
    sql = """
        SELECT e.id AS event_id,
               e.ts,
               e.frame_id,
               e.source_name,
               e.track_id,
               e.det_conf,
               (kv.idx)::int                 AS rank,
               kv.value->>'name'             AS name,
               kv.value->>'species_id'       AS species_id,
               (kv.value->>'accuracy')::real AS accuracy
          FROM detection_events e
          CROSS JOIN LATERAL jsonb_array_elements(e.topk) WITH ORDINALITY AS kv(value, idx)
         WHERE e.ts >= NOW() - (%s::int || ' hours')::interval
           AND (kv.value->>'accuracy')::real >= %s
         ORDER BY e.ts DESC, e.id DESC, rank ASC
    """
    cols = ["event_id", "ts", "frame_id", "source_name", "track_id",
            "det_conf", "rank", "name", "species_id", "accuracy"]
    return _stream(pool, sql, (hours, min_accuracy), cols)


# ---------------------------------------------------------------------------
# Corrections joined to original event context + crop path (if saved).
# Ready-to-train labelled data.
# ---------------------------------------------------------------------------


def export_labeled_corrections(pool: AsyncConnectionPool):
    sql = """
        SELECT c.id AS correction_id,
               c.created_at,
               c.event_id,
               c.corrected_name, c.corrected_species_id,
               c.not_a_fish, c.confidence, c.reviewer, c.notes,
               e.ts AS event_ts,
               e.source_name, e.frame_id, e.track_id,
               e.bbox, e.det_conf,
               e.best_name AS original_name,
               e.best_species_id AS original_species_id,
               e.best_accuracy   AS original_accuracy,
               e.topk            AS original_topk,
               COALESCE(sf.image_path, e.image_path) AS frame_image_path,
               sf.coco_path      AS frame_coco_path,
               e.model_version, e.detector_sha256, e.classifier_sha256,
               e.config_hash, e.pipeline_git_sha
          FROM detection_corrections c
          JOIN detection_events e ON e.id = c.event_id
          LEFT JOIN LATERAL (
              SELECT image_path, coco_path FROM saved_frames
               WHERE source_name = e.source_name AND frame_id = e.frame_id
               LIMIT 1
          ) sf ON true
         ORDER BY c.created_at DESC
    """
    cols = ["correction_id", "created_at", "event_id",
            "corrected_name", "corrected_species_id",
            "not_a_fish", "confidence", "reviewer", "notes",
            "event_ts", "source_name", "frame_id", "track_id",
            "bbox", "det_conf",
            "original_name", "original_species_id", "original_accuracy",
            "original_topk",
            "frame_image_path", "frame_coco_path",
            "model_version", "detector_sha256", "classifier_sha256",
            "config_hash", "pipeline_git_sha"]
    return _stream(pool, sql, tuple(), cols)


# ---------------------------------------------------------------------------
# Resource registry
# ---------------------------------------------------------------------------


KNOWN_RESOURCES = {
    "events", "species_counts", "water_quality",
    "frame_stats", "saved_frames", "corrections", "alerts",
    "sightings", "hourly_summary", "tracks_timeline",
    "topk_long", "labeled_corrections",
}


# ---------------------------------------------------------------------------
# Parquet path — materialises rows into a pandas DataFrame and writes a
# Parquet buffer. Heavier than streaming CSV, but produces a 5-15× smaller
# file that pandas/polars users will prefer.
# ---------------------------------------------------------------------------


async def fetch_parquet_bytes(pool: AsyncConnectionPool, sql: str,
                              params: tuple, columns: list[str]) -> bytes:
    import io
    import pandas as pd
    rows = await _fetch_all(pool, sql, params, columns)
    df = pd.DataFrame(rows, columns=columns) if rows else pd.DataFrame(columns=columns)
    # JSONB / list columns need stringification for parquet portability
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].map(_format)
    buf = io.BytesIO()
    df.to_parquet(buf, index=False, compression="snappy")
    return buf.getvalue()


# Resource → (sql, params_factory, columns) registry, used by the unified
# /api/export/<resource>.<format> path. The `_request_export()` helper in
# main.py dispatches; here we keep query construction self-contained.

def query_for(resource: str, **kwargs) -> tuple[str, tuple, list[str]]:
    """Centralised query builder so CSV and Parquet share SQL logic.

    Each branch returns (sql, params_tuple, column_list).
    """
    if resource == "events":
        hours = kwargs["hours"]; species_id = kwargs.get("species_id")
        min_accuracy = kwargs.get("min_accuracy", 0.0)
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
                   topk, image_path, track_id,
                   model_version, detector_sha256, classifier_sha256,
                   config_hash, pipeline_git_sha
              FROM detection_events
             WHERE {' AND '.join(where)}
             ORDER BY ts DESC
        """
        cols = ["id", "ts", "frame_id", "source_name",
                "det_conf", "bbox",
                "best_name", "best_species_id", "best_accuracy",
                "topk", "image_path", "track_id",
                "model_version", "detector_sha256", "classifier_sha256",
                "config_hash", "pipeline_git_sha"]
        return sql, tuple(params), cols

    if resource == "tracks_timeline":
        hours = kwargs["hours"]; min_frames = kwargs.get("min_frames", 3)
        sql = """
            WITH eligible_tracks AS (
                SELECT track_id
                  FROM detection_events
                 WHERE ts >= NOW() - (%s::int || ' hours')::interval
                   AND track_id IS NOT NULL
                 GROUP BY 1
                HAVING count(*) >= %s
            )
            SELECT e.track_id,
                   row_number() OVER (PARTITION BY e.track_id ORDER BY e.ts ASC) AS step,
                   e.id AS event_id, e.ts, e.frame_id, e.source_name,
                   e.bbox,
                   ((e.bbox[1] + e.bbox[3])::real) / 2.0 AS bbox_cx,
                   ((e.bbox[2] + e.bbox[4])::real) / 2.0 AS bbox_cy,
                   (e.bbox[3] - e.bbox[1])::real AS bbox_w,
                   (e.bbox[4] - e.bbox[2])::real AS bbox_h,
                   e.det_conf, e.best_name, e.best_species_id, e.best_accuracy,
                   e.image_path
              FROM detection_events e
              JOIN eligible_tracks t USING (track_id)
             WHERE e.ts >= NOW() - (%s::int || ' hours')::interval
             ORDER BY e.track_id, e.ts ASC
        """
        cols = ["track_id", "step", "event_id", "ts", "frame_id", "source_name",
                "bbox", "bbox_cx", "bbox_cy", "bbox_w", "bbox_h",
                "det_conf", "best_name", "best_species_id", "best_accuracy",
                "image_path"]
        return sql, (hours, min_frames, hours), cols

    if resource == "topk_long":
        hours = kwargs["hours"]; min_accuracy = kwargs.get("min_accuracy", 0.0)
        sql = """
            SELECT e.id AS event_id, e.ts, e.frame_id, e.source_name, e.track_id,
                   e.det_conf,
                   (kv.idx)::int AS rank,
                   kv.value->>'name'             AS name,
                   kv.value->>'species_id'       AS species_id,
                   (kv.value->>'accuracy')::real AS accuracy
              FROM detection_events e
              CROSS JOIN LATERAL jsonb_array_elements(e.topk) WITH ORDINALITY AS kv(value, idx)
             WHERE e.ts >= NOW() - (%s::int || ' hours')::interval
               AND (kv.value->>'accuracy')::real >= %s
             ORDER BY e.ts DESC, e.id DESC, rank ASC
        """
        cols = ["event_id", "ts", "frame_id", "source_name", "track_id",
                "det_conf", "rank", "name", "species_id", "accuracy"]
        return sql, (hours, min_accuracy), cols

    if resource == "hourly_summary":
        hours = kwargs["hours"]; deployment = kwargs.get("deployment", "wahoo_2")
        sql = """
            WITH detection_buckets AS (
                SELECT date_trunc('hour', ts) AS hour,
                       source_name,
                       best_species_id  AS species_id,
                       best_name        AS name,
                       count(*)::int    AS event_count,
                       count(DISTINCT track_id) FILTER (WHERE track_id IS NOT NULL)::int AS sighting_count,
                       avg(best_accuracy)::real AS mean_accuracy
                  FROM detection_events
                 WHERE ts >= NOW() - (%s::int || ' hours')::interval
                   AND best_species_id IS NOT NULL
                 GROUP BY 1, 2, 3, 4
            ),
            wq_buckets AS (
                SELECT date_trunc('hour', ts) AS hour,
                       avg(water_temp_c)::real      AS water_temp_c,
                       avg(ph)::real                AS ph,
                       avg(do_pct)::real            AS do_pct,
                       avg(chlorophyll_rfu)::real   AS chlorophyll_rfu,
                       avg(phycoerythrin_rfu)::real AS phycoerythrin_rfu,
                       avg(turbidity_fnu)::real     AS turbidity_fnu,
                       avg(no3_mg_l)::real          AS no3_mg_l,
                       avg(spcond_ms_cm)::real      AS spcond_ms_cm
                  FROM sensor_readings
                 WHERE deployment_uri = %s
                   AND ts >= NOW() - (%s::int || ' hours')::interval
                 GROUP BY 1
            )
            SELECT db.hour, db.source_name, db.species_id, db.name,
                   db.event_count, db.sighting_count, db.mean_accuracy,
                   wq.water_temp_c, wq.ph, wq.do_pct,
                   wq.chlorophyll_rfu, wq.phycoerythrin_rfu, wq.turbidity_fnu,
                   wq.no3_mg_l, wq.spcond_ms_cm
              FROM detection_buckets db
              LEFT JOIN wq_buckets wq USING (hour)
             ORDER BY db.hour ASC, db.event_count DESC
        """
        cols = ["hour", "source_name", "species_id", "name",
                "event_count", "sighting_count", "mean_accuracy",
                "water_temp_c", "ph", "do_pct",
                "chlorophyll_rfu", "phycoerythrin_rfu", "turbidity_fnu",
                "no3_mg_l", "spcond_ms_cm"]
        return sql, (hours, deployment, hours), cols

    raise ValueError(f"resource '{resource}' has no query builder")
