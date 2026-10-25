# Wahoo Bay · Real-time Fish ID

Real-time fish detection and species identification for the underwater
cameras at Wahoo Bay (Pompano Beach, FL). A YOLOv26 object detector
finds fish in each camera frame; a DINOv2 ViT-S/14 classifier
(trained from random init on Wahoo Bay community footage + iNaturalist
supplements) names the species. Everything is persisted to Postgres
and surfaced through a FastAPI dashboard that doubles as a reviewer
queue — humans correct mis-identifications and those corrections feed
the next training cycle.

Currently runs 24/7 on a single NVIDIA H200 (`/raid/scratch/dzimmerman2021/wahoobay`
on FAU's lab DGX). Public access is via an ephemeral Cloudflare quick
tunnel; per lab policy this machine cannot host a permanent public
endpoint, so the cloud migration is on the roadmap.

---

## Table of contents

- [What this project does](#what-this-project-does)
- [System diagram](#system-diagram)
- [Repository layout](#repository-layout)
- [Quick start](#quick-start)
- [What's currently running](#whats-currently-running)
- [Component map](#component-map)
- [Operating glossary](#operating-glossary)
- [Documentation](#documentation)
- [License + attribution](#license--attribution)
- [Citation](#citation)

---

## What this project does

End-to-end, in one paragraph: two underwater RTSP cameras stream into
worker processes running on the DGX's GPU. Every frame goes through a
YOLO object detector. Each detected fish box gets cropped, classified
into one of 223 species by a from-scratch DINOv2 classifier, smoothed
across consecutive frames by a constant-velocity tracker, and persisted
to Postgres. The dashboard reads from Postgres, proxies the worker's
annotated MJPEG stream for the live view, and exposes a reviewer UI for
humans to correct anything that looks wrong. Corrections accumulate in
their own table and are the seed corpus for the next training run.

**Six things this repo is also doing along the way:**

| Concern | How it's handled |
|---|---|
| Stationary reef geometry (the SEAHIVE "holes") producing false-positive boxes | Per-camera polygon mask (`data/camera_negative_regions.json`); drop detections whose bbox-IoU with a polygon clears `min_iou`. Configurable via the admin UI at `/admin/regions.html`. |
| Water-quality + weather sensor ingest | A separate poller pulls from the Wahoo Bay public API (sonde + WXT-536 weather), with fallback chains documented in [`docs/data_pipeline.md`](docs/data_pipeline.md). |
| Named recurring animals (Bobby the Blue Tang, Marty the Black Margate) | "Celebrities" feature: parsed from the Google Sheet, surfaced as a Reports tab, reviewers tag detections during the correction flow. |
| Source-of-truth species list maintained outside the codebase | Daily polling of the Wahoo Bay Google Sheet refreshes `assets/species.xlsx` + downstream JSON catalogues + hot-reloads the dashboard. |
| Reviewer ROI quality | Modal lets reviewers redraw the bbox, switch ROI/full-frame views, pick from top-K alternatives, and tag celebrities — all of which feed back into the training set. |
| Backend reversibility | Both v2 (DINOv2) and legacy Fishial classifiers are baked into the worker image; `CLASSIFIER_BACKEND=fishial` rolls back without a rebuild. |

---

## System diagram

```
                                                                       ┌───────────────────────────────────────────┐
                                                                       │  Reviewer queue (humans correct labels)   │
                                                                       │  · ROI lightbox + bbox editor             │
                                                                       │  · Top-K species autocomplete             │
                                                                       │  · Tag-as-celebrity dropdown              │
                                                                       └───────────────────┬───────────────────────┘
                                                                                           │
                                                                                           ▼
   ┌─────────────────┐                                                  ┌──────────────────────────────────────────┐
   │ SEAHIVECAM      │──RTSP──┐                                         │ FastAPI dashboard  (services/dashboard/) │
   │ (Axis PTZ in    │        │                                         │  · live MJPEG proxy                      │
   │ VITB Octopus)   │        │                                         │  · /api/events, /api/species_counts, ... │
   └─────────────────┘        │                                         │  · CSV / Parquet exports                 │
                              ▼                                         │  · Reports tabs (Overview, Fish Explorer,│
                ┌─────────────────────────────────────┐                  │    Celebrities, Water, Weather,         │
                │ Worker process (parent)             │                  │    Insights, Data Notes)                │
                │  · FrameTap thread reads frames     │                  │  · /admin/regions.html, /admin/...      │
                │  · Raw MJPEG + raw snapshot         │                  └────────────┬─────────────────────────────┘
                │  · Pickle-free shm hand-off ───────┐│                               │
                └─────────────────┬──────────────────┘│                               ▼
                                  │                   │              ┌──────────────────────────────────────────┐
                                  ▼                   │              │ Postgres                                  │
                ┌─────────────────────────────────────┘              │  · detection_events                      │
                │ Inference subprocess                                │  · detection_corrections                 │
                │  · YOLO detector (v26 nano)                         │  · saved_frames                          │
                │  · Negative-regions filter (IoU)                    │  · frame_stats                           │
                │  · v2 classifier (DINOv2 ViT-S/14)                  │  · sensor_readings, weather_readings     │
                │      OR Fishial legacy (one env flag)               │  · alerts, ptz_states                    │
                │  · Velocity smoother + track IDs                    │  · species_sightings_mat (refreshed 60s) │
                │  · Persistence (events, frames, stats)              └────────────┬─────────────────────────────┘
                │  · Annotate + JPEG encode → parent                               │
                └─────────────────────────────────────┘                            │
                                                                                   ▲
                                                                                   │
   ┌─────────────────┐                              ┌──────────────────────────────┴──────────┐
   │ Pier cam        │──RTSP──── (second worker)    │ SenseStream poller (water + weather)    │
   │ (Axis PTZ,      │                              │  · Wahoo Bay API (primary)              │
   │  CoP cam)       │                              │  · SenseStream direct (fallback 1)      │
   └─────────────────┘                              │  · Synthetic data (fallback 2, banner)  │
                                                    │  · Open-Meteo + NWS METAR (forecast)    │
   ┌─────────────────┐                              └─────────────────────────────────────────┘
   │ YSI EXO2 sonde  │
   │ (via Wahoo Bay  │
   │ API → poller)   │
   └─────────────────┘
```

[`docs/architecture.md`](docs/architecture.md) breaks each box down.

---

## Repository layout

```
.
├── README.md                       ← you are here
├── PLAN.md                         original week-1 plan (some details have drifted; defer to docs/)
├── .env.example                    every env var the system reads, with comments
├── docker-compose.yml              postgres + worker + dashboard + poller (one-command bring-up)
│
├── services/
│   ├── worker/                     GPU inference loop (one process per camera; runs on the GPU host)
│   │   ├── app/
│   │   │   ├── main.py             FastAPI + lifecycle; spawns inference subprocess + frame_tap
│   │   │   ├── pipeline.py         FrameTap + InferenceClient + shared-memory frame pool wiring
│   │   │   ├── inference_proc.py   Subprocess entry point: detector + classifier + smoother + persistence + JPEG encode
│   │   │   ├── frame_shm.py        Zero-copy frame hand-off (/dev/shm pool) parent ↔ subprocess
│   │   │   ├── pipeline_types.py   Shared FishDetection / Prediction dataclasses
│   │   │   ├── classifier_v2.py    DINOv2 ViT-S/14 classifier (the default backend)
│   │   │   ├── fishial.py          Legacy 866-class Fishial classifier (revert path; CLASSIFIER_BACKEND=fishial)
│   │   │   ├── negative_regions.py Per-camera polygon mask (drops detector false-positives)
│   │   │   ├── tracker.py          Constant-velocity smoother + track ID assignment
│   │   │   ├── overlay.py          Bbox drawing for the annotated MJPEG stream
│   │   │   ├── persistence.py      ImageSaver, EventLog (JSONL), PgWriter
│   │   │   ├── provenance.py       SHA256 per checkpoint + canonical config hash → stamped on every row
│   │   │   ├── sources.py          RTSPSource / PlaylistSource / AutoswitchSource
│   │   │   ├── ptz.py              VAPIX/ONVIF pan-tilt-zoom poller (currently off)
│   │   │   └── config.py           All env-var defaults; single source of truth
│   │   ├── Dockerfile              torch 2.4 + CUDA 12.4 + ultralytics + timm
│   │   └── requirements.txt
│   │
│   ├── dashboard/                  FastAPI web UI + REST API + reviewer queue
│   │   ├── app/
│   │   │   ├── main.py             All HTTP endpoints (≈ 2k lines)
│   │   │   ├── common_names.py     Latin→common-name lookup (curated dict + manifest fallback)
│   │   │   ├── exports.py          CSV/Parquet streamers (events, water, weather, sightings, ...)
│   │   │   ├── alerts.py           SLO rule evaluator (latency, ingest gap, etc.)
│   │   │   ├── matviews.py         Background refresher for species_sightings_mat
│   │   │   ├── stats.py            Wilson confidence intervals etc. for /api/species_counts_ci
│   │   │   ├── sheet_poller.py     Daily Google Sheet refresh → rebuild manifests → hot-reload
│   │   │   ├── templates/          index.html + regions_admin.html (Jinja2)
│   │   │   ├── static/             app.js (≈ 3k lines) + style.css
│   │   │   └── data/               README.md served alongside CSV downloads
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   │
│   └── sensestream_poller/         Pulls water + weather, writes to Postgres
│       └── app/main.py
│
├── scripts/                        Data + training scripts; not run in production
│   ├── build_species_manifest.py        species.xlsx → species_manifest.{json,csv}
│   ├── build_celebrities.py             species.xlsx → celebrities.json
│   ├── build_finetune_dataset_from_clips.py  Drive clips + iNat photos → cropped training set
│   ├── download_species_clips.py        Drive bulk download w/ virus-scan handling
│   ├── download_inat_photos.py          iNaturalist API harvest (licence-filtered)
│   ├── enrich_taxonomy_from_worms.py    WoRMS API → taxonomy.csv (AphiaID, accepted name, ranks; keyed by raw_name)
│   ├── train_baseline.py                DINOv2 ViT-S/14 from-scratch trainer (3 split modes)
│   ├── gen_synthetic_sensor_data.py     Diurnal-pattern synthetic sonde for offline demos
│   └── dev/                             start_postgres.sh, run_worker.sh, run_worker_pier.sh,
│                                        run_dashboard.sh, run_poller.sh, stop_postgres.sh
│
├── db/                             Alembic: alembic.ini + migrations/versions/ (0001_baseline.sql = all tables, indexes, matviews)
│
├── data/                           Gitignored or partially gitignored — see .gitignore
│   ├── models/
│   │   ├── detector_v26/                YOLO v26 nano (Fishial.ai)
│   │   ├── classifier_v0_10_2/          Legacy Fishial classifier (revert path)
│   │   └── classifier_v2/               DINOv2 ViT-S/14 (default)
│   ├── camera_metadata.json             lat/lng/depth/heading per camera (committed)
│   ├── camera_negative_regions.json     per-camera no-fish-zone polygons (committed; edit via /admin/regions.html)
│   ├── test_videos/                     fallback playlist (gitignored)
│   └── baseline_runs/                   train_baseline output dirs
│
├── assets/                         Source-of-truth catalogues + training data
│   ├── species.xlsx                     Wahoo Bay Google Sheet snapshot (auto-refreshed daily)
│   ├── species_manifest.{json,csv}      Derived: per-species rows from the sheet (gitignored)
│   ├── celebrities.json                 Derived: 25 named recurring animals (committed)
│   ├── taxonomy.csv                     WoRMS ranks per species (committed)
│   ├── inat_manifest.json               iNat photos w/ attribution + licence (committed)
│   ├── inat_photos/<binomial>/*.jpg     Downloaded iNat photos (gitignored; large)
│   ├── community_clips/                 Cropped fish from Drive (gitignored)
│   └── LICENSES_AND_CREDITS.md          Source-by-source licence picture
│
├── eval/                           Frozen eval harness (placeholder; populated as labelled clips land)
├── docs/                           Long-form docs — see Documentation section below
└── logs/app/                       Runtime logs (gitignored)
```

---

## Quick start

### A. Existing DGX install

The system is already running. To check or restart, follow
[`docs/operations.md`](docs/operations.md). Briefly:

```bash
# Confirm services are listening
ss -ltn | grep -E ':8081|:8083|:8082|:18080|:5432'
# Workers (8081 = SEAHIVECAM, 8083 = pier), poller (8082), dashboard (18080), postgres (5432)

# Tail the dashboard log
tail -f logs/app/dashboard.log
```

The dashboard is at `http://127.0.0.1:18080`. From a laptop:

```bash
ssh -L 18080:localhost:18080 <dgx-host>
# then http://localhost:18080 in a browser on the laptop
```

The Cloudflare quick tunnel URL is ephemeral (changes on every
`cloudflared` restart, including DGX reboots). Find the current URL:

```bash
grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' logs/app/cloudflared.log | head -1
```

### B. Fresh machine (Docker Compose)

The fastest path on a machine with a GPU and Docker + NVIDIA Container
Toolkit installed:

```bash
./scripts/fetch_models.sh          # first time only — pulls detector + legacy classifier weights
./scripts/fetch_test_videos.sh     # first time only — pulls 18 Wahoo Bay clips for the fallback playlist
cp .env.example .env
# Edit .env: set the camera RTSP URLs, GPU pinning (CUDA_VISIBLE_DEVICES),
# and any optional knobs (DASHBOARD_MAIL_BACKEND, SHEET_URL, etc.)
docker compose up --build
```

Dashboard at <http://localhost:18080>.

### C. Fresh machine (conda, no Docker — this is how the DGX runs)

```bash
# One-time env setup (only if the wahoobay env doesn't already exist):
conda create -n wahoobay python=3.11
conda activate wahoobay
pip install -r services/worker/requirements.txt
pip install -r services/dashboard/requirements.txt
pip install -r services/sensestream_poller/requirements.txt

# Make sure assets/species.xlsx + data/models/* are populated (see Layout).

# Start services
./scripts/dev/start_postgres.sh                                              # local pgdata cluster on :5432
nohup ./scripts/dev/run_worker.sh      > logs/app/worker.log      2>&1 &     # SEAHIVECAM on :8081
nohup ./scripts/dev/run_worker_pier.sh > logs/app/worker_pier.log 2>&1 &     # pier cam   on :8083
nohup ./scripts/dev/run_dashboard.sh   > logs/app/dashboard.log   2>&1 &     # dashboard on :18080
nohup ./scripts/dev/run_poller.sh      > logs/app/poller.log      2>&1 &     # sensestream poller on :8082
```

First-boot caveats:

- The v2 classifier does a `torch.compile(mode="reduce-overhead")` warmup
  at subprocess startup; expect ~25-30 s before the worker is ready.
- The MJPEG stream may show "fallback playlist" until the RTSP source
  connects (~3-5 s after worker ready).
- The matview `species_sightings_mat` is empty until the dashboard's
  refresher fires at its `MATVIEW_REFRESH_INTERVAL_S` cadence (60 s
  default).

---

## What's currently running

Authoritative current state lives in [`docs/operations.md`](docs/operations.md).
As of the last doc refresh:

| Service | Port | Role |
|---|---:|---|
| worker (SEAHIVECAM) | `127.0.0.1:8081` | Primary cam = Axis PTZ inside the VITB Octopus housing |
| worker (pier cam)   | `127.0.0.1:8083` | Secondary cam = City of Pompano Beach pier-mounted Axis |
| sensestream poller  | `127.0.0.1:8082` | Pulls water + weather; running live (Wahoo Bay API + Open-Meteo) |
| dashboard           | `127.0.0.1:18080` | FastAPI + reviewer queue + admin endpoints |
| postgres            | `127.0.0.1:5432` | Local cluster under `./pgdata/` |
| cloudflared         | (random `*.trycloudflare.com`) | Public-tunnel proxy in front of the dashboard |

Hardware: NVIDIA H200 on FAU's lab DGX. GPU pinning lives in `.env`
(`CUDA_VISIBLE_DEVICES=4` for SEAHIVECAM, `CUDA_VISIBLE_DEVICES_PIER=5`
for pier — the workers use separate GPUs so neither's compile / inference
contends with the other).

---

## Component map

Pointers from "I want to change X" to "edit Y." Keep this table fresh
as the system evolves.

| To change... | Touch... |
|---|---|
| Which classifier the worker runs | `CLASSIFIER_BACKEND` env (default `v2`; set to `fishial` for the legacy rollback) |
| The v2 classifier's checkpoint or class set | Drop new files into `data/models/classifier_v2/` and restart the worker |
| Detector confidence floor / NMS threshold | `DET_CONF_THRESHOLD`, `DET_IOU_THRESHOLD` in `.env` (see [`docs/operations.md`](docs/operations.md)) |
| When the classifier runs vs is skipped | `MIN_CLASSIFY_DET_CONF` (default 0.25 — runs on every detector emit) |
| When a softmax result is "unknown" | `MIN_ACCEPT_ACCURACY` (default 0.55 — top-1 below this → topk=[]) |
| Camera resolution / Axis compression / JPEG quality | `VIDEO_SOURCE` URL (`resolution=`, `compression=`), `JPEG_QUALITY` |
| Inter-process frame buffer count | `SHM_NUM_SLOTS` in `services/worker/app/frame_shm.py` (default 3) |
| What's in the reviewer queue | `services/dashboard/app/main.py` `/api/events` SQL (filters at the source — already excludes reviewed events by default) |
| Adding a CSV / Parquet export | Add a generator in `services/dashboard/app/exports.py`, an endpoint in `main.py`, and a button in `templates/index.html` |
| The species list / common-name catalogue | Edit the Google Sheet (sheet poller refreshes daily; `POST /api/admin/refresh_sheets` forces it immediately) |
| The celebrities list | Edit the "Fish named by Chatters" tab of the same sheet; same refresh path |
| Per-camera no-fish-zone polygons | `/admin/regions.html` in the dashboard (or hand-edit `data/camera_negative_regions.json`); restart worker |
| Curated Latin→common-name overrides | `services/dashboard/app/common_names.py` `COMMON_NAMES` dict (no restart hot-reload yet for the curated layer) |
| Reviewer flow / correction modal | `services/dashboard/app/static/app.js` (`correctionFormHtml`, `submitCorrection`, `_enterBboxEdit`) |
| Postgres schema | Alembic revisions in `db/migrations/versions/`: `alembic -c db/alembic.ini upgrade head` (see docs/operations.md, "Applying a schema change to the live database") |

---

## Operating glossary

Terms that appear all over the codebase + docs:

- **SEAHIVECAM** — the camera at Wahoo Bay's SEAHIVE artificial reef
  (Axis PTZ inside a View Into The Blue "Octopus" housing). Source label
  in the database = `seahivecam`.
- **Pier cam** — the City of Pompano Beach pier-mounted Axis camera.
  Source label = `pier_cam`.
- **v2 classifier** — our from-scratch DINOv2 ViT-S/14 on 223 species.
  `data/models/classifier_v2/`. Default backend.
- **Fishial / Fishial v0.10.2** — the legacy 866-class community
  classifier we used before v2. `data/models/classifier_v0_10_2/`. Still
  bundled as the one-env-var rollback path.
- **Reviewer / Reviewer queue** — the human-in-the-loop correction
  flow. Events show up in the dashboard's Recent Events list and the
  ROI modal; reviewers endorse / correct / mark "not a fish" / redraw
  bbox / tag a celebrity. Corrections land in `detection_corrections`.
- **Review tool** — `/review` — the reviewer annotation tool (reviewer
  or admin role): a queue of boxes to verify, whole-frame mode, and
  browse. See docs/operations.md, "Review tool".
- **Celebrity** — a named recurring animal from the "Fish named by
  Chatters" sheet (Bobby, Marty, Stumpy, …). Optional `celebrity_id`
  field on each correction.
- **Smoother** — the constant-velocity track-association layer in
  `tracker.py`. Smooths the live overlay; the **raw** detections (not
  the smoothed ones) are what get persisted.
- **Autoswitch** — when the primary camera goes dark (rolling-window
  brightness crosses the threshold), the worker falls over to the
  fallback playlist of test videos. **No data is collected** during
  fallback — the banner on the dashboard tells viewers what's happening.
- **Negative region** — a per-camera polygon where detector
  false-positives reliably appear (the SEAHIVE holes are the canonical
  case). Detections whose bbox-IoU with the polygon clears `min_iou`
  get dropped before the classifier runs.
- **Provenance** — the four columns (`model_version`,
  `detector_sha256`, `classifier_sha256`, `config_hash`,
  `pipeline_git_sha`) stamped on every detection row. Given any row you
  can reconstruct which weights + which code + which config produced it.
- **Matview** — Postgres materialised views (`species_sightings_mat`,
  `species_sightings_hourly_mat`) refreshed by a dashboard background
  task. They back the user-facing aggregate endpoints; refreshing them
  costs a fraction of a second so we do it every 60 s.

---

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — system overview,
  components, data flow, smoother + autoswitch + provenance design,
  shared-memory frame hand-off, backend abstraction (v2 vs fishial).
- [`docs/operations.md`](docs/operations.md) — runbook: starting and
  stopping services, common failures, log locations, the public-tunnel
  lifecycle, every env var grouped by concern.
- [`docs/data_pipeline.md`](docs/data_pipeline.md) — exactly what gets
  stored where (every table, every JSONL log, every disk artifact), the
  autoswitch fallback isolation rules, retention guidance.
- [`docs/finetune_workflow.md`](docs/finetune_workflow.md) — end-to-end
  playbook from the Wahoo Bay spreadsheet to a fine-tuned v2 classifier
  checkpoint going live, including the iNat supplement + WoRMS taxonomy
  + the three split-mode honest-eval strategies.
- [`docs/reviewer_guide.md`](docs/reviewer_guide.md) — non-technical
  guide for the humans submitting corrections: the ROI modal, top-K
  alternatives, bbox redraw, "not a fish," celebrity tagging.
- [`assets/LICENSES_AND_CREDITS.md`](assets/LICENSES_AND_CREDITS.md) —
  source-by-source dataset provenance, licence breakdown, attribution
  obligations, and what the trained v2 checkpoint may / may not be used
  for. Read before any public release or commercial use of the model.
- [`PLAN.md`](PLAN.md) — original architecture / deployment plan from
  the first week of the project. Some details have evolved (notably the
  classifier swap and shared-memory frame hop); defer to the `docs/`
  files for the current state.

---

## License + attribution

- **This project's source code:** MIT.
- **Detector weights** (`data/models/detector_v26`): Fishial.ai, MIT —
  see the `info.json` next to the checkpoint.
- **Legacy classifier weights** (`data/models/classifier_v0_10_2`):
  Fishial.ai, MIT — available as the `CLASSIFIER_BACKEND=fishial`
  fallback.
- **v2 classifier weights** (`data/models/classifier_v2`): this project,
  MIT for the architecture and training code, but the trained
  parameters inherit non-commercial / no-derivatives constraints from
  a subset of the training data. See
  [`assets/LICENSES_AND_CREDITS.md`](assets/LICENSES_AND_CREDITS.md)
  for the exact picture and what's required before publication or
  commercial release.
- **Training data sources:** Wahoo Bay community Drive contributors
  (internal-use grant), [iNaturalist](https://www.inaturalist.org/)
  (mixed CC licences — see credits file), and the
  [WoRMS](https://www.marinespecies.org/) editorial board for taxonomy.
- **Hardware references:** VITB Octopus camera (SEAHIVECAM hardware) —
  View Into The Blue. SpotAI is the camera-management platform Wahoo
  Bay uses; this project bypasses SpotAI and connects to the cameras
  directly.

---

## Citation

If you use this data or code in a publication:

> [1] D. Zimmerman, "Wahoo Bay SEAHIVE Artificial Reef dataset,"
> Shipwreck Park, Pompano Beach, FL, USA; FAU Center for Connected
> Autonomy & AI, Florida Atlantic University, Boca Raton, FL, USA,
> 2026. [Online]. Available: https://wahoobay.org

Updates and corrections: dzimmerman2021@fau.edu.
