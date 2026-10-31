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
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from psycopg.postgres import types as pg_types
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from starlette.concurrency import run_in_threadpool

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
    if isinstance(v, list | tuple):
        return ",".join(map(str, v))  # bbox arrays etc. — flatten to "x1,y1,x2,y2"
    if isinstance(v, dict):
        return json.dumps(v, separators=(",", ":"), default=str)
    return str(v)


async def _stream(
    pool: AsyncConnectionPool, sql: str, params: tuple, columns: list[str]
) -> AsyncIterator[str]:
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


async def _fetch_all(
    pool: AsyncConnectionPool, sql: str, params: tuple, columns: list[str]
) -> list[dict]:
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


# Columns that say who labelled a box, what they wrote, or which admin
# acknowledged an alert. Spec §12.1: since spec 1 these are accounts'
# display names, so they are returned to administrators only.
IDENTITY_COLUMNS = ("reviewer", "notes", "user_id", "session_id", "acknowledged_by")


def _without_identity(columns: list[str], include_identity: bool) -> list[str]:
    if include_identity:
        return columns
    return [c for c in columns if c not in IDENTITY_COLUMNS]


def hide_pending_spot_checks(alias: str) -> str:
    """SQL condition for public outputs (follow-up spec D3): leave out every
    answer on a box whose spot-check is waiting or being answered. The
    second volunteer is shown the box's event id, so a public list naming
    that event's first answer would let them look it up."""
    return (
        f"NOT EXISTS (SELECT 1 FROM review_items ri WHERE ri.event_id = {alias}.event_id"
        " AND ri.pass = 2 AND ri.status IN ('open', 'claimed'))"
    )


# ---------------------------------------------------------------------------
# Per-resource generators
# ---------------------------------------------------------------------------


def _events_query(
    hours: int, species_id: str | None, min_accuracy: float
) -> tuple[str, tuple, list[str]]:
    where = ["ts >= NOW() - (%s::int || ' hours')::interval", "best_accuracy >= %s"]
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
    cols = [
        "id",
        "ts",
        "frame_id",
        "source_name",
        "det_conf",
        "bbox",
        "best_name",
        "best_species_id",
        "best_accuracy",
        "topk",
        "image_path",
        "model_version",
        "detector_sha256",
        "classifier_sha256",
        "config_hash",
        "pipeline_git_sha",
    ]
    return sql, tuple(params), cols


def export_events(
    pool: AsyncConnectionPool, hours: int, species_id: str | None, min_accuracy: float
):
    return _stream(pool, *_events_query(hours, species_id, min_accuracy))


def _species_counts_query(hours: int) -> tuple[str, tuple, list[str]]:
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
    cols = [
        "species_id",
        "name",
        "n",
        "mean_accuracy",
        "min_accuracy",
        "max_accuracy",
        "first_seen",
        "last_seen",
    ]
    return sql, (hours,), cols


def export_species_counts(pool: AsyncConnectionPool, hours: int):
    return _stream(pool, *_species_counts_query(hours))


def _water_quality_query(hours: int, deployment: str) -> tuple[str, tuple, list[str]]:
    sql = """
        SELECT ts, deployment_uri, source,
               water_temp_c, ph, do_pct,
               chlorophyll_rfu, phycoerythrin_rfu,
               turbidity_fnu, spcond_ms_cm
          FROM sensor_readings
         WHERE deployment_uri = %s
           AND ts >= NOW() - (%s::int || ' hours')::interval
         ORDER BY ts ASC
    """
    cols = [
        "ts",
        "deployment_uri",
        "source",
        "water_temp_c",
        "ph",
        "do_pct",
        "chlorophyll_rfu",
        "phycoerythrin_rfu",
        "turbidity_fnu",
        "spcond_ms_cm",
    ]
    return sql, (deployment, hours), cols


def export_water_quality(pool: AsyncConnectionPool, hours: int, deployment: str):
    return _stream(pool, *_water_quality_query(hours, deployment))


