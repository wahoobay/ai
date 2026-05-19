# Data pipeline

What gets stored, where, and — important — when it does NOT get
stored. Read alongside [`architecture.md`](architecture.md) (process
model) and [`operations.md`](operations.md) (runtime knobs).

---

## The three collection streams

```
┌──────────────────────────────────────────┐   ┌──────────────────────────┐   ┌───────────────────────┐
│ video → detector → classifier → smoother │   │ sonde (Wahoo Bay API)    │   │ reviewer corrections  │
│ ↓                                         │   │ → sensor_readings        │   │ → detection_corrections│
│ detection_events (raw, every frame)       │   │                          │   │  (incl. celebrity_id  │
│ frame_stats (~1 Hz drift samples)         │   │ weather (WXT-536 + OM)   │   │   + corrected_bbox)   │
│ saved_frames + JPEG + COCO sidecars      │   │ → weather_readings       │   │                       │
└──────────────────────────────────────────┘   └──────────────────────────┘   └───────────────────────┘
                          ↑                              ↑                              ↑
              gated by "is the camera live?"   independent of camera           human-triggered
```

The three streams are **architecturally separate**. The poller doesn't
depend on the worker; reviewer corrections don't depend on either.
This matters at night (cameras go dark, water-quality keeps streaming)
and during planned outages (workers can restart without affecting the
historical event store reviewers are correcting against).

---

## Per-frame writes (live camera path)

For every frame from the **primary** (live) source:

| Destination | Always written? | Notes |
|---|---|---|
| `detection_events` | One row per detected fish that survived the negative-regions filter | Includes top-K, bbox, det_conf, classifier accuracy, track_id, image_path (if a frame was saved), plus all five provenance columns. |
| `frame_stats` | Sampled at `FRAME_STATS_EVERY_N_FRAMES` (default 30 frames ≈ 1 Hz) | Brightness + per-channel RGB + std-luma + detection count + mean det conf. Used by the input-drift monitor. |
| `saved_frames` + disk | Conditional on three knobs | (1) timelapse cadence, (2) per-detection, (3) interesting-only. See "Image saves" below. |
| `logs/events/events-YYYY-MM-DD.jsonl` | One line per detection per frame | Same content as `detection_events` row, JSON-encoded. Durable backup independent of Postgres. |

For every frame, the live MJPEG stream is updated (annotated and raw
versions both encoded and published). The smoother updates its tracks
and produces the smoothed bboxes used for the live overlay.

### Where the negative-regions filter sits

Detections suppressed by `NegativeRegionFilter` (matched a reef-hole
polygon at IoU >= `min_iou`) are dropped **before any persistence**.
They never appear in `detection_events`, `frame_stats` (the
`num_detections` field reflects post-filter counts), the JSONL log, or
the live overlay's smoothed track set. This is intentional — keeps the
reviewer queue clean of known-fake bboxes.

---

## What does NOT get written during fallback

When `AutoswitchSource` flips into fallback mode (the rolling
brightness average drops below `AUTOSWITCH_DARK_THRESHOLD`, default
25), the pipeline serves frames from the fallback source (the playlist
of `data/test_videos/*.mp4`) **for visual continuity only**. During
this period:

| Operation | Status |
|---|---|
| Detector + classifier inference | **skipped** (saves GPU) |
| Negative-regions filter | **n/a** (no detections to filter) |
| `detection_events` writes | **none** |
| `logs/events/*.jsonl` writes | **none** |
| `frame_stats` samples | **none** |
| `saved_frames` writes | **none** |
| Image saves to `frames/` | **none** |
| COCO sidecar writes | **none** |
| Smoother updates | **none** (smoother is reset on each transition so playlist tracks never bridge to real-camera tracks) |
| `sensor_readings` / `weather_readings` writes | **continues** (poller is independent of camera state) |
| Reviewer corrections via the dashboard | **continues** (operates on historical events, not the fallback feed) |
| Live MJPEG stream | **continues** (the playlist frame is published to keep the dashboard alive) |
| `(N on-screen)` badge | always reads 0 during fallback |

The dashboard surfaces this state with a banner overlay on the live
stream:

> 🌙 **Camera is dark** — showing previously-recorded clips for visual
> continuity. *No new species, sightings, or detection data are being
> collected during this period.*

