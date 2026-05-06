# Wahoo Bay Real-Time Fish ID — Plan

## Goal
Run 24/7 real-time fish identification on a single RTSP camera stream at Wahoo Bay. Public-facing eventually.

## Compute
- Host: shared DGX with 8× NVIDIA H200 (144 GB each), bare-metal, no SLURM, no MIG.
- Allocation strategy: pin to one GPU via `CUDA_VISIBLE_DEVICES`, auto-select the least-busy at startup. Other 7 GPUs untouched.
- Expected footprint (single 30 FPS stream, detector + classifier, no segmentation):
  - ~10–15% SM util on one GPU
  - ~1.5–2 GB VRAM of 144 GB
  - ~1 CPU core (RTSP ingest + glue)
  - ~2–3 GB system RAM
  - ~+120 W power over idle
- H200 is a datacenter SKU rated for sustained 24/7/365 duty; no hardware-lifetime concern.
- Authorized use (machine owned by advisor).

## Models (Fishial open-source, MIT license)
All TorchScript, hosted at `storage.googleapis.com/fishial-ml-resources/`.

- **Detector**: YOLOv26-nano — `detector_v26_n3.zip`
- **Classifier**: DINOv2-224 + ViT pooling-3 head, sub-center ArcFace, 866 classes, 768-d embedding — `classification_model_v0.10.2.zip` (93.22% top-1 on Fishial eval)
- **Labels**: `labels.json` from the repo
- **Skip segmentation** — not needed for species + bbox.

Pipeline per frame: RTSP → NVDEC decode → YOLOv26 detect → crop each fish → batched DINOv2+ViT embed → nearest-centroid lookup against 866-class DB tensor → top-K species with similarity scores.

Optimizations: BF16 autocast, `torch.compile` on the ViT stage, batch all crops from a frame.

## Architecture

```
┌─────────────┐    ┌────────────────┐    ┌──────────────┐
│ RTSP camera │───▶│ FFmpeg NVDEC   │───▶│ Inference    │
└─────────────┘    │ (H.264 → CUDA) │    │ worker       │
                   └────────────────┘    │ (YOLOv26 +   │
                                         │  DINOv2+ViT) │
                                         └──────┬───────┘
                                                │
                    ┌───────────────────────────┼───────────────────────────┐
                    ▼                           ▼                           ▼
             ┌────────────┐            ┌────────────────┐          ┌───────────────┐
             │ Postgres   │            │ FastAPI +      │          │ Disk          │
             │ (events +  │            │ WebSocket      │          │ (periodic     │
             │  timeseries)│           │ dashboard      │          │  JPEGs + COCO │
             └────────────┘            │ (live overlay) │          │  annotations) │
                                       └────────────────┘          └───────────────┘
```

## Component choices

### Ingest
FFmpeg with `hwaccel cuda` (NVDEC) via PyAV or `torchcodec`. Keeps CPU near zero. Assumes H.264; adjust for H.265/MJPEG when camera is confirmed.

### Inference worker
Python + PyTorch. TorchScript models loaded on one GPU selected by least-busy probe at startup. BF16 autocast. Batches all detected fish per frame through the classifier. SIGTERM handler flushes queues and exits within 5 s.

### Dashboard
FastAPI backend + minimal HTML/JS frontend. WebSocket pushes annotated JPEG frames (draw bbox + species + confidence overlay). REST endpoints for recent events, species counts, histograms. Starts localhost-only; harden for public later with:
- Reverse proxy (nginx or Caddy) + TLS
- Auth (OAuth via institution SSO, or simple email+password with bcrypt via `fastapi-users`)
- Rate limiting on live-stream endpoint
- Readonly public view separate from admin view

### Event log (JSON)
Append-only JSONL at `/raid/scratch/dzimmerman2021/wahoobay/logs/events/`. One line per detection with timestamp, species UUID + name, confidence, bbox, frame-id. Rotated daily via `logrotate` or built-in rotation.

