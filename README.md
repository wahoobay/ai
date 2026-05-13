# Wahoo Bay · Real-time Fish ID

Real-time fish detection and species identification for the underwater
cameras at Wahoo Bay (Pompano Beach, FL). Uses the open-source
[Fishial.ai](https://github.com/fishial/fish-identification) detector and
classifier (MIT-licensed) on a single NVIDIA H200, exposes a live web
dashboard, and persists every detection to Postgres for ecological
analysis and model fine-tuning.

Currently running 24/7 on the lab DGX with a public demo URL via a
Cloudflare quick tunnel. The architecture is designed to lift-and-shift
to AWS once that account access lands. See
[`docs/operations.md`](docs/operations.md) for the live status and how
to manage the running services.

```
┌───────────────────┐    ┌──────────────────────┐    ┌─────────────────────┐
│ underwater camera │───▶│ worker (this repo)   │───▶│ Postgres / disk     │
│ (Axis / VITB)     │    │ • YOLOv26 detector   │    │ • detection_events  │
│ on Wahoo Bay      │    │ • DINOv2+ViT class.  │    │ • frame_stats       │
│ pier              │    │ • velocity tracker   │    │ • saved_frames      │
└───────────────────┘    │ • autoswitch to     │    │ • sensor_readings   │
                         │   playlist if dark   │    │ • detection_corr.   │
┌───────────────────┐    └──────────┬───────────┘    └─────────┬───────────┘
│ YSI EXO2 sonde    │               │                          │
│ (via SenseStream) │───────────────┘                          │
└───────────────────┘                                          │
                                                                │
                         ┌──────────────────────┐              │
                         │ FastAPI dashboard    │◀─────────────┘
                         │ • live MJPEG overlay │
                         │ • reef explorer      │
                         │ • CSV / Parquet      │
                         │   exports            │
                         │ • corrections UI     │
                         └──────────┬───────────┘
                                    │
                                    ▼
                         ┌──────────────────────┐
                         │ public demo URL      │
                         │ (Cloudflare tunnel)  │
                         └──────────────────────┘
```

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — system overview, components,
  data flow, smoother + autoswitch + provenance design.
- [`docs/operations.md`](docs/operations.md) — runbook: starting and stopping
  services, common failures, log locations, the public-tunnel lifecycle.
- [`docs/data_pipeline.md`](docs/data_pipeline.md) — exactly what gets stored
  where, the autoswitch fallback isolation rules, retention guidance.
- [`docs/finetune_workflow.md`](docs/finetune_workflow.md) — end-to-end
  playbook from a human reviewer's first correction to a fine-tuned
  classifier checkpoint going live.
- [`docs/reviewer_guide.md`](docs/reviewer_guide.md) — non-technical guide
  for the humans submitting corrections.
- [`PLAN.md`](PLAN.md) — original architecture/deployment plan (week 1).
  Some details have evolved since; defer to the `docs/` files for the
  current state.

## Quick start

### A. Existing DGX install

The services are already running. To check or restart, follow
[`docs/operations.md`](docs/operations.md). The dashboard is at
`http://127.0.0.1:18080`; reach it from a laptop via SSH tunnel
(`ssh -L 18080:localhost:18080 dgx1`) or via the Cloudflare tunnel URL
(see operations doc — URL is ephemeral).

### B. Fresh machine (Docker Compose)

```bash
./scripts/fetch_models.sh          # first time only — pulls Fishial weights
./scripts/fetch_test_videos.sh     # first time only — pulls 18 Wahoo Bay clips
cp .env.example .env
docker compose up --build
```

Dashboard at <http://localhost:18080>.

### C. Fresh machine (conda, no Docker)

```bash
conda activate wahoobay
./scripts/dev/start_postgres.sh
./scripts/dev/run_worker.sh    > logs/app/worker.log    2>&1 &
DASHBOARD_PORT=18080 ./scripts/dev/run_dashboard.sh > logs/app/dashboard.log 2>&1 &
./scripts/dev/run_poller.sh    > logs/app/poller.log    2>&1 &
```

## Layout

```
services/worker/         GPU inference: video → detector → classifier → DB/log/images → MJPEG
services/dashboard/      FastAPI web UI + REST API + CSV/Parquet exports + reef explorer
services/sensestream_poller/  Pulls water-quality readings from sensestream.org

scripts/                 fetch_models.sh, fetch_test_videos.sh, gen_synthetic_sensor_data.py,
                         build_finetuning_dataset.py, dev/{start_postgres,run_worker,...}.sh

eval/                    Frozen-eval harness: run.py, metrics.py, manifest.json
                         (waiting on labeled clip set to populate)

db/init.sql              All tables, views, indexes — idempotent / safe to re-run
data/                    test_videos/ (gitignored), models/ (gitignored),
                         camera_metadata.json (lat/lng/depth/heading per source)

docs/                    See above
docker-compose.yml       postgres + worker + dashboard + sensestream_poller
.env.example             Every env var with comments
```

## What's currently running

See `docs/operations.md` for the authoritative current state. As of the
last documentation update:

- worker on `127.0.0.1:8081` — primary source = pier cam (HTTPS MJPEG),
  fallback = YouTube playlist; auto-switches on rolling brightness.
- dashboard on `127.0.0.1:18080` — public via Cloudflare quick tunnel.
- sensestream poller on `127.0.0.1:8082` — stub mode (waiting for token).
- postgres on `127.0.0.1:5432` — local cluster under `./pgdata`.

## License + attribution

- This project: MIT (matching Fishial's licensing).
- Detector + classifier weights: Fishial.ai (MIT) — see
  `data/models/{detector_v26,classifier_v0_10_2}/info.json`.
- VITB Octopus camera (SEAHIVECAM hardware): View Into The Blue.
- SpotAI is the camera-management platform Wahoo Bay uses; this project
  bypasses SpotAI and connects to the cameras directly.

## Citation

If you use this data or code in a publication:

> [1] D. Zimmerman, "Wahoo Bay SEAHIVE Artificial Reef dataset,"
> Shipwreck Park, Pompano Beach, FL, USA; FAU Center for Connected
> Autonomy & AI, Florida Atlantic University, Boca Raton, FL, USA,
> 2026. [Online]. Available: https://wahoobay.org

Updates and corrections: dzimmerman2021@fau.edu.