This isolation was verified by force-test (2026-05-02): with both
thresholds set to impossibly-high values (200 / 250 luma) to force
permanent fallback, the worker consumed 2,102 playlist frames and
wrote zero rows to `detection_events`, `frame_stats`, or
`saved_frames`.

(The brief startup window — typically 2-10 seconds — before the
autoswitch has accumulated enough rolling samples to confidently call
"dark" *will* see real-camera frames written normally. This is correct:
the autoswitch can't tell whether the camera is dark until it's
measured. The window only happens at worker startup.)

---

## Image saves

Three independent rules, all configurable, any combination on:

| Mode | Env var | When it triggers |
|---|---|---|
| Timelapse | `SAVE_TIMELAPSE_SECONDS` (default 60) | Every N seconds, regardless of detections. Useful for sun-rate timelapses, biofouling tracking. |
| Per-detection | `SAVE_PER_DETECTION` (default false) | Any frame that produced ≥1 detection. Heaviest disk footprint. |
| Interesting-only | `SAVE_INTERESTING_ONLY` (default true) | A frame is "interesting" if (a) it contains a species not seen yet today, OR (b) the highest detection confidence is ≥ `SAVE_INTERESTING_MIN_CONF` (default 0.5), OR (c) the previous save was more than `SAVE_INTERESTING_QUIET_SECONDS` (default 300) ago. |

**Throttling: `SAVE_MIN_TRACK_GAP_SECONDS`** (default 1.0 s) — at most
one save per persistent track per N seconds. A fish lingering in front
of the camera was previously triggering a save on every frame
(~30/sec); the throttle reduces that ~30× without losing the species
discovery moment.

For each save, three files are written:

```
frames/YYYY/MM/DD/HH/<timestamp>_f<frame_id>_<source>.jpg            # raw
frames/YYYY/MM/DD/HH/<timestamp>_f<frame_id>_<source>.annotated.jpg  # bboxes drawn
frames/YYYY/MM/DD/HH/<timestamp>_f<frame_id>_<source>.coco.json      # COCO format
```

The COCO sidecar follows the standard schema (`info`, `images`,
`annotations`, `categories`) and is directly importable into any
COCO-aware ML pipeline.

A row is also written to `saved_frames` linking to those paths plus
the trigger reason (`timelapse`, `interesting:new_species`,
`interesting:high_conf`, `interesting:after_quiet`, `detection`).

**Disk-I/O isolation:** the actual JPEG encode + write happens on a
background save-worker thread inside the inference subprocess. The
inference loop hands off a `SaveJob` (frame buffers explicitly copied
since the shm slot recycles immediately after the loop iteration) and
returns to processing the next frame. A bounded queue (`maxsize=8`)
with drop-oldest semantics keeps the writer from holding up inference
when the disk is slow.

---

## Postgres schema (column-by-column)

All tables, views, and indexes are defined in `db/init.sql`, which is
idempotent and safe to re-run.

### `detection_events`

```
id                BIGSERIAL PRIMARY KEY
ts                TIMESTAMPTZ          -- when the frame was grabbed
frame_id          BIGINT               -- monotone counter from FrameTap
source_name       TEXT                 -- 'seahivecam' | 'pier_cam' | ...
track_id          BIGINT               -- assigned by DetectionSmoother (NULL if smoother disabled)
det_conf          REAL                 -- YOLO objectness score
bbox              INTEGER[]            -- [x1, y1, x2, y2] in source pixels
topk              JSONB                -- [{name, species_id, accuracy}, ...] up to CLASSIFIER_TOPK; [] if classifier was skipped or all picks below MIN_ACCEPT_ACCURACY
best_name         TEXT                 -- topk[0].name; NULL when topk is []
best_species_id   TEXT                 -- topk[0].species_id (binomial for v2; UUID for legacy fishial)
best_accuracy     REAL                 -- topk[0].accuracy
image_path        TEXT                 -- annotated JPEG path on disk, NULL if no save fired for this frame
model_version     TEXT                 -- e.g. 'det:YOLO26 nano@3 | cls:vit_small_patch14_dinov2@temporal_within_source'
detector_sha256   TEXT                 -- 16-char SHA of the detector checkpoint
classifier_sha256 TEXT                 -- 16-char SHA of the active classifier checkpoint
config_hash       TEXT                 -- 16-char SHA of canonical runtime Config
pipeline_git_sha  TEXT                 -- 12-char git rev-parse HEAD at subprocess start
```

