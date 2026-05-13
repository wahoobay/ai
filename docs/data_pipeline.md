# Data pipeline

What gets stored, where, and — important — when it does NOT get stored.

## The two collection streams

```
┌──────────────────────────────────────────┐    ┌──────────────────────────┐
│ video → detector → classifier → smoother │    │ sonde via SenseStream    │
│ ↓                                         │    │ ↓                        │
│ detection_events (raw, every frame)       │    │ sensor_readings          │
│ frame_stats (1 Hz drift samples)          │    │                          │
│ saved_frames + JPEG + COCO sidecars      │    │                          │
└──────────────────────────────────────────┘    └──────────────────────────┘
                          ↑                                      ↑
              gated by "is the camera live?"            independent of camera
```

The two streams are **architecturally separate**. The water-quality
poller doesn't depend on the worker; the worker doesn't depend on the
poller. This matters at night.

## Per-frame writes (live camera path)

For every frame from the **primary** (live) source:

| Destination | Always written? | Notes |
|---|---|---|
| `detection_events` | One row per detected fish | Includes top-K, bbox, det_conf, classifier accuracy, track_id, image_path (if saved), plus all 5 provenance columns. |
| `frame_stats` | Sampled at `FRAME_STATS_EVERY_N_FRAMES` (default 30 frames ≈ 1 Hz) | Brightness + per-channel RGB + std-luma + detection count + mean det conf. Used by the input-drift monitor. |
| `saved_frames` + disk | Conditional on three knobs | (1) timelapse cadence, (2) per-detection, (3) interesting-only. See "Image saves" below. |
| `logs/events/events-YYYY-MM-DD.jsonl` | One line per detection per frame | Same content as `detection_events` row, JSON-encoded. |

For every frame, the live MJPEG stream is updated (annotated and raw
versions both encoded and published). The smoother updates its tracks
and produces the smoothed bboxes used for the live overlay.

## What does NOT get written during fallback

When `AutoswitchSource` flips into fallback mode (the rolling brightness
average drops below `AUTOSWITCH_DARK_THRESHOLD`, default 25), the
pipeline serves frames from the fallback source (typically the YouTube
playlist) **for visual continuity only**. During this period:

| Operation | Status |
|---|---|
| Detector + classifier inference | **skipped** (saves GPU) |
| `detection_events` writes | **none** |
| `logs/events/*.jsonl` writes | **none** |
| `frame_stats` samples | **none** |
| `saved_frames` writes | **none** |
| Image saves to `frames/` | **none** |
| COCO sidecar writes | **none** |
| Smoother updates | **none** (smoother is reset on each transition so playlist tracks never bridge to real-camera tracks) |
| `sensor_readings` writes | **continues** (poller is independent) |
| Live MJPEG stream | **continues** (the playlist frame is published to keep the dashboard alive) |
| `(N on-screen)` badge | always reads 0 during fallback |
| Existing event corrections via the dashboard UI | **continues** (operates on historical events, not the fallback feed) |

The dashboard surfaces this state with a banner overlay on the live
stream:

> 🌙 **Camera is dark** — showing previously-recorded clips for visual
> continuity. *No new species, sightings, or detection data are being
> collected during this period.*

This isolation was verified by force-test on 2026-05-02: with both
thresholds set to impossibly-high values (200/250 luma) to force
permanent fallback, the worker consumed 2,102 playlist frames and
wrote zero rows to `detection_events`, `frame_stats`, or
`saved_frames`.

