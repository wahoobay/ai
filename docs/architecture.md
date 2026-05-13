# Architecture

## Three services + Postgres

```
┌──────────┐   ┌──────────────┐   ┌─────────────────┐   ┌────────────┐
│ camera   │──▶│ worker       │──▶│ Postgres        │◀──│ dashboard  │──▶ browser
│ (RTSP /  │   │ • inference  │   │ • events        │   │ • REST API │
│  MJPEG)  │   │ • smoother   │   │ • frame_stats   │   │ • exports  │
│          │   │ • autoswitch │   │ • saved_frames  │   │ • UI       │
└──────────┘   │ • saves      │   │ • corrections   │   │ • SLO loop │
               │ • MJPEG out  │   │ • sensor data   │   └────────────┘
               └──────┬───────┘   │ • alerts        │
                      │           │ • ptz_states    │
                      ▼           └────────┬────────┘
              ┌──────────────┐             │
              │ frames/      │             │
              │ logs/events/ │             │
              └──────────────┘             │
                                           │
   ┌──────────────────┐    ┌───────────────┴────────────┐
   │ sensestream      │───▶│ sensor_readings table      │
   │ poller (water Q) │    └────────────────────────────┘
   └──────────────────┘
```

Each service is a separate Python process. All persistence is via
Postgres (DSN `postgresql://wahoobay:wahoobay@localhost:5432/wahoobay`)
plus the local filesystem under `frames/` and `logs/`.

## Worker pipeline (`services/worker/app/`)

```
                                                                ┌──── COCO save ──┐
VideoSource ─▶ FishialPipeline ─▶ DetectionSmoother ─▶ persist ─┤                  │
                                                                ├─ live MJPEG    ─┤
                                                                └────── overlay ─┘
                                                                                  │
                                                                                  ▼
                                                                            dashboard
```