### Database (Postgres)
Schema sketch:
```sql
CREATE TABLE detection_events (
  id           BIGSERIAL PRIMARY KEY,
  ts           TIMESTAMPTZ NOT NULL,
  frame_id     BIGINT NOT NULL,
  species_id   TEXT NOT NULL,
  species_sci  TEXT NOT NULL,
  species_eng  TEXT,
  confidence   REAL NOT NULL,
  bbox         INT4[] NOT NULL,
  image_path   TEXT
);
CREATE INDEX ON detection_events (ts DESC);
CREATE INDEX ON detection_events (species_id, ts DESC);

CREATE TABLE species_rollup_hourly (
  hour         TIMESTAMPTZ NOT NULL,
  species_id   TEXT NOT NULL,
  count        INT NOT NULL,
  mean_conf    REAL NOT NULL,
  PRIMARY KEY (hour, species_id)
);
```
Optional TimescaleDB hypertable on `detection_events` if retention policies / continuous aggregates are wanted.

### Image saving (tunable)
Three independent knobs, all configurable:
1. **Timelapse**: every N seconds regardless of detections.
2. **Per-detection**: save the frame whenever any fish is detected.
3. **Interesting-only**: save when (a) new species seen today, (b) confidence > threshold, or (c) first detection after quiet period.

Saved to `/raid/scratch/dzimmerman2021/wahoobay/frames/YYYY/MM/DD/HH/<frame-id>.jpg` with a sibling `.json` in **COCO format** (single-image dataset fragment per annotation file; a daily merge job concatenates into a proper COCO dataset).

## Deployment

Dockerized from day one. Same images run on the DGX for development and on AWS for production — no separate systemd / bare-metal path. Config via env vars (12-factor): `RTSP_URL`, `DATABASE_URL`, `GPU_INDEX`, `SAVE_CADENCE_S`, etc. `.env.example` committed; real `.env` gitignored.

### Phase 1 — Dockerized development (on DGX)
Three containers, defined in `docker-compose.yml`, run on the DGX via NVIDIA Container Toolkit. Dashboard localhost-only behind SSH tunnel.

### Phase 2 — AWS production (long-term home)
Same container images, promoted from ECR to AWS-native services (see diagram and table below).

### Containers

| Service | Base image | GPU | Notes |
|---|---|---|---|
| `inference-worker` | `nvidia/cuda:12.4-runtime-ubuntu22.04` + PyTorch | yes | Fishial TorchScript checkpoints baked into image at build (pulled from `storage.googleapis.com/fishial-ml-resources/`). ~2–3 GB image. |
| `dashboard` | `python:3.11-slim` + FastAPI | no | Serves WebSocket stream, REST API, static frontend. |
| `postgres` | `postgres:16` | no | Containerized for dev; managed (RDS) in prod. |

Both app services expose `/healthz` + `/readyz` for orchestrator probes.

### AWS production architecture

```
┌─────────────────┐
│ Wahoo Bay       │
│ RTSP camera     │
└────────┬────────┘
         │
         ▼
┌──────────────────────────┐
│ Kinesis Video Streams    │   durable ingest, handles reconnects
│ (KVS)                    │   avoids exposing camera to public net
└────────┬─────────────────┘
         │ consume via GStreamer plugin
         ▼
┌──────────────────────────┐      ┌──────────────────────────┐
│ EC2 g4dn.xlarge (T4)     │      │ ECR (container registry) │
│  • inference-worker      │◀─────│  images built by         │
│  • pulls from ECR        │      │  GitHub Actions on tag   │
└────┬──────────────┬──────┘      └──────────────────────────┘
     │              │
     ▼              ▼
┌─────────┐   ┌──────────────────────┐
│ S3      │   │ RDS Postgres         │
│ (frames │   │ (detection_events,   │
│  + COCO)│   │  rollups)            │
└─────────┘   └──────────────────────┘
                        ▲
                        │
┌──────────────────────────────────────┐
│ ECS Fargate: dashboard container     │
│  • reads from RDS + S3               │
│  • WebSocket bridge to worker        │
└────────────────┬─────────────────────┘
                 │
                 ▼
┌──────────────────────────────────────┐
│ Application Load Balancer + ACM TLS  │
│ Route53 DNS → public domain          │
│ Cognito for auth (public-viewer +    │
│  admin user pools)                   │
└──────────────────────────────────────┘
```

