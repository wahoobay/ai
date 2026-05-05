-- Wahoo Bay fish detection schema

CREATE TABLE IF NOT EXISTS detection_events (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    frame_id        BIGINT      NOT NULL,
    source_name     TEXT        NOT NULL,
    det_conf        REAL        NOT NULL,
    bbox            INTEGER[]   NOT NULL,  -- [x1,y1,x2,y2] in source pixel space
    topk            JSONB       NOT NULL,  -- [{name, species_id, accuracy}, ...]
    best_name       TEXT,                   -- denormalized for indexing
    best_species_id TEXT,
    best_accuracy   REAL,
    image_path      TEXT                     -- null if frame wasn't saved
);

CREATE INDEX IF NOT EXISTS detection_events_ts_desc     ON detection_events (ts DESC);
CREATE INDEX IF NOT EXISTS detection_events_species_ts  ON detection_events (best_species_id, ts DESC);
CREATE INDEX IF NOT EXISTS detection_events_source_ts   ON detection_events (source_name, ts DESC);

-- Model-ops provenance columns. Added after initial schema shipped, so we use
-- IDEMPOTENT ALTERs rather than editing the CREATE above (keeps `init.sql`
-- re-runnable on existing databases).
ALTER TABLE detection_events ADD COLUMN IF NOT EXISTS model_version      TEXT;
ALTER TABLE detection_events ADD COLUMN IF NOT EXISTS detector_sha256    TEXT;
ALTER TABLE detection_events ADD COLUMN IF NOT EXISTS classifier_sha256  TEXT;
ALTER TABLE detection_events ADD COLUMN IF NOT EXISTS config_hash        TEXT;
ALTER TABLE detection_events ADD COLUMN IF NOT EXISTS pipeline_git_sha   TEXT;

ALTER TABLE saved_frames     ADD COLUMN IF NOT EXISTS model_version      TEXT;
ALTER TABLE saved_frames     ADD COLUMN IF NOT EXISTS config_hash        TEXT;
ALTER TABLE saved_frames     ADD COLUMN IF NOT EXISTS pipeline_git_sha   TEXT;

-- Track id from the smoother — lets us treat a single fish across many frames
-- as one "sighting" instead of N independent events. NULL when the smoother
-- is disabled or the event predates this column.
ALTER TABLE detection_events ADD COLUMN IF NOT EXISTS track_id BIGINT;
CREATE INDEX IF NOT EXISTS detection_events_track_ts
    ON detection_events (track_id, ts)
    WHERE track_id IS NOT NULL;

-- PTZ pose snapshots, polled from the camera at PTZ_POLL_INTERVAL_S (default
-- 1 Hz). Joined to detection_events on (source_name, nearest ts) at query
-- time to give every detection a "look direction". Off by default; the
-- poller stays idle until PTZ_POLL_URL is provided.
CREATE TABLE IF NOT EXISTS ptz_states (
    id           BIGSERIAL PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_name  TEXT NOT NULL,
    pan_deg      REAL,
    tilt_deg     REAL,
    zoom         REAL,
    raw          JSONB,            -- full response, in case the camera reports extras
    poll_method  TEXT,             -- 'vapix' | 'onvif' | other
    config_hash  TEXT
);
CREATE INDEX IF NOT EXISTS ptz_states_ts_desc      ON ptz_states (ts DESC);
CREATE INDEX IF NOT EXISTS ptz_states_src_ts_desc  ON ptz_states (source_name, ts DESC);

CREATE TABLE IF NOT EXISTS saved_frames (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    frame_id    BIGINT      NOT NULL,
    source_name TEXT        NOT NULL,
    reason      TEXT        NOT NULL,  -- 'timelapse' | 'detection' | 'interesting:<why>'
    image_path  TEXT        NOT NULL,
    coco_path   TEXT        NOT NULL,
    num_fish    INTEGER     NOT NULL
);

CREATE INDEX IF NOT EXISTS saved_frames_ts_desc ON saved_frames (ts DESC);

-- Water-quality sonde readings (sensestream.org wahoo_2 deployment).
-- Populated either from a live poller or from the synthetic data generator
-- at scripts/gen_synthetic_sensor_data.py. Units match the sonde's native output.
CREATE TABLE IF NOT EXISTS sensor_readings (
    id                BIGSERIAL PRIMARY KEY,
    ts                TIMESTAMPTZ NOT NULL,
    deployment_uri    TEXT        NOT NULL,
    water_temp_c      REAL,
    ph                REAL,
    do_pct            REAL,
    chlorophyll_rfu   REAL,
    phycoerythrin_rfu REAL,
    turbidity_fnu     REAL,
    no3_mg_l          REAL,
    spcond_ms_cm      REAL,
    source            TEXT NOT NULL DEFAULT 'live',  -- 'live' | 'synthetic'
    UNIQUE (deployment_uri, ts)
);

CREATE INDEX IF NOT EXISTS sensor_readings_ts_desc ON sensor_readings (ts DESC);
CREATE INDEX IF NOT EXISTS sensor_readings_uri_ts_desc ON sensor_readings (deployment_uri, ts DESC);