| Component | File | Job |
|---|---|---|
| `VideoSource` | `sources.py` | Yields `(frame_id, frame_bgr, source_name)`. Three concrete sources: `PlaylistSource` (folder of MP4s), `RTSPSource`, `HTTPSource` (MJPEG over HTTP/HTTPS). Plus `AutoswitchSource` that wraps two underlying sources and switches based on rolling mean-luma — see "Autoswitch" below. |
| `FishialPipeline` | `fishial.py` | Wraps Fishial's YOLOv26-nano detector + DINOv2+ViT classifier. Returns `FishDetection(bbox, det_conf, topk: List[Prediction])` per frame. |
| `DetectionSmoother` | `tracker.py` | Constant-velocity tracker. Greedy IoU association between frames, EMA on velocity, blends raw observation with motion prediction. Returns smoothed display detections AND a parallel list of track IDs for the raw detections (so each event row gets stamped with its track). Smoothing is **display-only** — the raw detections are what's persisted. |
| `persist_events` / `_sample_frame_stats` / `ImageSaver` | `pipeline.py`, `persistence.py` | DB writes: `detection_events` (one row per detection per frame), `frame_stats` (1 Hz brightness + colour + detection-rate samples), `saved_frames` (when one of the three save modes triggers). `EventLog` also writes one JSONL line per detection per frame to `logs/events/events-YYYY-MM-DD.jsonl`. |
| FastAPI HTTP layer | `main.py` | `GET /healthz`, `/readyz`, `/snapshot{,_raw}.jpg`, `/stream{,_raw}.mjpeg`, `/live.json`, `/stats`. Both annotated and raw streams are served (the dashboard's bbox-toggle just swaps the `<img src>`). |

### Provenance

Every persisted row carries the SHA256 of the detector + classifier
checkpoints, a 16-character hash of the runtime config, and the git
SHA of the worker code (see `provenance.py`). This lets us reproduce
any historical detection: given a row, you can identify exactly which
weights and code produced it.

### Autoswitch (`AutoswitchSource`)

When a fallback source is configured (`FALLBACK_VIDEO_SOURCE` env var),
the worker wraps both sources in `AutoswitchSource`:

1. Always reads from the **primary** source so we have current frames
   to measure.
2. Computes mean-luma of every Nth frame (default: every 15th, ~1 Hz at
   15 fps), keeps a rolling 60-second window of those samples.
3. Switches to fallback when `avg(luma) < dark_threshold` (default 25);
   switches back to primary when `avg(luma) > light_threshold` (default
   50). The gap is hysteresis — prevents flapping at the boundary.
4. While on fallback, **the pipeline skips ALL video-data writes** —
   no events, no frame_stats, no saves, no smoother updates. The
   fallback frame is only published to the live stream for visual
   continuity. Independent collectors (water quality, alerts) keep
   running. Smoother is reset on each transition so playlist tracks
   never bridge to real-camera tracks. See
   [`data_pipeline.md`](data_pipeline.md) for the formal isolation
   rules.

### Bbox toggle

Each `LiveFrame` carries both an annotated JPEG (bboxes + labels drawn
on) and a raw JPEG. Two MJPEG endpoints (`/stream.mjpeg` and
`/stream_raw.mjpeg`) serve from the same `LiveBuffer`. The dashboard's
toggle just rewrites the `<img src>` between them. Cost: one extra
JPEG encode at the publish rate (≤15 fps), negligible.

## Dashboard (`services/dashboard/app/`)

FastAPI app serving:

- `/` — main HTML page (Jinja2 template).
- `/static/{app.js,style.css}?v=<mtime-hash>` — automatic cache-bust on
  every JS/CSS edit so browsers never see stale UI.
- `/api/live.json`, `/api/stats` — proxy to the worker.
- `/api/stream{,_raw}.mjpeg` — proxy to the worker's two streams.
- `/api/events`, `/api/species_counts`, `/api/water_quality/*`,
  `/api/drift/*`, `/api/alerts/*`, `/api/corrections{,/stats}`,
  `/api/visitor_stats`, `/api/provenance/current` — Postgres-backed.
- `/api/export/<resource>.{csv,parquet}` — streaming downloads, server-
  paginated from a psycopg cursor (so a year of detection events
  doesn't materialise in memory).
- `/api/auth/mode`, `POST /api/corrections`, `POST /api/alerts/{id}/ack`
  — mutation endpoints, optionally gated on a `Bearer` token from
  `DASHBOARD_WRITE_TOKEN`.

### Bootstrap CIs

Every aggregate the dashboard exposes that ships externally
(`/api/species_counts_ci`, `/api/frame_rate_ci`) returns a 95 %
confidence interval — Poisson for counts, Wilson for Bernoulli rates,
percentile-bootstrap for arbitrary statistics. See
`services/dashboard/app/stats.py`.

### SLO checker

Runs as a background asyncio task inside the dashboard process. Every
30 s, six rules query Postgres / probe the worker and poller, and
upsert into the `alerts` table:

1. `pipeline_silence` (critical) — no detection_events in 5 min
2. `frame_stats_stalled` (warning) — drift sampler silent
3. `inference_latency_p95` (warning) — last frame >100 ms
4. `poller_probe_stale` (warning) — sensestream stale
5. `drift_luma_delta` (warning) — current vs 7d brightness shift > 25
6. `drift_fish_rate_crash` (warning) — 1h fish rate < ½ × 7d rate

Alerts auto-resolve when the condition clears. The dashboard banner
surfaces any active alert with an "ack" button; ack is a write-token
mutation.

### Reef explorer + visitor stats

`/api/visitor_stats` is a single bundled endpoint optimised for the
public-friendly Reef explorer card: top-N species (with common names
from `common_names.py`), hourly activity bucketed by local hour-of-day,
latest water-quality reading. Three SVG charts in vanilla JS — no
chart library dependency.

## SenseStream poller (`services/sensestream_poller/app/`)

Tiny standalone service. Polls `https://api.sensestream.org/manager/`
every minute for the `wahoo_2` deployment metadata (unauthenticated;
just confirms the camera is alive). When `SENSESTREAM_AUTH_TOKEN` is
set, also fetches observations from the auth'd endpoint and upserts
into `sensor_readings`. Currently in **stub mode** — token not yet
provided. Synthetic placeholder rows are produced by
`scripts/gen_synthetic_sensor_data.py` until then.

## PTZ poller (`services/worker/app/ptz.py`)

Background thread inside the worker. Off by default; when
`PTZ_POLL_ENABLED=true` and `PTZ_POLL_URL` are set, polls the camera's
pan/tilt/zoom every second via Axis VAPIX
(`/axis-cgi/com/ptz.cgi?query=position`). Writes to `ptz_states`. The
camera's reported PTZ pan can be joined to detection_events at query
time to give every detection a "look direction." Currently dormant —
needs PTZ-capable camera credentials (the FAU role lacks PTZ
privilege).

## Schema (`db/init.sql`)

Idempotent (every `CREATE` is `IF NOT EXISTS`, every `ALTER` is
`ADD COLUMN IF NOT EXISTS`). Re-running the file always converges the
schema to the current state.

| Table | What it holds |
|---|---|
| `detection_events` | One row per detected fish per frame. bbox, top-K JSON, det_conf, classifier accuracy, track_id, image_path, plus the provenance columns. |
| `frame_stats` | 1 Hz drift sampler output: brightness, RGB, detection rate. |
| `saved_frames` | One row per JPEG written to disk, with reason (`timelapse` / `interesting:new_species` / etc.) and COCO sidecar path. |
| `detection_corrections` | Human corrections joined to events via `event_id`. The gold-standard data for fine-tuning. |
| `sensor_readings` | Water-quality samples from the sonde (synthetic or live). |
| `alerts` | SLO alerts with ack/resolve lifecycle. |
| `ptz_states` | Pan/tilt/zoom samples (currently empty; poller dormant). |

| View | What it does |
|---|---|
| `species_sightings` | One row per persistent track, dominant species by weighted vote (sum of accuracy across track lifetime). The "fish counter" — folds out the flip-flop where one fish gets multiple species labels across frames. |
| `species_counts_hourly` | Hourly rollup of detection_events by species. |
| `frame_stats_hourly` | Hourly rollup of drift signals. |
| `frame_stats_drift` | 1 h vs 7 d vs 28 d baseline for drift alerting. |

## What gets exposed publicly

When the Cloudflare tunnel is up, **only the dashboard** (port 18080) is
reachable from the internet. The worker (8081) and poller (8082) are
local-only. There's no inbound port forwarding on the DGX or on FAU's
network — `cloudflared` makes an outbound TLS connection to Cloudflare
and routes return traffic back through it.

The dashboard exposes all read endpoints freely; mutation endpoints
(corrections, alert ack) require a `Bearer` token from
`DASHBOARD_WRITE_TOKEN` (stored at
`/raid/scratch/dzimmerman2021/wahoobay/.dashboard_write_token`,
mode 600, gitignored).