def _weather_query(hours: int, deployment: str) -> tuple[str, tuple, list[str]]:
    """All columns of weather_readings — on-site WXT-536 station feed
    plus the merged Open-Meteo / NWS forecast layer. Both sources are
    materialised on the same row by the poller; NULLs are normal where
    one feed has a measurement the other doesn't."""
    sql = """
        SELECT ts, deployment_uri, source,
               bar_press_hpa,
               wind_dir_avg_deg, wind_dir_min_deg, wind_dir_max_deg,
               wind_speed_avg_ms, wind_speed_min_ms, wind_speed_max_ms,
               air_temp_c, rel_humidity_pct,
               rain_accum_mm, rain_dur_frac_hr,
               rain_int_mm_hr, rain_peak_int_mm_hr,
               hail_accum, hail_dur, hail_int, hail_peak_int,
               water_level_mm, solar_rad_wm2,
               cloud_cover_pct, dew_point_c,
               forecast_humidity_pct, precip_intensity_mm_hr, precip_prob_pct,
               press_sea_level_hpa, press_surface_hpa,
               temp_apparent_c, uv_index,
               weather_code, weather_description
          FROM weather_readings
         WHERE deployment_uri = %s
           AND ts >= NOW() - (%s::int || ' hours')::interval
         ORDER BY ts ASC
    """
    cols = [
        "ts",
        "deployment_uri",
        "source",
        "bar_press_hpa",
        "wind_dir_avg_deg",
        "wind_dir_min_deg",
        "wind_dir_max_deg",
        "wind_speed_avg_ms",
        "wind_speed_min_ms",
        "wind_speed_max_ms",
        "air_temp_c",
        "rel_humidity_pct",
        "rain_accum_mm",
        "rain_dur_frac_hr",
        "rain_int_mm_hr",
        "rain_peak_int_mm_hr",
        "hail_accum",
        "hail_dur",
        "hail_int",
        "hail_peak_int",
        "water_level_mm",
        "solar_rad_wm2",
        "cloud_cover_pct",
        "dew_point_c",
        "forecast_humidity_pct",
        "precip_intensity_mm_hr",
        "precip_prob_pct",
        "press_sea_level_hpa",
        "press_surface_hpa",
        "temp_apparent_c",
        "uv_index",
        "weather_code",
        "weather_description",
    ]
    return sql, (deployment, hours), cols


def export_weather(pool: AsyncConnectionPool, hours: int, deployment: str):
    return _stream(pool, *_weather_query(hours, deployment))


def _frame_stats_query(hours: int) -> tuple[str, tuple, list[str]]:
    sql = """
        SELECT ts, source_name, frame_id,
               mean_luma, mean_r, mean_g, mean_b, std_luma,
               num_detections, mean_det_conf,
               model_version, config_hash
          FROM frame_stats
         WHERE ts >= NOW() - (%s::int || ' hours')::interval
         ORDER BY ts ASC
    """
    cols = [
        "ts",
        "source_name",
        "frame_id",
        "mean_luma",
        "mean_r",
        "mean_g",
        "mean_b",
        "std_luma",
        "num_detections",
        "mean_det_conf",
        "model_version",
        "config_hash",
    ]
    return sql, (hours,), cols


def export_frame_stats(pool: AsyncConnectionPool, hours: int):
    return _stream(pool, *_frame_stats_query(hours))


def export_saved_frames(pool: AsyncConnectionPool, hours: int):
    sql = """
        SELECT id, ts, frame_id, source_name, reason,
               num_fish, image_path, coco_path,
               model_version, config_hash, pipeline_git_sha
          FROM saved_frames
         WHERE ts >= NOW() - (%s::int || ' hours')::interval
         ORDER BY ts DESC
    """
    cols = [
        "id",
        "ts",
        "frame_id",
        "source_name",
        "reason",
        "num_fish",
        "image_path",
        "coco_path",
        "model_version",
        "config_hash",
        "pipeline_git_sha",
    ]
    return _stream(pool, sql, (hours,), cols)