-- Frame-level stats, sampled below the full frame rate, for input-drift
-- monitoring: brightness, per-channel colour balance, detection rate.
-- Intended cadence is ~1 Hz (see FRAME_STATS_EVERY_N_FRAMES in the worker).
CREATE TABLE IF NOT EXISTS frame_stats (
    id               BIGSERIAL PRIMARY KEY,
    ts               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_name      TEXT NOT NULL,
    frame_id         BIGINT NOT NULL,
    mean_luma        REAL,
    mean_r           REAL,
    mean_g           REAL,
    mean_b           REAL,
    std_luma         REAL,
    num_detections   INTEGER NOT NULL,
    mean_det_conf    REAL,
    model_version    TEXT,
    config_hash      TEXT
);

CREATE INDEX IF NOT EXISTS frame_stats_ts_desc       ON frame_stats (ts DESC);
CREATE INDEX IF NOT EXISTS frame_stats_src_ts_desc   ON frame_stats (source_name, ts DESC);

-- Hourly rollup: quick drift-over-time view.
CREATE OR REPLACE VIEW frame_stats_hourly AS
    SELECT date_trunc('hour', ts) AS hour,
           source_name,
           avg(mean_luma)::real        AS mean_luma,
           avg(mean_r)::real           AS mean_r,
           avg(mean_g)::real           AS mean_g,
           avg(mean_b)::real           AS mean_b,
           avg(std_luma)::real         AS mean_std_luma,
           avg(num_detections)::real   AS mean_detections_per_frame,
           (sum(CASE WHEN num_detections > 0 THEN 1 ELSE 0 END)::real / count(*))
                                       AS frame_with_fish_rate,
           count(*)                    AS samples
      FROM frame_stats
     GROUP BY 1, 2;

-- Drift-against-baseline: compare the most recent hour to the same hour
-- (same-source) averaged over the prior 7 and 28 days. A large delta in
-- brightness or colour balance = biofouling or lighting change; a large
-- drop in frame_with_fish_rate = possible model degradation or just a
-- quiet day, but worth surfacing.
CREATE OR REPLACE VIEW frame_stats_drift AS
    WITH recent AS (
        SELECT source_name,
               avg(mean_luma)::real AS luma,
               avg(mean_r)::real    AS r,
               avg(mean_g)::real    AS g,
               avg(mean_b)::real    AS b,
               (sum(CASE WHEN num_detections > 0 THEN 1 ELSE 0 END)::real
                / NULLIF(count(*), 0)) AS fish_rate,
               count(*)             AS samples
          FROM frame_stats
         WHERE ts >= NOW() - INTERVAL '1 hour'
         GROUP BY 1
    ),
    ref_7d AS (
        SELECT source_name,
               avg(mean_luma)::real AS luma,
               avg(mean_r)::real    AS r,
               avg(mean_g)::real    AS g,
               avg(mean_b)::real    AS b,
               (sum(CASE WHEN num_detections > 0 THEN 1 ELSE 0 END)::real
                / NULLIF(count(*), 0)) AS fish_rate
          FROM frame_stats
         WHERE ts BETWEEN NOW() - INTERVAL '8 days' AND NOW() - INTERVAL '1 day'
         GROUP BY 1
    ),
    ref_28d AS (
        SELECT source_name,
               avg(mean_luma)::real AS luma,
               avg(mean_r)::real    AS r,
               avg(mean_g)::real    AS g,
               avg(mean_b)::real    AS b,
               (sum(CASE WHEN num_detections > 0 THEN 1 ELSE 0 END)::real
                / NULLIF(count(*), 0)) AS fish_rate
          FROM frame_stats
         WHERE ts BETWEEN NOW() - INTERVAL '29 days' AND NOW() - INTERVAL '1 day'
         GROUP BY 1
    )
    SELECT r.source_name,
           r.samples,
           r.luma  AS luma_1h,   r7.luma  AS luma_7d,   r28.luma  AS luma_28d,
           r.r     AS r_1h,      r7.r     AS r_7d,      r28.r     AS r_28d,
           r.g     AS g_1h,      r7.g     AS g_7d,      r28.g     AS g_28d,
           r.b     AS b_1h,      r7.b     AS b_7d,      r28.b     AS b_28d,
           r.fish_rate     AS fish_rate_1h,
           r7.fish_rate    AS fish_rate_7d,
           r28.fish_rate   AS fish_rate_28d
      FROM recent r
 LEFT JOIN ref_7d  r7  ON r.source_name = r7.source_name
 LEFT JOIN ref_28d r28 ON r.source_name = r28.source_name;