Indexes: `(ts DESC)`, `(best_species_id, ts DESC)`, `(source_name, ts DESC)`,
`(track_id) WHERE track_id IS NOT NULL`.

### `frame_stats`

```
ts              TIMESTAMPTZ
source_name     TEXT
frame_id        BIGINT
mean_luma       REAL          -- 0.114*B + 0.587*G + 0.299*R over a 160x90 downsample
mean_r/g/b      REAL          -- channel means over the same downsample
std_luma        REAL          -- contrast proxy
num_detections  INTEGER       -- count post-filter (so suppressed reef-hole boxes don't inflate)
mean_det_conf   REAL          -- mean YOLO conf across detections (0 if none)
model_version   TEXT          -- same as detection_events
config_hash     TEXT
```

### `saved_frames`

```
ts            TIMESTAMPTZ
frame_id      BIGINT
source_name   TEXT
reason        TEXT             -- 'timelapse' | 'interesting:new_species' | ... | 'detection'
image_path    TEXT             -- annotated JPEG path
coco_path     TEXT             -- COCO sidecar JSON path
num_fish      INTEGER          -- count of detections at save time
```

Indexes: `(frame_id, source_name)` is the hot path (joined from
`detection_events` to resolve `image_path` lazily).

### `detection_corrections`

```
id                    BIGSERIAL PRIMARY KEY
event_id              BIGINT NOT NULL REFERENCES detection_events(id) ON DELETE CASCADE
corrected_name        TEXT                     -- NULL + not_a_fish=true → false-positive marker
corrected_species_id  TEXT
not_a_fish            BOOLEAN NOT NULL DEFAULT FALSE
is_endorsement        BOOLEAN NOT NULL DEFAULT FALSE  -- reviewer hit "Looks right"; corrected_name == best_name
corrected_bbox        INTEGER[]                -- redrawn bbox if the reviewer used the bbox editor; NULL otherwise
celebrity_id          TEXT                     -- optional slug from assets/celebrities.json (Bobby, Marty, ...)
confidence            TEXT NOT NULL            -- 'certain' | 'probable' | 'uncertain'
reviewer              TEXT                     -- self-reported reviewer name
notes                 TEXT
created_at            TIMESTAMPTZ NOT NULL
```

Indexes: `(event_id)`, `(reviewer, created_at DESC)`,
`(created_at DESC)`, `(celebrity_id, created_at DESC) WHERE celebrity_id IS NOT NULL`.

### `sensor_readings` (water quality)

```
ts                  TIMESTAMPTZ
deployment_uri      TEXT          -- 'wahoo_2' for the EXO2 sonde co-located with SEAHIVECAM
source              TEXT          -- 'live' | 'synthetic' (banner trigger when synthetic)
water_temp_c        REAL
ph                  REAL
do_pct              REAL          -- dissolved oxygen, % saturation
chlorophyll_rfu     REAL
phycoerythrin_rfu   REAL
turbidity_fnu       REAL
spcond_ms_cm        REAL          -- specific conductance
```

Unique on `(deployment_uri, ts)` to handle backfill idempotency.

### `weather_readings`