def export_corrections(
    pool: AsyncConnectionPool,
    hours: int | None,
    reviewer: str | None,
    include_identity: bool = False,
):
    # include_identity: an admin is asking; they also see answers under a
    # pending spot-check.
    where = []
    params: list = []
    if hours is not None:
        where.append("c.created_at >= NOW() - (%s::int || ' hours')::interval")
        params.append(hours)
    if reviewer:
        where.append("c.reviewer = %s")
        params.append(reviewer)
    if not include_identity:
        where.append(hide_pending_spot_checks("c"))
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT c.id, c.created_at, c.event_id,
               c.corrected_name, c.corrected_species_id, c.not_a_fish, c.unidentifiable,
               c.confidence, c.reviewer, c.notes, c.user_id, c.session_id,
               e.ts AS event_ts, e.source_name, e.frame_id,
               e.best_name AS original_name, e.best_species_id AS original_species_id,
               e.best_accuracy AS original_accuracy, e.bbox, e.image_path
          FROM detection_corrections c
          JOIN detection_events e ON e.id = c.event_id
          {where_sql}
         ORDER BY c.created_at DESC
    """
    cols = [
        "id",
        "created_at",
        "event_id",
        "corrected_name",
        "corrected_species_id",
        "not_a_fish",
        "unidentifiable",
        "confidence",
        "reviewer",
        "notes",
        "user_id",
        "session_id",
        "event_ts",
        "source_name",
        "frame_id",
        "original_name",
        "original_species_id",
        "original_accuracy",
        "bbox",
        "image_path",
    ]
    return _stream(pool, sql, tuple(params), _without_identity(cols, include_identity))


def export_alerts(
    pool: AsyncConnectionPool, include_resolved: bool, include_identity: bool = False
):
    where = "" if include_resolved else "WHERE resolved_at IS NULL"
    sql = f"""
        SELECT id, name, severity, message, details,
               first_seen, last_seen, resolved_at,
               acknowledged_by, acknowledged_at
          FROM alerts
          {where}
         ORDER BY last_seen DESC
    """
    cols = [
        "id",
        "name",
        "severity",
        "message",
        "details",
        "first_seen",
        "last_seen",
        "resolved_at",
        "acknowledged_by",
        "acknowledged_at",
    ]
    return _stream(pool, sql, tuple(), _without_identity(cols, include_identity))


# ---------------------------------------------------------------------------
# Marine-biology friendly: one row per persistent fish track (sighting).
# ---------------------------------------------------------------------------


def _sightings_query(hours: int, min_frames: int) -> tuple[str, tuple, list[str]]:
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
          FROM species_sightings_mat s
         WHERE s.last_seen >= NOW() - (%s::int || ' hours')::interval
           AND s.frame_count >= %s
         ORDER BY s.first_seen DESC
    """
    cols = [
        "track_id",
        "source_name",
        "species_id",
        "name",
        "frame_count",
        "duration_s",
        "first_seen",
        "last_seen",
        "mean_accuracy",
        "peak_accuracy",
        "top3_species",
    ]
    return sql, (hours, min_frames), cols


def export_sightings(pool: AsyncConnectionPool, hours: int, min_frames: int):
    return _stream(pool, *_sightings_query(hours, min_frames))


# ---------------------------------------------------------------------------
# Time-aligned hourly rollup of detections × water quality. One row per
# (hour, source, species). Water-quality columns are the deployment's hourly
# means so a single CSV gives biologists everything for "did fish counts
# change with turbidity / DO / temperature?" with no joins required.
# ---------------------------------------------------------------------------


def _hourly_summary_query(
    hours: int, deployment: str, cameras: Sequence[str] | None = None
) -> tuple[str, tuple, list[str]]:
    """With `cameras`, a source is its camera's id, any other source '' (one group of them)."""
    source = "source_name"
    if cameras is not None:
        source = "CASE WHEN source_name = ANY(%s) THEN source_name ELSE '' END AS source_name"
    sql = f"""
        WITH detection_buckets AS (
            SELECT date_trunc('hour', ts) AS hour,
                   {source},
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
               wq.spcond_ms_cm
          FROM detection_buckets db
          LEFT JOIN wq_buckets wq USING (hour)
         ORDER BY db.hour ASC, db.event_count DESC
    """
    cols = [
        "hour",
        "source_name",
        "species_id",
        "name",
        "event_count",
        "sighting_count",
        "mean_accuracy",
        "water_temp_c",
        "ph",
        "do_pct",
        "chlorophyll_rfu",
        "phycoerythrin_rfu",
        "turbidity_fnu",
        "spcond_ms_cm",
    ]
    params = (hours, deployment, hours)
    return sql, params if cameras is None else (list(cameras), *params), cols


def export_hourly_summary(pool: AsyncConnectionPool, hours: int, deployment: str):
    return _stream(pool, *_hourly_summary_query(hours, deployment))


