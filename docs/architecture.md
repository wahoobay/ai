# Architecture

A practical tour of every long-running process, every Postgres table,
and every place data crosses a boundary. The README has the
one-paragraph version; this is the room-by-room walk-through. New
engineers should be able to follow the data path end-to-end after
reading this.

## Process model

```
┌──────────┐   ┌─────────────────────┐   ┌─────────────────────────┐   ┌────────────────┐
│ camera   │──▶│ worker (parent)     │──▶│ Postgres                │◀──│ dashboard      │──▶ browser
│ (Axis    │   │ • FrameTap thread   │   │ • detection_events      │   │ • REST + UI    │
│  PTZ /   │   │ • raw MJPEG buffer  │   │ • detection_corrections │   │ • CSV exports  │
│  RTSP)   │   │ • shm pool          │   │ • saved_frames          │   │ • reviewer UI  │
└──────────┘   └────────┬────────────┘   │ • frame_stats           │   │ • SLO + matview│
                        │ /dev/shm       │ • sensor_readings       │   │   refreshers   │
                        │ (zero-copy)    │ • weather_readings      │   │ • sheet poller │
                        ▼                │ • alerts                │   └────────────────┘
              ┌─────────────────────┐    │ • ptz_states            │
              │ inference subprocess│    │ • species_sightings_mat │
              │ • YOLO detector     │    └────┬────────────────────┘
              │ • negative-regions  │         │
              │ • v2 OR fishial cls │         │
              │ • smoother + tracks │         │
              │ • persistence       │         │
              │ • JPEG encode       │         │
              └─────────────────────┘         │
                                              │
                ┌────────────────────┐        │
                │ sensestream poller │────────┘
                │ (water + weather)  │
                └────────────────────┘
```

**Four long-running services, all written in Python 3.11**, isolated
to their own processes for blast-radius and CUDA-state reasons:

| Service | Module | Purpose |
|---|---|---|
| `worker` (×2) | `services/worker/app/` | RTSP ingest + GPU inference + DB writes + annotated MJPEG. **One worker per camera** — SEAHIVECAM on `:8081`, pier cam on `:8083`. Each worker spawns its own inference subprocess. |
| `dashboard` | `services/dashboard/app/` | FastAPI web UI + REST API + reviewer queue + CSV/Parquet exports + SLO/matview/sheet-poller background tasks. |
| `sensestream_poller` | `services/sensestream_poller/app/` | Pulls water-quality + weather, writes to Postgres. |
| `postgres` | (built-in `pg_ctl`) | Persistence. Local cluster under `./pgdata/`. |

Everything talks via:
1. **Postgres** — the durable substrate. DSN
   `postgresql://wahoobay:wahoobay@localhost:5432/wahoobay`.
2. **Local filesystem** — `frames/<YYYY>/<MM>/<DD>/<HH>/*.jpg` for
   saved frames, `logs/events/events-YYYY-MM-DD.jsonl` for the
   structured JSONL event log (durable mirror of `detection_events`),
   `logs/app/*.log` for runtime logs.
3. **HTTP** — workers expose live snapshots + MJPEG; the dashboard
   proxies these to browsers. No worker→worker traffic.

The dashboard never imports CUDA / torch / ultralytics. Workers never
import the dashboard's HTTP layer. Each side is replaceable in
isolation.

---

## Worker pipeline (`services/worker/app/`)

A worker is two OS processes:

- **Parent**: FastAPI app, `FrameTap` reader thread, shared-memory
  pool, drain thread for the inference subprocess's output queue.
- **Inference subprocess**: detector + classifier + smoother +
  persistence + JPEG encode. Spawned via `multiprocessing.get_context("spawn")`
  so the parent inherits no CUDA state.

