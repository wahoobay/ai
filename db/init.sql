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