# ---------------------------------------------------------------------------
# Per-detection rows sorted track-then-time, suitable for trajectory plotting.
# ---------------------------------------------------------------------------


def _tracks_timeline_query(hours: int, min_frames: int) -> tuple[str, tuple, list[str]]:
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
    cols = [
        "track_id",
        "step",
        "event_id",
        "ts",
        "frame_id",
        "source_name",
        "bbox",
        "bbox_cx",
        "bbox_cy",
        "bbox_w",
        "bbox_h",
        "det_conf",
        "best_name",
        "best_species_id",
        "best_accuracy",
        "image_path",
    ]
    return sql, (hours, min_frames, hours), cols


def export_tracks_timeline(pool: AsyncConnectionPool, hours: int, min_frames: int):
    return _stream(pool, *_tracks_timeline_query(hours, min_frames))


# ---------------------------------------------------------------------------
# Flat top-K — every (event, candidate, rank). Enables direct construction of
# confusion matrices, calibration curves, etc.
# ---------------------------------------------------------------------------


def _topk_long_query(hours: int, min_accuracy: float) -> tuple[str, tuple, list[str]]:
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
    cols = [
        "event_id",
        "ts",
        "frame_id",
        "source_name",
        "track_id",
        "det_conf",
        "rank",
        "name",
        "species_id",
        "accuracy",
    ]
    return sql, (hours, min_accuracy), cols


def export_topk_long(pool: AsyncConnectionPool, hours: int, min_accuracy: float):
    return _stream(pool, *_topk_long_query(hours, min_accuracy))


# ---------------------------------------------------------------------------
# Corrections joined to original event context + crop path (if saved).
# Ready-to-train labelled data.
# ---------------------------------------------------------------------------


def export_labeled_corrections(pool: AsyncConnectionPool, include_identity: bool = False):
    # The frame is matched on camera + frame_id + ts. frame_id restarts at 1
    # with every worker restart, so frame_id + camera alone can name a
    # different frame. When one frame was saved under several reasons, the
    # lowest id wins.
    where_sql = "" if include_identity else "WHERE " + hide_pending_spot_checks("c")
    sql = f"""
        SELECT c.id AS correction_id,
               c.created_at,
               c.event_id,
               c.corrected_name, c.corrected_species_id, c.not_a_fish, c.unidentifiable,
               c.is_endorsement, c.corrected_bbox, c.life_stage, c.label_source,
               c.review_item_id, c.is_adjudication,
               c.confidence, c.reviewer, c.notes, c.user_id, c.session_id,
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
               WHERE source_name = e.source_name AND frame_id = e.frame_id AND ts = e.ts
               ORDER BY id
               LIMIT 1
          ) sf ON true
          {where_sql}
         ORDER BY c.created_at DESC
    """
    cols = [
        "correction_id",
        "created_at",
        "event_id",
        "corrected_name",
        "corrected_species_id",
        "not_a_fish",
        "unidentifiable",
        "is_endorsement",
        "corrected_bbox",
        "life_stage",
        "label_source",
        "review_item_id",
        "is_adjudication",
        "confidence",
        "reviewer",
        "notes",
        "user_id",
        "session_id",
        "event_ts",
        "source_name",
        "frame_id",
        "track_id",
        "bbox",
        "det_conf",
        "original_name",
        "original_species_id",
        "original_accuracy",
        "original_topk",
        "frame_image_path",
        "frame_coco_path",
        "model_version",
        "detector_sha256",
        "classifier_sha256",
        "config_hash",
        "pipeline_git_sha",
    ]
    return _stream(pool, sql, tuple(), _without_identity(cols, include_identity))


# ---------------------------------------------------------------------------
# Resource registry
# ---------------------------------------------------------------------------


KNOWN_RESOURCES = {
    "events",
    "species_counts",
    "water_quality",
    "frame_stats",
    "saved_frames",
    "corrections",
    "alerts",
    "sightings",
    "hourly_summary",
    "tracks_timeline",
    "topk_long",
    "labeled_corrections",
}


@dataclass(frozen=True)
class PublicExport:
    description: str
    allowed_days: tuple[int, ...]
    default_days: int


# The windows a public download may cover, in days: few, so that CloudFront's cache keys and the
# work behind a download stay bounded. The four largest exports (a row per detection, per
# classifier candidate or per second of video) reach back a month; the others a year.
_WEEK_OR_MONTH = (7, 30)
_UP_TO_A_YEAR = (7, 30, 90, 365)