**AWS service map:**
| Concern | AWS service |
|---|---|
| Container registry | ECR |
| GPU inference host | EC2 g4dn.xlarge (NVIDIA T4, ~$380/mo on-demand, ~$120/mo spot) |
| Video ingest | Kinesis Video Streams (camera → KVS Producer SDK; worker consumes via GStreamer) |
| Dashboard hosting | ECS Fargate (CPU task, autoscales) |
| Database | RDS Postgres (db.t4g.small to start; with TimescaleDB extension if we add time-series rollups) |
| Object storage | S3 (frames + COCO annotation files; lifecycle rule → Glacier after 90 days) |
| Secrets | AWS Secrets Manager (DB creds, Fishial API keys if we ever use the hosted API) |
| Public entry | ALB + ACM (TLS) + Route53 |
| Auth | Cognito user pool (separate public-viewer and admin groups) |
| Monitoring | CloudWatch Logs + Metrics; alarms on GPU util, worker restarts, DB CPU |
| CI/CD | GitHub Actions: test → build → push to ECR → update ECS service |
| IaC | Terraform module in `infra/` — single-command deploy |

**Why Kinesis Video Streams for ingest:** RTSP from a field camera to a public cloud VM is fragile (camera needs public IP or VPN, reconnect logic is hand-rolled, no durable replay). KVS inverts it: camera pushes to AWS, the worker reads a stable AWS endpoint. Handles network drops, buffers, and gives us cheap retroactive replay of any frame. Adds ~$20–50/mo for a single 1080p/30 stream.

**Cost envelope (rough, single stream, on-demand):**
- g4dn.xlarge EC2: ~$380/mo (drop to ~$120/mo on Spot if we tolerate occasional restart)
- RDS db.t4g.small: ~$25/mo
- ECS Fargate dashboard: ~$15/mo
- KVS: ~$30/mo for 1 stream
- S3 + data transfer: ~$10–30/mo depending on frame-save cadence
- ALB + Route53 + Cognito: ~$20/mo
- **Total: ~$480/mo on-demand; ~$220/mo with Spot + reserved instances**

**Migration path from DGX to AWS:**
1. Push the same images built on DGX to ECR.
2. Spin up the AWS stack via Terraform, pointing at a test RTSP feed (or KVS-mirrored dev stream).
3. Validate parity with DGX outputs for ~1 week (same detections, same DB schema, same image outputs).
4. Cut camera ingest over to KVS, decommission DGX service (or keep as warm standby).

## Deployment-grade rollout

Phased build-out of the dev → CI → CD → production → MLOps story. Each
phase is a checkpoint we can stop at without breaking anything that
came before. Decisions agreed 2026-05-05:
- Long-term home: AWS; for now, keep everything on the DGX.
- Secrets: `.env` for now → GitHub Actions secrets in CI →
  AWS Secrets Manager / SSM Parameter Store at runtime.
- Container registry: GHCR.
- Public domain: `wahoobay.org` subdomain eventually, not yet.
- Kafka: deferred; revisit when scale or fan-out justifies it.

### Phase 1 — Containerise (in progress)

| File | Purpose |
|---|---|
| `services/worker/Dockerfile` | CUDA 12.4 runtime, models baked, GPU passthrough |
| `services/dashboard/Dockerfile` | python:3.11-slim, exposes 18080 |
| `services/sensestream_poller/Dockerfile` | python:3.11-slim, COPYs `scripts/gen_synthetic_sensor_data.py` for stub-mode synthetic |
| `services/*/requirements.txt` | Pinned to the DGX conda env's working set (2026-05-05) |
| `docker-compose.yml` | Postgres + worker + dashboard + poller, all bound to 127.0.0.1, GPU on worker via `deploy.resources.reservations.devices` |
| `.dockerignore` | Keeps `pgdata/`, `frames/`, `logs/`, secrets, model zips out of build context |
| `Makefile` | `make up / down / build / logs / psql / worker-logs / ...` |

**The DGX bare-metal `scripts/dev/run_*.sh` flow continues to work
unchanged** — Phase 1 is additive. Once the compose stack is verified,
PR review + CI work in Phase 2 can run against it.

**Phase 1 blocker (DGX-side, one-time):** the user account isn't in
the `docker` group. Fix:
```bash
sudo usermod -aG docker $USER
# log out + back in (or `newgrp docker`)
```
After that, `make up` works.

### Phase 2 — GitHub Actions CI

ruff / black / mypy / pytest matrix, multi-arch container build on every
PR (with cache), push to GHCR on merge to main, image vulnerability
scan via Trivy. Branch protection on `main`.

### Phase 3 — Prometheus + Grafana

