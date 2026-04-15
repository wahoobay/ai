# Wahoo Bay · Real-time Fish ID

Real-time fish detection + species identification for a live underwater camera
at Wahoo Bay. Uses the open-source [Fishial.ai](https://github.com/fishial/fish-identification)
detector + classifier (MIT).

Full design: [PLAN.md](PLAN.md).

## What's here

```
services/worker/     inference worker: video → detector → classifier → DB/log/images → MJPEG
services/dashboard/  FastAPI web UI: proxies live stream, queries Postgres for history
db/init.sql          event schema
data/test_videos/    (gitignored) pulled by scripts/fetch_test_videos.sh
data/models/         (gitignored) pulled by scripts/fetch_models.sh
docker-compose.yml   postgres + worker + dashboard
scripts/dev/         run locally in a conda env without docker
```

## Two ways to run

### A. Docker Compose (prod-equivalent)

```bash
./scripts/fetch_models.sh          # first time only
./scripts/fetch_test_videos.sh     # first time only, requires yt-dlp
cp .env.example .env
docker compose up --build
```
Open [http://localhost:18080](http://localhost:18080) (SSH-tunnel if the host is remote:
`ssh -L 18080:localhost:18080 dgx1`).

### B. Conda (dev on the DGX without docker)

```bash
conda activate wahoobay
./scripts/dev/start_postgres.sh                 # boots a local cluster under ./pgdata
./scripts/dev/run_worker.sh    &                # GPU inference worker
./scripts/dev/run_dashboard.sh &                # web UI
```
Then open [http://localhost:18080](http://localhost:18080) (or SSH-tunnel).

## Config

Everything is env-var driven (12-factor). See [`.env.example`](.env.example) for the
full list. Key knobs:

| Var | Purpose |
|---|---|
| `VIDEO_SOURCE` | `file:///abs/dir` for a folder of mp4s, or `rtsp://...` |
| `DET_CONF_THRESHOLD` | drop detector boxes below this |
| `CLASSIFIER_METHOD` | `natural_centroid` (default), `arcface_centroid`, or `arcface_logits` |
| `SAVE_TIMELAPSE_SECONDS` | >0 to save every N seconds |
| `SAVE_PER_DETECTION` | `true` to save whenever any fish is seen |
| `SAVE_INTERESTING_ONLY` | `true` (default) for new-species / high-conf / after-quiet saves |
| `DATABASE_URL` | Postgres DSN |

Image saves land in `$FRAMES_DIR/YYYY/MM/DD/HH/` with a sibling `.coco.json`
annotation file per frame.

## Data model

- `detection_events` — one row per detected fish per frame (bbox, top-k species, confidence).
- `saved_frames` — one row per frame written to disk, with reason and paths.
- `species_counts_hourly` — view for hourly rollups.

## Deployment

Designed to run identically on the DGX (dev) and on AWS (long-term home):
EC2 g4dn.xlarge for the worker, ECS Fargate for the dashboard, RDS Postgres,
S3 for `frames/`, Kinesis Video Streams for the camera feed, Cognito for auth.
See [PLAN.md § Deployment](PLAN.md#deployment).