# The exports anyone may download from the public API (routes_public.py), in the order the site
# lists them, each with one line about it and its windows. None of them carries a reviewer, a
# note, a user or a session, and the public API names their sources by camera id alone; the
# corrections, the alerts, the saved frames' storage paths and the camera's metadata stay
# behind sign-in.
PUBLIC_EXPORTS = {
    "events": PublicExport(
        "One row per detection per frame: the box, the classifier's candidates, provenance.",
        _WEEK_OR_MONTH,
        7,
    ),
    "species_counts": PublicExport(
        "Detections per species with confidence statistics (prefer sightings).", _UP_TO_A_YEAR, 90
    ),
    "sightings": PublicExport(
        "One row per tracked fish seen in at least three frames: species and duration.",
        _UP_TO_A_YEAR,
        90,
    ),
    "hourly_summary": PublicExport(
        "Detections and sightings per species per hour, beside the water's means.",
        _UP_TO_A_YEAR,
        90,
    ),
    "water_quality": PublicExport(
        "The sonde: temperature, pH, oxygen, chlorophyll, turbidity, conductivity.",
        _UP_TO_A_YEAR,
        90,
    ),
    "weather": PublicExport(
        "The weather station's readings and the forecast layer, merged per reading.",
        _UP_TO_A_YEAR,
        90,
    ),
    "frame_stats": PublicExport(
        "About one row per second of video: brightness, colour and detection rate.",
        _WEEK_OR_MONTH,
        7,
    ),
    "tracks_timeline": PublicExport(
        "Each tracked fish's detections in order, with the box's centre and size.",
        _WEEK_OR_MONTH,
        7,
    ),
    "topk_long": PublicExport(
        "One row per detection and classifier candidate, ranked, for calibration.",
        _WEEK_OR_MONTH,
        7,
    ),
}


# ---------------------------------------------------------------------------
# Parquet path — materialises rows into a pandas DataFrame and writes a
# Parquet buffer. Heavier than streaming CSV, but produces a 5-15× smaller
# file that pandas/polars users will prefer.
# ---------------------------------------------------------------------------


async def fetch_parquet_bytes(
    pool: AsyncConnectionPool, sql: str, params: tuple, columns: list[str]
) -> bytes:
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
        hours = kwargs["hours"]
        species_id = kwargs.get("species_id")
        min_accuracy = kwargs.get("min_accuracy", 0.0)
        where = ["ts >= NOW() - (%s::int || ' hours')::interval", "best_accuracy >= %s"]
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
        cols = [
            "id",
            "ts",
            "frame_id",
            "source_name",
            "det_conf",
            "bbox",
            "best_name",
            "best_species_id",
            "best_accuracy",
            "topk",
            "image_path",
            "track_id",
            "model_version",
            "detector_sha256",
            "classifier_sha256",
            "config_hash",
            "pipeline_git_sha",
        ]
        return sql, tuple(params), cols

    if resource == "tracks_timeline":
        return _tracks_timeline_query(kwargs["hours"], kwargs.get("min_frames", 3))

    if resource == "topk_long":
        return _topk_long_query(kwargs["hours"], kwargs.get("min_accuracy", 0.0))

    if resource == "hourly_summary":
        # The route fills in the site's deployment; the public API, its cameras.
        return _hourly_summary_query(kwargs["hours"], kwargs["deployment"], kwargs.get("cameras"))

    if resource == "species_counts":
        return _species_counts_query(kwargs["hours"])

    if resource == "sightings":
        return _sightings_query(kwargs["hours"], kwargs.get("min_frames", 3))

    if resource == "water_quality":
        return _water_quality_query(kwargs["hours"], kwargs["deployment"])

    if resource == "weather":
        return _weather_query(kwargs["hours"], kwargs["deployment"])

    if resource == "frame_stats":
        return _frame_stats_query(kwargs["hours"])

    raise ValueError(f"resource '{resource}' has no query builder")


# ---------------------------------------------------------------------------
# Public downloads (routes_public.py): the same SQL, read from a server-side
# cursor a batch at a time, so that no download is ever held in memory whole.
# The dashboard's own routes above keep their client-side reads.
# ---------------------------------------------------------------------------