`prometheus_client` instrumentation in worker / dashboard / poller,
`/metrics` endpoints, scrape config. Grafana dashboards for: worker
fps + GPU + inference latency, dashboard request rate / errors,
Postgres health, business metrics (sightings/hr, model accuracy from
corrections). Alertmanager replaces the current ad-hoc `alerts` table
SLO checker.

### Phase 4 — CD to staging

GitHub Actions deploy job, secrets via GitHub → AWS Secrets Manager,
blue/green or rolling deploy on a single GPU EC2 (worker) +
ECS Fargate (dashboard, poller, optional Postgres) with automatic
rollback on healthcheck failure.

### Phase 5 — Production

Same as staging plus persistent domain, TLS, durable Postgres
(RDS or self-hosted on EBS), S3-backed frame storage, retention policy,
on-call escalation.

### Phase 6 — MLOps pipeline

Reviewer-correction threshold trigger → fine-tune job → eval-set gate
(must beat current per-class P/R) → model registry → blue/green model
swap with provenance fingerprint.

### Phase 7 — Kafka (deferred)

Adds replayability and horizontal fan-out. Revisit when (a) we add
multiple cameras, (b) we want a real-time analytics path that doesn't
touch the OLTP DB, or (c) we want event replay for ML feature backfills.

## Inference performance backlog

After moving inference into a separate OS process (commit fdcf70a), the
detector + classifier subprocess is the throughput ceiling. With
SEAHIVECAM at 720p compression=90 we observe `frames_seen` ≈ 14.4 fps
and `frames_inferred` ≈ 4.8 fps. Speed-ups in priority order:

| # | Change | Speedup | Status | Notes |
|---|---|---|---|---|
| 1 | Cap detections / frame to top-K by det_conf | ~1.5–2× | deferred | sort detector output by `det_conf`, classify only top-K. Drops are likely-redundant detections in dense scenes. New env: `MAX_CLASSIFY_PER_FRAME` (default unset = no cap). |
| 2 | Skip classifier when `det_conf < threshold` | ~1.3× | done (this commit) | det_conf-low events still persist with empty top-K (UI shows "unknown species"). Default threshold 0.4, env: `MIN_CLASSIFY_DET_CONF`. |
| 3 | bf16 autocast on classifier forward | ~1.5–2× | done (this commit) | bfloat16 is preferred over fp16 on H200 (same throughput, better numerics). Env: `CLASSIFIER_AUTOCAST` (off/float16/bfloat16). |
| 4 | Image saves go async | ~1.2× (measured) | done | `ImageSaver.maybe_save` returns a `SaveJob` synchronously; a `save-worker` thread inside the inference subprocess does the JPEG encodes / COCO write / `record_saved_frame` so the inference loop never blocks on disk. Bounded queue with drop-oldest if the writer falls behind. |
| 5 | TensorRT engine for detector + classifier | ~2–3× on top | deferred | torch → ONNX → TRT, FP16 first, INT8 with calibration set. Probably worth doing right before the public URL goes wide. |
| 6 | Smaller backbone (DinoV2-Small → Tiny) | ~2–3× | deferred | Fishial doesn't ship a Tiny checkpoint; would need retraining or distillation. |
| 7 | Custom small classifier (MobileNetV3-S / EfficientNet-B0) on the **176 Wahoo Bay species** after fine-tuning | ~5–10× eventually | deferred until reviewer corrections accumulate | the right destination — 176 classes is small enough that a 2–5 M-param model handles it comfortably and is ≥10× faster than the 22 M-param DinoV2-S. |

## Blockers (what we're waiting on)
1. **Dataset** — Wahoo Bay-specific validation set or ground-truth species list to verify / fine-tune Fishial's 866-class model.
2. **Camera access** — RTSP URL, stream resolution, framerate, codec (H.264 vs H.265), day/night mode behavior.
3. **Public-deployment constraints** — which domain, TLS cert source, auth provider, access policy.
4. **AWS account & billing owner** — which AWS account hosts this, who pays, and whether Wahoo Bay or the lab manages the root/billing identity.

## Open questions
- Does Wahoo Bay have a preferred cloud for public hosting of the dashboard, or should it be served from the DGX through a reverse proxy?
- Do we need Spanish localization in the dashboard?
- Retention policy: how long to keep raw frames vs aggregated stats?