-- Human corrections to detector/classifier output. The corrections UI writes
-- here; the eval harness and fine-tuning pipeline read from it.
CREATE TABLE IF NOT EXISTS detection_corrections (
    id                    BIGSERIAL PRIMARY KEY,
    event_id              BIGINT NOT NULL REFERENCES detection_events(id) ON DELETE CASCADE,
    corrected_name        TEXT,                    -- NULL + not_a_fish=true  →  false positive
    corrected_species_id  TEXT,
    not_a_fish            BOOLEAN NOT NULL DEFAULT FALSE,
    confidence            TEXT NOT NULL DEFAULT 'probable',  -- 'certain' | 'probable' | 'uncertain'
    reviewer              TEXT,
    notes                 TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS detection_corrections_event    ON detection_corrections (event_id);
CREATE INDEX IF NOT EXISTS detection_corrections_reviewer ON detection_corrections (reviewer, created_at DESC);
CREATE INDEX IF NOT EXISTS detection_corrections_ts_desc  ON detection_corrections (created_at DESC);

-- SLO alerts. Rule evaluator upserts on (name); acknowledgement + resolution
-- are in-band. Keeping it flat (one table, no history) to start.
CREATE TABLE IF NOT EXISTS alerts (
    id                BIGSERIAL PRIMARY KEY,
    name              TEXT NOT NULL UNIQUE,
    severity          TEXT NOT NULL,                      -- 'info' | 'warning' | 'critical'
    message           TEXT NOT NULL,
    details           JSONB NOT NULL DEFAULT '{}'::jsonb,
    first_seen        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at       TIMESTAMPTZ,
    acknowledged_by   TEXT,
    acknowledged_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS alerts_active ON alerts (name) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS alerts_severity_ts ON alerts (severity, last_seen DESC);

-- One row per (track, species) — used to fold the fact that the classifier may
-- flip between species across frames into a single "sighting" of the dominant
-- one. The dominant species is the one whose summed accuracy across the
-- track's lifetime is highest (i.e. weighted vote).
CREATE OR REPLACE VIEW species_sightings AS
WITH track_votes AS (
    SELECT track_id,
           source_name,
           best_species_id,
           best_name,
           SUM(best_accuracy) AS score,
           COUNT(*)           AS frame_count,
           MIN(ts)            AS first_seen,
           MAX(ts)            AS last_seen,
           AVG(best_accuracy)::real AS mean_accuracy,
           MAX(best_accuracy)::real AS peak_accuracy
      FROM detection_events
     WHERE track_id IS NOT NULL
       AND best_species_id IS NOT NULL
     GROUP BY 1, 2, 3, 4
),
ranked AS (
    SELECT *,
           ROW_NUMBER() OVER (PARTITION BY track_id ORDER BY score DESC) AS rn
      FROM track_votes
)
SELECT track_id, source_name,
       best_species_id AS species_id,
       best_name       AS name,
       frame_count,
       first_seen, last_seen,
       mean_accuracy, peak_accuracy
  FROM ranked
 WHERE rn = 1;

-- Materialised mirror of species_sightings, refreshed periodically by the
-- dashboard's background refresher (~60 s cadence). Use this for any
-- user-facing dashboard endpoint or export — the live view above seq-scans
-- the full detection_events table on every call (33 M+ rows after a few
-- weeks → ~20 s queries) and is only acceptable for ad-hoc psql.
CREATE MATERIALIZED VIEW IF NOT EXISTS species_sightings_mat AS
WITH track_votes AS (
    SELECT track_id,
           source_name,
           best_species_id,
           best_name,
           SUM(best_accuracy)         AS score,
           COUNT(*)                   AS frame_count,
           MIN(ts)                    AS first_seen,
           MAX(ts)                    AS last_seen,
           AVG(best_accuracy)::real   AS mean_accuracy,
           MAX(best_accuracy)::real   AS peak_accuracy
      FROM detection_events
     WHERE track_id IS NOT NULL
       AND best_species_id IS NOT NULL
     GROUP BY 1, 2, 3, 4
),
ranked AS (
    SELECT *,
           ROW_NUMBER() OVER (PARTITION BY track_id ORDER BY score DESC) AS rn
      FROM track_votes
)
SELECT track_id, source_name,
       best_species_id AS species_id,
       best_name       AS name,
       frame_count,
       first_seen, last_seen,
       mean_accuracy, peak_accuracy
  FROM ranked
 WHERE rn = 1
 WITH DATA;

-- Unique index is required for REFRESH MATERIALIZED VIEW CONCURRENTLY,
-- which lets us refresh in the background without blocking reads. Each
-- track_id appears exactly once by construction (rn = 1 above).
CREATE UNIQUE INDEX IF NOT EXISTS species_sightings_mat_track
  ON species_sightings_mat (track_id);
CREATE INDEX IF NOT EXISTS species_sightings_mat_last_seen
  ON species_sightings_mat (last_seen DESC);
CREATE INDEX IF NOT EXISTS species_sightings_mat_species
  ON species_sightings_mat (species_id);
CREATE INDEX IF NOT EXISTS species_sightings_mat_first_seen
  ON species_sightings_mat (first_seen DESC);

CREATE OR REPLACE VIEW species_counts_hourly AS
    SELECT
        date_trunc('hour', ts)   AS hour,
        best_species_id          AS species_id,
        best_name                AS name,
        count(*)                 AS n,
        avg(best_accuracy)::real AS mean_acc
    FROM detection_events
    WHERE best_species_id IS NOT NULL
    GROUP BY 1, 2, 3;