# Rows per FETCH from the server-side cursor: one CSV chunk, or one Parquet row group.
CSV_BATCH = 2_000
PARQUET_BATCH = 50_000

# For a column, the function each row's value passes through before it is written.
Values = Mapping[str, Callable] | None


def public_query(
    name: str, fmt: str, *, hours: int, deployment: str, cameras: Sequence[str]
) -> tuple[str, tuple, list[str]]:
    """(sql, params, columns) of a public export in `fmt` ("csv" or "parquet"): the SQL of the
    dashboard's own download in that format, with its routes' defaults for every filter but
    the window, and hourly_summary grouped by camera (any other source as one '')."""
    if fmt == "csv" and name == "events":
        return _events_query(hours, None, 0.0)  # the dashboard's events.csv has no track_id
    return query_for(
        name, hours=hours, deployment=deployment, min_frames=3, min_accuracy=0.0, cameras=cameras
    )


def _cells(row: dict, columns: list[str], values: Values) -> list:
    if values:
        row = {**row, **{c: f(row[c]) for c, f in values.items() if c in row}}
    return [row[c] for c in columns]


async def stream_csv(
    pool: AsyncConnectionPool,
    sql: str,
    params: tuple,
    columns: list[str],
    *,
    values: Values = None,
    done: Callable[[], None] = lambda: None,
) -> AsyncIterator[str]:
    """The query's rows as CSV from a server-side cursor: the header once the query runs, then
    one chunk per CSV_BATCH rows. `done` runs when the stream ends, however it ends."""
    try:
        async with pool.connection() as conn:
            async with conn.cursor(name="public_export", row_factory=dict_row) as cur:
                await cur.execute(sql, params)
                yield _csv_line(columns)
                while rows := await cur.fetchmany(CSV_BATCH):
                    yield "".join(_csv_line(_cells(r, columns, values)) for r in rows)
    finally:
        done()


def _arrow_type(oid: int):
    """The Parquet column type for a PostgreSQL type; text for an array, JSON or anything
    else, written as the CSV writes it."""
    import pyarrow as pa

    info = pg_types.get(oid)
    if info is None or info.oid != oid:  # unknown, or an array (registered under its element)
        return pa.string()
    return {
        "bool": pa.bool_(),
        "int2": pa.int64(),
        "int4": pa.int64(),
        "int8": pa.int64(),
        "float4": pa.float32(),
        "float8": pa.float64(),
        "numeric": pa.float64(),
        "date": pa.date32(),
        "timestamp": pa.timestamp("us"),
        "timestamptz": pa.timestamp("us", tz="UTC"),
    }.get(info.name, pa.string())


def _write_row_group(writer, schema, rows: list[list]) -> None:
    import pyarrow as pa

    arrays = []
    for i, field in enumerate(schema):
        cells = [row[i] for row in rows]
        if pa.types.is_string(field.type):
            cells = [None if v is None else _format(v) for v in cells]
        elif pa.types.is_floating(field.type):
            cells = [None if v is None else float(v) for v in cells]
        arrays.append(pa.array(cells, type=field.type))
    writer.write_table(pa.Table.from_arrays(arrays, schema=schema), row_group_size=len(rows))


async def write_parquet(
    pool: AsyncConnectionPool,
    sql: str,
    params: tuple,
    columns: list[str],
    path: str,
    *,
    values: Values = None,
) -> None:
    """The query's rows into a Parquet file at `path`, from a server-side cursor, PARQUET_BATCH
    rows (one row group) at a time; the encoding and the writing run off the event loop. Each
    column is typed from its PostgreSQL type, and a column `values` maps is text."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    async with pool.connection() as conn:
        async with conn.cursor(name="public_export", row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            oids = {c.name: c.type_code for c in cur.description}
            schema = pa.schema(
                [
                    (c, pa.string() if values and c in values else _arrow_type(oids[c]))
                    for c in columns
                ]
            )
            writer = await run_in_threadpool(pq.ParquetWriter, path, schema, compression="snappy")
            try:
                while rows := await cur.fetchmany(PARQUET_BATCH):
                    cells = [_cells(r, columns, values) for r in rows]
                    await run_in_threadpool(_write_row_group, writer, schema, cells)
            finally:
                await run_in_threadpool(writer.close)