Two feeds materialised on each row (NULLs are normal where one feed
doesn't have a column):

```
ts                       TIMESTAMPTZ
deployment_uri           TEXT             -- 'wahoo_2'
source                   TEXT             -- 'live' | 'synthetic'

-- Vaisala WXT-536 station feed (from Wahoo Bay API)
bar_press_hpa            REAL
wind_dir_avg/min/max_deg REAL
wind_speed_avg/min/max_ms REAL
air_temp_c               REAL
rel_humidity_pct         REAL
rain_accum_mm            REAL
rain_dur_frac_hr         REAL
rain_int_mm_hr           REAL
rain_peak_int_mm_hr      REAL
hail_accum/dur/int/peak_int  REAL
water_level_mm           REAL
solar_rad_wm2            REAL

-- Forecast / nearest-METAR layer (Open-Meteo + NWS station KPMP)
cloud_cover_pct          REAL
dew_point_c              REAL
forecast_humidity_pct    REAL
precip_intensity_mm_hr   REAL
precip_prob_pct          REAL
press_sea_level_hpa      REAL
press_surface_hpa        REAL
temp_apparent_c          REAL
uv_index                 REAL
weather_code             INTEGER          -- WMO code (0=clear, 3=overcast, 95=thunderstorm, ...)
weather_description      TEXT             -- human-readable METAR ('Mostly Cloudy', ...)
```

Unique on `(deployment_uri, ts)`.

### `alerts`

```
id            BIGSERIAL PRIMARY KEY
name          TEXT NOT NULL UNIQUE        -- 'pipeline_silence', 'inference_latency_p95', ...
severity      TEXT                        -- 'critical' | 'warning'
description   TEXT
status        TEXT                        -- 'active' | 'acknowledged' | 'resolved'
acked_by      TEXT
opened_at     TIMESTAMPTZ
acked_at      TIMESTAMPTZ
resolved_at   TIMESTAMPTZ
detail_json   JSONB                       -- rule-specific evidence (last seen ts, current value, threshold)
```

Auto-resolve when the rule's condition clears. Manual ack is via
`POST /api/alerts/{id}/ack` (write-token).

### `ptz_states`

```
ts            TIMESTAMPTZ
source_name   TEXT          -- which camera ('seahivecam', ...)
pan_deg       REAL
tilt_deg      REAL
zoom          REAL
```

Currently empty — PTZ poller dormant (no PTZ credentials yet).

### Views and materialised views

| Object | Cardinality | Purpose |
|---|---|---|
| `species_sightings` (view) | One row per persistent track | "Fish counter" — folds the per-frame events into per-track sightings via weighted-vote on dominant species. **Use this for any "how many fish did we see?" question, not raw event count.** |
| `species_sightings_mat` (matview) | Same shape, refreshed every 60 s | Materialised version used by user-facing endpoints so they don't seq-scan `detection_events`. |
| `species_sightings_hourly_mat` (matview) | Hourly rollup | Backs the hourly-trends chart in the dashboard. |
| `species_counts_hourly` (view) | Hourly rollup of `detection_events` by species | Cheap rollup for the species-counts panel. |
| `frame_stats_hourly` (view) | Hourly rollup of `frame_stats` | Drives the drift dashboard. |
| `frame_stats_drift` (view) | 1 h vs 7 d vs 28 d baseline | Source for `drift_*` SLO rules. |

---

## File-system layout (`/raid/scratch/dzimmerman2021/wahoobay/`)

```
frames/
├── YYYY/
│   └── MM/
│       └── DD/
│           └── HH/
│               ├── 20260517T143012Z_f0000951189_seahivecam.jpg
│               ├── 20260517T143012Z_f0000951189_seahivecam.annotated.jpg
│               └── 20260517T143012Z_f0000951189_seahivecam.coco.json
│
logs/
├── app/
│   ├── worker.log
│   ├── worker_pier.log
│   ├── dashboard.log
│   ├── poller.log
│   ├── postgres.log
│   └── cloudflared.log
└── events/
    └── events-YYYY-MM-DD.jsonl       # rotated daily by date
│
assets/
├── species.xlsx                       # Google Sheet snapshot (auto-refreshed by sheet poller)
├── species_manifest.{json,csv}        # derived; gitignored
├── celebrities.json                   # derived; committed
├── taxonomy.csv                       # WoRMS-derived; committed
├── inat_manifest.json                 # iNat photos + licences; committed
├── inat_photos/<binomial>/*.jpg       # photo store; gitignored
└── community_clips/                   # YOLO-cropped fish from Drive; gitignored
│
data/
├── camera_metadata.json               # lat/lng/depth per camera (committed)
├── camera_negative_regions.json       # per-camera polygon mask (committed)
├── models/
│   ├── detector_v26/
│   ├── classifier_v0_10_2/            # legacy Fishial (revert path)
│   └── classifier_v2/                 # DINOv2 ViT-S/14 (default)
├── test_videos/                       # fallback playlist (gitignored)
└── baseline_runs/                     # train_baseline output dirs (gitignored)
```

---

## Exports

The dashboard exposes 14 download endpoints under `/api/export/`. Each
streams CSV from a server-side cursor (so a year of events doesn't
materialise in memory). Several heavy ones also offer Parquet for
pandas/polars users.

| Endpoint | Content | Audience |
|---|---|---|
| `events.csv` (+ `.parquet`) | Every detection event with full top-K + provenance | ML / debugging |
| `species_counts.csv` | Aggregate species counts | Quick stats |
| `species_counts_ci` (via `/api/species_counts_ci`) | Species counts with 95 % CIs | Reports / publication |
| `water_quality.csv` | Sonde readings | Marine biology |
| `weather.csv` | WXT-536 station + Open-Meteo + NWS forecast layer | Marine biology / context |
| `frame_stats.csv` | Drift signals | Ops / debugging |
| `saved_frames.csv` | Index of saved frames + COCO paths | Curation |
| `corrections.csv` | Compact corrections | Reviewer audit |
| `alerts.csv` | SLO alert log | Ops |
| `sightings.csv` (+ `.parquet`) | One row per persistent track ★ | **Marine biology — primary export** |
| `hourly_summary.csv` (+ `.parquet`) | Hour-bucketed events × water quality | Ecology, environmental analysis |
| `tracks_timeline.csv` (+ `.parquet`) | Per-detection rows sorted track-then-time | Trajectory plots |
| `topk_long.csv` (+ `.parquet`) | Flat exploded top-K | Confusion matrices, calibration |
| `labeled_corrections.csv` | Corrections joined with original event + crop path | Fine-tuning input |

Plus two static sidecars:

| Endpoint | What |
|---|---|
| `/api/export/README.md` | Data dictionary explaining every column + units |
| `/api/export/camera_metadata.json` | Lat/lng/depth/heading per camera |

The full spec for each export lives in
`services/dashboard/app/data/README.md`, which is what gets served by
the `README.md` endpoint.

---

## Provenance fingerprint

Every persisted row in `detection_events` and `frame_stats` carries:

| Column | What |
|---|---|
| `model_version` | Human-readable: e.g. `det:YOLO26 nano@3 \| cls:vit_small_patch14_dinov2@temporal_within_source` |
| `detector_sha256` | First 16 chars of SHA-256 of the detector model.pt |
| `classifier_sha256` | First 16 chars of SHA-256 of the **active** classifier's checkpoint (changes when `CLASSIFIER_BACKEND` flips) |
| `config_hash` | First 16 chars of SHA-256 of the canonical JSON of the runtime Config (excluding volatile fields like ports / log levels) |
| `pipeline_git_sha` | First 12 chars of `git rev-parse HEAD` of the worker code at subprocess start |

Combined: any historical detection can be traced back to exactly the
weights, config, and code that produced it. The dashboard's
`/api/provenance/current` endpoint shows the fingerprint of recent
rows; the worker's `provenance.py` computes the same fields at boot.

**Practical use:** when you cut a new classifier checkpoint, the new
`classifier_sha256` shows up on detections written from then on. The
old hash is preserved on every historical row, so you can filter
pre- / post-cutover data trivially in SQL:

```sql
SELECT classifier_sha256, COUNT(*) FROM detection_events
WHERE ts >= NOW() - INTERVAL '7 days'
GROUP BY classifier_sha256;
```

---

## Retention guidance (TBD)

Nothing is auto-deleted. Reasonable defaults once the project commits
to retention policy:

- Frames + COCO sidecars: **30 days** in `frames/` on local disk;
  older → S3 Glacier or deleted depending on Wahoo Bay's data policy.
  Currently ~1-2 GB/day per camera with default save settings.
- `detection_events` rows: **forever** in Postgres, but consider
  archiving to Parquet on S3 quarterly to keep working-set size sane.
  ~10 GB after a few weeks of continuous operation.
- `frame_stats`: **90 days** (drift baselines need ~28 days; 90 gives
  buffer for seasonal comparisons).
- JSONL event log: **30 days**, then compressed and archived.
- `detection_corrections`: **forever** — these are the training data.

This is a stakeholder decision (Wahoo Bay + FAU). Documented as an
outstanding decision in `PLAN.md`.

---

## What we DON'T collect from the public/visitor side

- No tracking of dashboard viewers — no analytics, no cookies beyond
  localStorage for personal UI preferences (write token, reviewer
  email, hide-reef-explorer flag, bbox-toggle state, last-active
  Reports tab).
- No PII collection. Reviewer email is voluntary and stored in
  `detection_corrections.reviewer` — used only to attribute the label.
- No camera footage of identifiable people. The cameras are
  underwater / pier-substructure-facing; if humans appear in frame
  we should consider face obfuscation before any wider public
  deployment (currently in `PLAN.md` as a deferred work item).