```
                          ┌─────────────────────────────────────┐
                          │ FrameTap (thread, parent process)   │
                          │  - read frame from VideoSource      │
                          │  - encode raw JPEG → live_raw       │
                          │  - write BGR bytes into shm slot    │
                          │  - put FrameIn(slot_idx, shape...)  │
                          │     onto filled_q                   │
                          └─────────────────────┬───────────────┘
                                                │
                                                ▼  filled_q (mp.Queue)
                  ┌────────────────────────────────────────────────────┐
                  │ Inference subprocess                              │
                  │   - get FrameIn metadata from filled_q             │
                  │   - reconstruct numpy view over /dev/shm slot      │
                  │   - YOLO detector → list of (bbox, det_conf)       │
                  │   - NegativeRegionFilter drops false-positive boxes│
                  │   - classifier.process_frame → top-K species       │
                  │   - DetectionSmoother → display + track IDs        │
                  │   - persist to Postgres + JSONL                    │
                  │   - ImageSaver.maybe_save → SaveJob → save thread  │
                  │   - cv2.imencode annotated JPEG                    │
                  │   - put FrameOut onto out_queue                    │
                  │   - return slot to empty_q (finally block)         │
                  └────────────────────┬─────────────────────────┬─────┘
                                       │  out_queue              │  empty_q
                                       ▼                         ▼
                          ┌─────────────────────────┐  (slots returned)
                          │ drain thread (parent)   │
                          │  - get FrameOut         │
                          │  - publish to live_anno │──▶ /stream.mjpeg
                          └─────────────────────────┘
```

### Shared-memory frame hop (`frame_shm.py`)

The frame bytes do **not** travel through the multiprocessing pickle
path. Before the swap, a 720p frame was 2.7 MB per pickle and the
round-trip burned ~20 ms (≈30 % of the 15 fps budget). Now:

- The parent pre-allocates `SHM_NUM_SLOTS=3` shared-memory segments at
  `SHM_MAX_H × SHM_MAX_W × 3 = 6.2 MB` each (sized for 1080p headroom).
- `FrameTap` `memcpy`s the cv2 frame into a free slot, then drops a
  ~80-byte `FrameIn` metadata tuple (`slot_idx`, `shape`, `ts`, ...)
  on the queue.
- The subprocess attaches via `FrameShmReader` and reconstructs a
  **zero-copy numpy view** over the slot. The classifier, annotator,
  and JPEG encoder all run against that view.
- After the iteration completes (in a `finally` block — so a crash can't
  leak slots), the subprocess returns the slot to `empty_q`.
- Backpressure: if all slots are in-flight when a new frame arrives,
  FrameTap pops the oldest queued `FrameIn` and recycles its slot
  (latest-frame-wins). This keeps inference from chasing stale frames
  when it momentarily falls behind the camera.

Result: sustained 15 fps grab / 15 fps infer on both cameras (native
camera rate, no drops). See [`docs/operations.md`](operations.md) for
the resolution / compression / JPEG-quality knobs that tune the
remaining budget.

### Components