(The brief startup window — typically 2–10 seconds — before the
autoswitch has accumulated enough rolling samples to confidently call
"dark" *will* see real-camera frames written normally. This is
correct: the autoswitch can't tell whether the camera is dark until
it's measured. The window only happens at worker startup.)

## Image saves

Three independent rules, all configurable, any combination on:

| Mode | Env var | When it triggers |
|---|---|---|
| Timelapse | `SAVE_TIMELAPSE_SECONDS` (default 0 = off) | Every N seconds, regardless of detections. Useful for sun-rate timelapses, biofouling tracking. |
| Per-detection | `SAVE_PER_DETECTION` (default false) | Any frame that produced ≥1 detection. Heaviest disk footprint. |
| Interesting-only | `SAVE_INTERESTING_ONLY` (default true) | A frame is "interesting" if (a) it contains a species not seen yet today, OR (b) the highest detection confidence is ≥ `SAVE_INTERESTING_MIN_CONF` (default 0.5), OR (c) the previous save was more than `SAVE_INTERESTING_QUIET_SECONDS` (default 300) ago. |

For each save, three files are written:

```
frames/YYYY/MM/DD/HH/<timestamp>_f<frame_id>_<source>.jpg            # raw
frames/YYYY/MM/DD/HH/<timestamp>_f<frame_id>_<source>.annotated.jpg  # bboxes drawn
frames/YYYY/MM/DD/HH/<timestamp>_f<frame_id>_<source>.coco.json      # COCO format
```

The COCO sidecar follows the standard schema (`info`, `images`,
`annotations`, `categories`) and is directly importable into any
COCO-aware ML pipeline.

A row is also written to `saved_frames` linking to those paths plus the
trigger reason (`timelapse`, `interesting:new_species`,
`interesting:high_conf`, `interesting:after_quiet`, `detection`).

## Postgres schema

| Table / view | Cardinality (rough) | Purpose |
|---|---|---|
| `detection_events` | ~10 rows/sec when active | Authoritative event log. One row per detected fish per frame. Smoother adds `track_id`. Provenance columns identify the model + code that produced it. |
| `species_sightings` (view) | One row per persistent track | "Fish counter" — folds the per-frame events into per-track sightings via weighted-vote on dominant species. **Use this for any "how many fish did we see?" question, not raw event count.** |
| `frame_stats` | ~1 row/sec | Drift sampler: brightness, RGB, detection count. Drives `frame_stats_drift` (1 h vs 7 d vs 28 d) for input-drift alerts. |
| `saved_frames` | Depends on save modes; ~thousands/day with default config | Index of frames + COCO sidecars on disk. |
| `detection_corrections` | Sparse (a few /day at scale) | Human review labels. Joined to events via `event_id`. The gold-standard data for fine-tuning. |
| `sensor_readings` | One row per 10 min (sonde cadence) | Water-quality readings. Tagged `source='live'` (real sonde) or `source='synthetic'` (placeholder). |
| `alerts` | Stable size (~20 rows total) | SLO alerts: pipeline silence, latency, drift. Auto-resolves when condition clears. |
| `ptz_states` | Currently empty | PTZ pose snapshots. Filled when PTZ poller has credentials. |

All tables and views are defined in `db/init.sql`, which is idempotent
and safe to re-run.

## Exports

The dashboard exposes 13 download endpoints under `/api/export/`. Each
streams CSV from a server-side cursor (so a year of events doesn't
materialise in memory). Four heavy ones also offer Parquet for
pandas/polars users.

| Endpoint | What | Audience |
|---|---|---|
| `events.csv` (+ `.parquet`) | Every detection event with full top-K + provenance | ML / debugging |
| `species_counts.csv` | Aggregate species counts | Quick stats |
| `species_counts_ci` | Species counts with 95% CIs | Reports / publication |
| `water_quality.csv` | Sonde readings | Marine biology |
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
| `README.md` | Data dictionary explaining every column + units |
| `camera_metadata.json` | Lat/lng/depth/heading per camera |

The full spec for each export is in
`services/dashboard/app/data/README.md`, which is what gets served by
the `README.md` endpoint.

## Provenance fingerprint

Every persisted row in `detection_events` and `frame_stats` carries:

| Column | What |
|---|---|
| `model_version` | Human-readable: e.g. `det:YOLO26 nano@3 \| cls:DinoV2-224 + ViT Pooling 3 head + ArcFace + KNN@10.2` |
| `detector_sha256` | First 16 chars of SHA256 of the detector model.pt |
| `classifier_sha256` | Same for the classifier bundle |
| `config_hash` | First 16 chars of SHA256 of the canonical JSON of the runtime Config (excluding volatile fields like ports/log levels) |
| `pipeline_git_sha` | First 12 chars of `git rev-parse HEAD` of the worker code |

Combined: any historical detection can be traced back to exactly the
weights, config, and code that produced it. The dashboard's
`/api/provenance/current` endpoint shows the fingerprint of recent
rows.

## Retention guidance (TBD)

Nothing is auto-deleted. Reasonable defaults once the project commits
to retention policy:

- Frames + COCO sidecars: **30 days** in `frames/` on local disk; older
  → S3 Glacier or deleted depending on Wahoo Bay's data policy.
- `detection_events` rows: **forever** in Postgres, but consider
  archiving to Parquet on S3 quarterly to keep working-set size sane.
- `frame_stats`: **90 days** (drift baselines need ~28 days; 90 gives
  buffer for seasonal comparisons).
- JSONL event log: **30 days**, then compressed and archived.

This is a stakeholder decision (Wahoo Bay + FAU). Documented as an
outstanding decision in `PLAN.md`.

## What we DON'T collect from the public/visitor side

- No tracking of dashboard viewers — no analytics, no cookies beyond
  localStorage for personal UI preferences (write token, reviewer
  email, hide-reef-explorer flag, bbox-toggle state).
- No PII collection. Reviewer email is voluntary and stored in
  `detection_corrections.reviewer` — used only to attribute the label.
- No camera footage of identifiable people. The camera is underwater /
  pier-substructure-facing; if humans appear we should consider face
  obfuscation before any wider public deployment (currently in
  `PLAN.md` as a deferred work item).