| Component | File | Job |
|---|---|---|
| `VideoSource` | `sources.py` | Yields `(frame_id, frame_bgr, source_name)`. Three concrete sources: `PlaylistSource` (folder of MP4s, used as the dark-time fallback), `RTSPSource`, `HTTPSource` (MJPEG over HTTP/HTTPS). `AutoswitchSource` wraps a primary + fallback and switches based on rolling mean-luma — see "Autoswitch" below. |
| Classifier backend | `classifier_v2.py` (default) **or** `fishial.py` (legacy) | The inference subprocess selects one at boot from `CLASSIFIER_BACKEND`. **v2** is a DINOv2 ViT-S/14 trained from random init on our crops + iNat photos (223 classes), accelerated with `torch.compile(mode="reduce-overhead")` so single-crop latency is ~0.7 ms after warmup. **fishial** wraps the legacy 866-class Fishial ArcFace + sub-centre + KNN classifier. Both share the same YOLO v26 detector and return `list[FishDetection]` (defined in `pipeline_types.py`) so the rest of the pipeline is backend-agnostic. |
| `NegativeRegionFilter` | `negative_regions.py` | Drops detector false-positives on stationary reef geometry (SEAHIVE cut-outs). Per-camera polygons loaded from `data/camera_negative_regions.json`. A detection is dropped only when its bbox-IoU with the polygon's bounding rect clears `min_iou` (default 0.5) — so the hole's own large false positive matches and drops, while a fish swimming through has a tiny bbox and small IoU and is kept. |
| `DetectionSmoother` | `tracker.py` | Constant-velocity tracker. Greedy IoU association between frames, EMA on velocity, blends raw observation with motion prediction. Returns smoothed display detections AND a parallel list of track IDs for the raw detections (so each event row gets stamped with its track). Smoothing is **display-only** — the raw detections are what's persisted. |
| Persistence | `persistence.py`, `pipeline.py` | `PgWriter.record_detections` writes a row per detection per frame to `detection_events`. `EventLog` mirrors each event to JSONL on disk (durable backup). `ImageSaver.maybe_save` decides when to write an annotated + raw JPEG (three modes, see [`data_pipeline.md`](data_pipeline.md)); writes happen in a background save-worker thread inside the subprocess so the inference loop never blocks on disk I/O. |
| Annotation | `overlay.py` | Draws bboxes, species labels, confidence chips onto a frame. Returns a fresh buffer (copies the frame internally) so the annotated output is safe across the shm slot release. |
| FastAPI HTTP layer | `main.py` | `GET /healthz`, `/readyz`, `/snapshot{,_raw}.jpg`, `/stream{,_raw}.mjpeg`, `/live.json`, `/stats`. Both annotated and raw streams are served (the dashboard's bbox-toggle just swaps the `<img src>`). |

### Provenance

Every persisted row carries:

- `model_version` — human-readable identifier (e.g. `det:YOLO26 nano@3 | cls:vit_small_patch14_dinov2@temporal_within_source`)
- `detector_sha256` — first 16 chars of the detector checkpoint's SHA-256
- `classifier_sha256` — first 16 chars of the **active** classifier's checkpoint (changes when `CLASSIFIER_BACKEND` flips)
- `config_hash` — first 16 chars of a canonical-JSON hash of the runtime Config (drops non-functional fields like ports + log level)
- `pipeline_git_sha` — short SHA from `git rev-parse HEAD` at subprocess start

Given any row, you can identify exactly which weights + which code +
which config produced it. The four columns are stamped from `provenance.py`
(`compute(cfg)`) at subprocess start; the row insert just passes them
through unchanged.

### Autoswitch (`AutoswitchSource`)

When a fallback source is configured (`FALLBACK_VIDEO_SOURCE` env), the
worker wraps both sources in `AutoswitchSource`:

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
   never bridge to real-camera tracks.

The dashboard banner shows an "autoswitch active" indicator pulled from
`GET /stats` so viewers know the live data is on pause.

### Bbox toggle

Each `LiveFrame` carries both an annotated JPEG (bboxes + labels drawn
on) and a raw JPEG. Two MJPEG endpoints (`/stream.mjpeg` and
`/stream_raw.mjpeg`) serve from the same `LiveBuffer`. The dashboard's
toggle just rewrites the `<img src>` between them. Cost: one extra
JPEG encode at the publish rate (≤15 fps), negligible.

### Backend switch in one place

```python
# inference_proc.py
backend = (cfg.classifier_backend or "v2").strip().lower()
if backend == "v2":
    from .classifier_v2 import ClassifierV2Pipeline as _Pipeline
elif backend == "fishial":
    from .fishial import FishialPipeline as _Pipeline
else:
    raise RuntimeError(...)
classifier = _Pipeline(cfg)
```

The rest of the loop calls `classifier.process_frame(frame_bgr)` and
gets `list[FishDetection]` back, agnostic of which backend produced it.
`provenance.py` peeks at `cfg.classifier_backend` to hash the right
checkpoint — see "Provenance" above.

---

## Dashboard (`services/dashboard/app/`)

FastAPI app, single file `main.py` (≈ 2k lines, broadly organized by
endpoint group). Notable other modules:

- `common_names.py` — Latin → curated common-name lookup with a
  manifest-fallback layer for the long tail (97.8 % coverage on the v2
  class set, up from 21 % when only the curated dict was used).
- `exports.py` — psycopg async-cursor streamers for every CSV/Parquet
  download. Server-paginates so a year of detection events doesn't
  materialise in memory.
- `alerts.py` — SLO rule definitions + the async evaluator.
- `matviews.py` — background refresher for `species_sightings_mat`
  and friends.
- `stats.py` — Wilson / Poisson / bootstrap CIs for
  `/api/species_counts_ci` and `/api/frame_rate_ci`.
- `sheet_poller.py` — daily Google Sheet refresh task (see below).

### URL surface, by concern

| Surface | Endpoints |
|---|---|
| Page + assets | `GET /`, `GET /static/{app.js,style.css}?v=<mtime-hash>` |
| Worker proxies | `GET /api/live.json`, `/api/stats`, `/api/snapshot{,_pier}.jpg`, `/api/stream{,_raw,_pier,_pier_raw}.mjpeg` |
| Read endpoints (Postgres) | `/api/events`, `/api/species_counts`, `/api/species/list`, `/api/water_quality/*`, `/api/weather/*`, `/api/drift/*`, `/api/alerts/*`, `/api/corrections{,/stats}`, `/api/visitor_stats`, `/api/provenance/current`, `/api/celebrities`, `/api/celebrities/{id}` |
| Mutation endpoints (reviewer or admin session) | `POST /api/corrections` |
| Mutation endpoints (admin session only) | `POST /api/alerts/{id}/ack`, `POST /api/admin/negative_regions`, `POST /api/admin/refresh_sheets`, plus the account-management routes below |
| Account management (admin session only) | `POST /api/admin/users`, `POST /api/admin/users/{id}/approve`, `POST /api/admin/users/{id}/role`, `POST /api/admin/users/{id}/deactivate`, `POST /api/admin/users/{id}/reset-link`, `GET /api/admin/sessions`, `POST /api/admin/sessions/{id}/revoke` |
| Detection thumbnails | `GET /api/detections/{event_id}/roi.jpg`, `/frame.jpg` |
| Streaming exports | `GET /api/export/{events,sightings,hourly_summary,water_quality,weather,frame_stats,saved_frames,corrections,alerts,species_counts,tracks_timeline,topk_long,labeled_corrections}.csv` (Parquet variant where flagged) |
| Static doc sidecars | `GET /api/export/README.md`, `/api/export/camera_metadata.json` |
| Admin UI | `GET /admin/regions.html` (interactive polygon-drawing for negative regions), `GET /admin/users.html` (account/session management) |

All read endpoints are open, including to anonymous visitors. Every
mutation endpoint requires a session cookie from `POST /api/auth/login`
(see "Accounts and roles" in `docs/operations.md`) — `/api/corrections`
accepts the `reviewer` or `admin` role, every other mutation endpoint
requires `admin`. Enforced by the `require_role()` dependency in
`services/dashboard/app/auth.py`, not by a bearer token — the token
gate this table used to describe (`DASHBOARD_WRITE_TOKEN`) was retired
along with the token itself; see "How to extend the system" below for
how to gate a new endpoint the same way.

### Background tasks

Three async tasks run inside the dashboard process:

1. **`SLOChecker`** — every 30 s, six rules query Postgres / probe the
   worker + poller, upsert into the `alerts` table. Acknowledgement +
   resolution are in-band. The six current rules:
    1. `pipeline_silence` (critical) — no detection_events in 5 min
    2. `frame_stats_stalled` (warning) — drift sampler silent
    3. `inference_latency_p95` (warning) — last frame >100 ms
    4. `poller_probe_stale` (warning) — sensestream stale
    5. `drift_luma_delta` (warning) — current vs 7 d brightness shift > 25
    6. `drift_fish_rate_crash` (warning) — 1 h fish rate < ½ × 7 d rate

2. **`MatviewRefresher`** — every `MATVIEW_REFRESH_INTERVAL_S`
   (60 s default) refreshes `species_sightings_mat` and
   `species_sightings_hourly_mat`. These back the user-facing aggregate
   endpoints (Fish Explorer, Reef cards, etc.) so they don't seq-scan
   `detection_events` per request.

3. **`SheetPoller`** — every `SHEET_POLL_INTERVAL_HOURS` (24 h default)
   fetches the Wahoo Bay Google Sheet's xlsx export. When the bytes
   change: writes `assets/species.xlsx`, subprocess-runs
   `scripts/build_species_manifest.py` and `scripts/build_celebrities.py`,
   then hot-reloads the in-memory celebrities catalogue + the manifest
   common-name fallback layer. No dashboard restart needed for the
   chatters' new entries to surface. Manual force: `POST /api/admin/refresh_sheets`
   (admin session required).

### Reviewer queue

The reviewer flow is the dashboard's most consequential feature — every
correction becomes training data for the next classifier checkpoint.

The flow:

1. `GET /api/events` returns the latest N detection events (default 40)
   that have not been reviewed yet. The query joins to
   `detection_corrections` and excludes any event with a row there.
2. Each event renders as a card in the "Recent events" list with a
   thumbnail (ROI crop from the saved frame), species + accuracy,
   timestamp, and source camera.
3. Clicking the thumbnail opens a modal lightbox with three views:
   - **ROI** — the cropped fish region (default)
   - **Full frame** — the whole frame with bbox overlay
   - **✎ Edit bbox** — SVG canvas with draggable corner + edge handles for
     the reviewer to redraw the box (only when the detector clipped or
     mis-sized it)
4. Under each event: a correction form with:
   - **✓ Looks right** — one-click endorsement (writes `is_endorsement=true`)
   - **Type the correct species name** — autocomplete from the loaded
     species catalogue
   - **Top-K alternatives** — one-click chips for the model's 2nd and
     3rd guesses (when softmax for that pick cleared `min_accept_accuracy`)
   - **✗ Not a fish** — false-positive marker (writes `not_a_fish=true`,
     `corrected_name=null`)
   - **Tag as celebrity** — optional dropdown filtered by the species
     being corrected toward (`celebrity_id` slug from `celebrities.json`)
   - **Confidence** — `certain` / `probable` / `uncertain`
   - **Notes** — free-text
5. On submit, `POST /api/corrections` writes the row and
   `EVENT_CACHE.delete(event_id)` on the client drops the event from the
   list (a faded-out animation is just visual feedback; the source of
   truth is `GET /api/events` filtering reviewed events out).

The "Reviewing as: <name>" chip at the top stores the reviewer name in
localStorage so they don't have to retype it. Reviewer identity flows
through to `detection_corrections.reviewer`.

### Celebrities

A small read-only catalogue plus a reviewer-tagging integration:

- `assets/celebrities.json` is the source-of-truth list (parsed from the
  "Fish named by Chatters" tab of the Wahoo Bay Google Sheet). 25
  named recurring animals at last count.
- The dashboard loads it at startup and on every sheet-poll refresh,
  caches per-celebrity sightings counts + the canonical photo (most
  recent reviewer-tagged detection's ROI).
- The Reports panel has a "Celebrities" tab with a card grid. Each
  card → detail modal with the hero photo + every tagged sighting as a
  thumbnail; clicking a thumbnail jumps into that event's ROI modal.
- The reviewer correction form has the "Tag as celebrity" dropdown
  (above). Each tag updates the celebrity's gallery + sightings count
  on the next API hit; the canonical photo auto-improves over time.

### Bootstrap CIs

Every aggregate the dashboard exposes that ships externally
(`/api/species_counts_ci`, `/api/frame_rate_ci`) returns a 95 %
confidence interval — Poisson for counts, Wilson for Bernoulli rates,
percentile-bootstrap for arbitrary statistics. See
`services/dashboard/app/stats.py`.

---

## SenseStream poller (`services/sensestream_poller/app/`)

Tiny standalone service. Pulls water-quality + weather readings on a
schedule, writes to Postgres. The current fallback chain is:

1. **Wahoo Bay public API** (`api.wahoobay.net`) — preferred. Has both
   the YSI EXO2 sonde data and the Vaisala WXT-536 weather station.
2. **SenseStream direct** (`api.sensestream.org`) — fallback when the
   Wahoo Bay API times out. Needs `SENSESTREAM_AUTH_TOKEN`.
3. **Synthetic** — only when both APIs have been silent long enough
   that we'd otherwise have a gap. Marked `source='synthetic'` in the
   row so the dashboard can banner it correctly.

Forecast layer (additive, never overrides the station feed):

4. **Open-Meteo** — free, no auth, fills cloud cover / UV index / etc.
5. **NWS METAR (KPMP)** — human-readable description string for the
   weather banner.

Schedule: poll interval default 300 s; backfill window 60 min so a
worker outage doesn't leave a gap. Schema:

- `sensor_readings` (sonde): `water_temp_c`, `ph`, `do_pct`,
  `chlorophyll_rfu`, `phycoerythrin_rfu`, `turbidity_fnu`,
  `spcond_ms_cm`, …
- `weather_readings`: every column of WXT-536 + every Open-Meteo /
  NWS field. See [`docs/data_pipeline.md`](data_pipeline.md) for the
  full schema.

---

## PTZ poller (`services/worker/app/ptz.py`)

Background thread inside the worker. Off by default; when
`PTZ_POLL_ENABLED=true` and `PTZ_POLL_URL` are set, polls the camera's
pan/tilt/zoom every second via Axis VAPIX
(`/axis-cgi/com/ptz.cgi?query=position`). Writes to `ptz_states`. The
camera's reported PTZ pan can be joined to `detection_events` at query
time to give every detection a "look direction." Currently dormant —
needs PTZ-capable camera credentials (the FAU role lacks PTZ
privilege).

---

## Schema (`db/init.sql`)

Idempotent (every `CREATE` is `IF NOT EXISTS`, every `ALTER` is
`ADD COLUMN IF NOT EXISTS`). Re-running the file always converges the
schema to the current state. See [`docs/data_pipeline.md`](data_pipeline.md)
for the column-by-column reference.

| Table | What it holds | Written by |
|---|---|---|
| `detection_events` | One row per detected fish per frame. bbox, top-K JSON, det_conf, classifier accuracy, track_id, image_path, plus the four provenance columns. | Worker (inference subprocess) |
| `frame_stats` | ~1 Hz drift sampler: brightness, RGB, detection rate. | Worker |
| `saved_frames` | One row per JPEG written to disk, with reason (`timelapse` / `interesting:new_species` / etc.) and COCO sidecar path. | Worker (save-worker thread) |
| `detection_corrections` | Human corrections joined to events via `event_id`. Includes optional `corrected_bbox` (redraw) and `celebrity_id` (named-animal tag). | Dashboard (`POST /api/corrections`) |
| `sensor_readings` | Water-quality samples from the sonde. | SenseStream poller |
| `weather_readings` | Weather (WXT-536 station + Open-Meteo + NWS). | SenseStream poller |
| `alerts` | SLO alerts with ack/resolve lifecycle. | Dashboard (`SLOChecker`) |
| `ptz_states` | Pan/tilt/zoom samples. | Worker (`ptz.py`, currently dormant) |

**Views and materialised views:**

| Object | What it does |
|---|---|
| `species_sightings` (view) | One row per persistent track, dominant species by weighted vote (sum of accuracy across track lifetime). The "fish counter" — folds out the flip-flop where one fish gets multiple species labels across frames. |
| `species_sightings_mat` (matview) | Materialised version of `species_sightings`. Refreshed every `MATVIEW_REFRESH_INTERVAL_S` (60 s default) by the dashboard. |
| `species_sightings_hourly_mat` (matview) | Hourly rollup of the above. |
| `species_counts_hourly` (view) | Hourly rollup of `detection_events` by species. |
| `frame_stats_hourly` (view) | Hourly rollup of drift signals. |
| `frame_stats_drift` (view) | 1 h vs 7 d vs 28 d baseline for drift alerting. |

---

## What gets exposed publicly

When the Cloudflare tunnel is up, **only the dashboard** (port 18080) is
reachable from the internet. The worker (8081 / 8083) and poller
(8082) are local-only. There's no inbound port forwarding on the DGX
or on FAU's network — `cloudflared` makes an outbound TLS connection
to Cloudflare and routes return traffic back through it.

The dashboard exposes all read endpoints freely, including over the
public Cloudflare tunnel — anonymous visitors see the live fish view
unchanged. Every mutation endpoint requires an authenticated session
cookie (`POST /api/auth/login`) carrying the `reviewer` or `admin`
role; the admin endpoints (`/admin/*.html`, `POST
/api/admin/refresh_sheets`, `POST /api/admin/negative_regions`, and
the account/session-management routes) require `admin` specifically.
This replaced an earlier bearer-token scheme (`DASHBOARD_WRITE_TOKEN`)
that has since been removed entirely. Unlike that scheme, the admin
UI and admin endpoints are reachable through the public tunnel the
same as everything else — the design's whole premise is that
session/role auth, not network topology, is what has to hold here. See
"Accounts and roles" in `docs/operations.md` and
`docs/superpowers/specs/2026-09-22-reviewer-auth-rbac-design.md` for
the full design.

---

## How to extend the system

Common changes, with the file you'd touch:

| Change | Where |
|---|---|
| Add a new column to `detection_events` | `db/init.sql` (`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`) + apply to the live DB + update `services/worker/app/persistence.py` `PgWriter.record_detections` |
| Add a new SLO rule | `services/dashboard/app/alerts.py` `SLO_RULES` list |
| Add a CSV export | `services/dashboard/app/exports.py` (new streamer) + `services/dashboard/app/main.py` (new endpoint) + `services/dashboard/app/templates/index.html` (new button) |
| Add a new view to the Reports panel | `services/dashboard/app/templates/index.html` (new tab + panel) + `services/dashboard/app/static/app.js` (`tickReports` dispatcher + renderer) + any new API endpoints |
| Add a new camera source | Add fields to `.env` (`VIDEO_SOURCE_X`, `SOURCE_LABEL_PRIMARY_X`, `WORKER_HTTP_PORT_X`, `CUDA_VISIBLE_DEVICES_X`) + a `scripts/dev/run_worker_X.sh` script + a dashboard proxy endpoint (`/api/stream_X.mjpeg`, `/api/snapshot_X.jpg`) + a button in the camera-toggle UI |
| Add a new classifier backend | Mirror `services/worker/app/classifier_v2.py`'s constructor + `process_frame` signature; add a branch in `services/worker/app/inference_proc.py`'s `backend == "..."` dispatch |
| Change the smoother behaviour | `services/worker/app/tracker.py` |
| Add a new admin-only page | Drop a template under `services/dashboard/app/templates/`, add `GET /admin/<name>.html` + any companion API endpoints (mutation ones gated on `Depends(require_role("admin"))` from `services/dashboard/app/auth.py` — see `routes_admin_users.py` for the pattern: `require_csrf` too, and a `user_audit` row for anything that changes state) |
