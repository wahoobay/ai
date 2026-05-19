# Operations runbook

Everything you need to start, stop, recover, observe, and reconfigure
the live system on the DGX.

---

## Currently running (snapshot)

| Service | URL | Process model |
|---|---|---|
| Postgres | `127.0.0.1:5432` | Conda binary, cluster at `./pgdata`, daemonised by `pg_ctl` |
| Worker — SEAHIVECAM | `127.0.0.1:8081` | `python -m app.main` from `services/worker/`, primary cam, GPU-pinned via `CUDA_VISIBLE_DEVICES` |
| Worker — pier cam | `127.0.0.1:8083` | Same code, different env (`*_PIER` overrides). Runs on a separate GPU so neither cam contends with the other |
| Dashboard | `127.0.0.1:18080` | `python -m app.main` from `services/dashboard/` |
| SenseStream poller | `127.0.0.1:8082` | `python -m app.main` from `services/sensestream_poller/` |
| Cloudflare tunnel | random `*.trycloudflare.com` URL | `~/.local/bin/cloudflared tunnel --url http://localhost:18080` |

All five Python processes are launched via `scripts/dev/run_*.sh` from
disowned bash background jobs. They survive logout but **not a DGX
reboot** — see "Recovery from a DGX reboot" below.

---

## Quick health check

```bash
# from the DGX (or via SSH tunnel)
curl -s http://127.0.0.1:8081/healthz       # SEAHIVECAM worker
curl -s http://127.0.0.1:8083/healthz       # pier cam worker
curl -s http://127.0.0.1:8082/healthz       # sensestream poller
curl -s http://127.0.0.1:18080/healthz      # dashboard

# fuller picture (autoswitch state, fps, current source label)
curl -s http://127.0.0.1:8081/stats | python3 -m json.tool
curl -s http://127.0.0.1:8083/stats | python3 -m json.tool

# what model is running?
curl -s http://127.0.0.1:18080/api/provenance/current | python3 -m json.tool
```

The dashboard's banner across the top of the page lists any active SLO
alerts. If the banner is empty, all six health rules are satisfied.

---

## Starting from cold

Order matters: postgres → workers → dashboard → poller → tunnel.

All runtime knobs (RTSP URLs + creds, write token, GPU pin, autoswitch
thresholds, classifier backend, etc.) live in a single `.env` file at
the repo root. Each `scripts/dev/run_*.sh` auto-sources it, so
cold-start is just "run the script." If `.env` doesn't exist, copy
`.env.example` and fill in the secrets — the file is gitignored and
should be mode 600 (`chmod 600 .env`).

```bash
conda activate wahoobay
cd /raid/scratch/dzimmerman2021/wahoobay
mkdir -p logs/app

# 1. postgres
./scripts/dev/start_postgres.sh
# (idempotent: starts the cluster if not running, applies db/init.sql)

# 2. workers (one per camera)
nohup ./scripts/dev/run_worker.sh      > logs/app/worker.log      2>&1 &
nohup ./scripts/dev/run_worker_pier.sh > logs/app/worker_pier.log 2>&1 &

# 3. dashboard
nohup ./scripts/dev/run_dashboard.sh > logs/app/dashboard.log 2>&1 &

# 4. SenseStream poller
nohup ./scripts/dev/run_poller.sh > logs/app/poller.log 2>&1 &

# 5. public tunnel (optional — only when actively demoing)
nohup ~/.local/bin/cloudflared tunnel --url http://localhost:18080 \
  --no-autoupdate > logs/app/cloudflared.log 2>&1 &

# Find the public URL once cloudflared has logged it:
grep -aEo 'https://[a-z0-9-]+\.trycloudflare\.com' logs/app/cloudflared.log | head -1
```

Override a knob for a single run by setting it inline in front of the
script — inline env wins over `.env` because `.env` is sourced inside
the script, but only with `${VAR:-default}` semantics for the explicit
knobs the script references. To change a knob persistently, edit
`.env` and restart the relevant service.

### What "ready" looks like

A worker is *fully* ready when its log shows three things in order:

```
v2: torch.compile warmup done in 18.8s (batches=[1, 2, 4, 8])
v2: classifier ready — arch=vit_small_patch14_dinov2, classes=223, ...
RTSPSource connected: rtsp://...
```

Cold-start budget: ~25-30 s from `python -m app.main` to first detection.

### GPU pinning on the shared DGX

The DGX is shared with other researchers; one neighbour saturating GPU 0
will spike `last_infer_ms` from ~1 ms to several hundred. Pin to a
specific physical GPU via `CUDA_VISIBLE_DEVICES` in `.env`. With the
two-worker setup we use separate GPUs for SEAHIVECAM (GPU 4) and pier
(GPU 5). Inside the process, both keep `DEVICE=cuda:0` — the env var
remaps the visible GPU to index 0.

**Don't** set `DEVICE=cuda:4` directly. ultralytics YOLO clobbers
`CUDA_VISIBLE_DEVICES` on init when given a non-zero device index,
which breaks the subsequent classifier `torch.jit.load` with
`device_count() is 1`.

Pre-flight check before restarting:

```bash
nvidia-smi --query-gpu=index,utilization.gpu,memory.used --format=csv,noheader
```

Pick a GPU at 0 % util and below ~50 GB memory used.

---

## Stopping

```bash
# kill by listening port — robust against pid-file drift
for port in 8081 8083 18080 8082; do
  ss -ltnp 2>/dev/null | grep ":$port" \
    | grep -oP 'pid=\K\d+' | xargs -r kill -TERM
done

# stop the cloudflared tunnel
pkill -f 'cloudflared tunnel'

# stop postgres
./scripts/dev/stop_postgres.sh
```

Graceful shutdown gives each subprocess ~5 s to drain in-flight work
(save queue, frame-stats flush, pg connection close). If you `kill -9`
you'll get warnings about un-released semaphores in the log; harmless
but ugly.

---

## Recovery from a DGX reboot

The whole stack does not currently auto-start on boot. After a reboot:

```bash
conda activate wahoobay
cd /raid/scratch/dzimmerman2021/wahoobay
# follow "Starting from cold" above
```

To make this survive reboots, the documented fix is `systemd --user`
units (one per service); writing those is on the deferred-work list.
For now, restart by hand.

---

## Restarting one service

The pattern is the same for all of them: kill by listening port, then
re-run the launcher script. Common cases:

```bash
# Just the SEAHIVECAM worker
ss -ltnp 2>/dev/null | grep ':8081' | grep -oP 'pid=\K\d+' | xargs -r kill -TERM
sleep 5  # let the inference subprocess + save thread drain
nohup ./scripts/dev/run_worker.sh > logs/app/worker.log 2>&1 &

# Just the pier worker
ss -ltnp 2>/dev/null | grep ':8083' | grep -oP 'pid=\K\d+' | xargs -r kill -TERM
sleep 5
nohup ./scripts/dev/run_worker_pier.sh > logs/app/worker_pier.log 2>&1 &

# Just the dashboard
ss -ltnp 2>/dev/null | grep ':18080' | grep -oP 'pid=\K\d+' | xargs -r kill -TERM
sleep 3
nohup ./scripts/dev/run_dashboard.sh > logs/app/dashboard.log 2>&1 &
```

The smoother resets on worker restart (which is fine — its in-memory
state isn't precious; the persisted detections are unaffected). Track
IDs restart at 1 after the restart.

---

## When does a change require a restart?

| Change | Which service | Restart needed? |
|---|---|---|
| Dashboard HTML / JS / CSS | dashboard | **No** — `?v=<mtime-hash>` query string cache-busts; just hard-refresh the browser |
| Dashboard Python code | dashboard | **Yes** — restart dashboard |
| Worker Python code | worker(s) | **Yes** — restart worker(s) |
| `.env` value | depends on knob — all if shared | **Yes** for the affected service(s) |
| `db/init.sql` | none directly | **No** — but you must also apply the new DDL to the running DB (e.g. via `psql`); init.sql is just what a fresh deploy starts from |
| `data/camera_negative_regions.json` (polygon mask) | worker(s) | **Yes** — workers load the file at subprocess startup |
| `assets/celebrities.json` | dashboard | **No** — hot-reloaded automatically by the sheet poller; manual force: `POST /api/admin/refresh_sheets` |
| `assets/species_manifest.json` (common-name fallbacks) | dashboard | **No** — same hot-reload path |
| Curated `COMMON_NAMES` dict in `common_names.py` | dashboard | **Yes** — Python code change, restart |
| New classifier checkpoint at `data/models/classifier_v2/checkpoint_best.pt` | worker(s) | **Yes** — restart worker(s); torch.compile re-warms (~25 s) |

---

## Logs

All long-running processes write to `logs/app/`:

```
logs/app/worker.log          # SEAHIVECAM worker pipeline + provenance + smoother + autoswitch
logs/app/worker_pier.log     # pier cam worker (same format)
logs/app/dashboard.log       # FastAPI access log + SLO checker + sheet poller + reload events
logs/app/poller.log          # SenseStream probe results, idle-mode messages
logs/app/cloudflared.log     # tunnel status + the assigned public URL
logs/app/postgres.log        # Postgres server log
logs/events/events-YYYY-MM-DD.jsonl   # per-detection JSONL audit trail (durable backup of detection_events)
```

There is no log rotation configured at the moment — if disk fills,
manually trim or run `logrotate`.

Useful one-liners:

```bash
# Last 50 worker errors
grep -E 'ERROR|Traceback' logs/app/worker.log | tail -50

# Current fps from worker stats
curl -s http://127.0.0.1:8081/stats | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(f'seen={d[\"frames_seen\"]} inferred={d[\"frames_inferred\"]} infer_ms={d[\"last_infer_ms\"]:.1f}')"

# All ERROR-level lines from the dashboard in the last hour
awk -v cutoff="$(date -u -d '1 hour ago' '+%Y-%m-%d %H:%M:%S')" '
  $0 >= cutoff && /ERROR/' logs/app/dashboard.log
```

---

## Configuration

All knobs are env vars. `.env.example` documents every one. Major blocks:

| Block | Vars |
|---|---|
| Video source | `VIDEO_SOURCE`, `VIDEO_LOOP`, `PLAYLIST_SHUFFLE`, `REALTIME_PACING`, `VIDEO_SOURCE_PIER` |
| Source labels | `SOURCE_LABEL_PRIMARY`, `SOURCE_LABEL_FALLBACK`, `SOURCE_LABEL_PRIMARY_PIER` |
| Autoswitch fallback | `FALLBACK_VIDEO_SOURCE`, `AUTOSWITCH_DARK_THRESHOLD`, `AUTOSWITCH_LIGHT_THRESHOLD`, `AUTOSWITCH_SAMPLE_EVERY_N_FRAMES`, `AUTOSWITCH_WINDOW_SAMPLES` |
| Classifier backend | `CLASSIFIER_BACKEND` (`v2` default, `fishial` for revert) |
| v2 classifier | `CLASSIFIER_V2_CHECKPOINT`, `CLASSIFIER_V2_CLASSES_JSON`, `CLASSIFIER_V2_ARCH`, `CLASSIFIER_V2_INPUT_SIZE`, `CLASSIFIER_V2_COMPILE` |
| Legacy classifier | `CLASSIFIER_PATH`, `CLASSIFIER_INFERENCE_MODULE_DIR`, `CLASSIFIER_METHOD`, `CLASSIFIER_INPUT_H/W` |
| Detector | `DETECTOR_PATH`, `DET_IMGSZ`, `DET_CONF_THRESHOLD`, `DET_IOU_THRESHOLD` |
| Classifier thresholds | `MIN_CLASSIFY_DET_CONF` (gate before classifier runs), `MIN_ACCEPT_ACCURACY` (softmax floor for topk), `CLASSIFIER_TOPK`, `CLASSIFIER_AUTOCAST` |
| GPU | `CUDA_VISIBLE_DEVICES`, `CUDA_VISIBLE_DEVICES_PIER`, `DEVICE` |
| Smoother | `TRACKER_ENABLED`, `TRACKER_WINDOW_FRAMES`, `TRACKER_CENTER_ALPHA`, `TRACKER_VELOCITY_ALPHA`, `TRACKER_MAX_AGE`, `TRACKER_MIN_HITS`, `TRACKER_IOU_THRESHOLD` |
| Persistence | `DATABASE_URL`, `EVENTS_LOG_DIR`, `FRAMES_DIR` |
| Image saves (3 modes) | `SAVE_TIMELAPSE_SECONDS`, `SAVE_PER_DETECTION`, `SAVE_INTERESTING_ONLY`, `SAVE_INTERESTING_QUIET_SECONDS`, `SAVE_INTERESTING_MIN_CONF`, `SAVE_MIN_TRACK_GAP_SECONDS` |
| Negative regions | `NEGATIVE_REGIONS_PATH` |
| HTTP / dashboard | `WORKER_HTTP_PORT`, `WORKER_HTTP_PORT_PIER`, `WORKER_URL_PIER`, `DASHBOARD_PORT`, `DASHBOARD_HOST`, `DASHBOARD_WRITE_TOKEN`, `JPEG_QUALITY`, `LIVE_STREAM_MAX_FPS` |
| Drift / model-ops | `FRAME_STATS_EVERY_N_FRAMES`, `MATVIEW_REFRESH_INTERVAL_S` |
| Sheet poller | `SHEET_URL`, `SHEET_POLL_INTERVAL_HOURS` |
| SenseStream / Wahoo Bay API | `WAHOOBAY_API_URL`, `WAHOOBAY_API_ENABLED`, `WAHOOBAY_SITE_SONDE`, `WAHOOBAY_SITE_WEATHER`, `SENSESTREAM_*`, `OPEN_METEO_*` |
| PTZ poller | `PTZ_POLL_ENABLED`, `PTZ_POLL_URL`, `PTZ_POLL_BACKEND`, `PTZ_POLL_INTERVAL_S`, `PTZ_SOURCE_NAME` |
| Logging | `LOG_LEVEL` |

### Threshold tuning quick reference

For v2 (default backend), thresholds work in three layers:

1. **Detector** drops anything below `DET_CONF_THRESHOLD` (default 0.25).
   NMS at `DET_IOU_THRESHOLD` (0.45).
2. **Gate before classifier**: detections with det_conf >=
   `MIN_CLASSIFY_DET_CONF` (default 0.25) go to the classifier; below
   that, the event is persisted with `topk=[]` and the dashboard shows
   "unknown species." v2 is fast enough that this can effectively be
   set equal to `DET_CONF_THRESHOLD` (runs on everything).
3. **Classifier softmax floor**: only top-K candidates with softmax >=
   `MIN_ACCEPT_ACCURACY` (default 0.55) survive into the topk list. If
   the top-1 is below 0.55, the dashboard renders the event as
   "unknown species" — better than showing a confident wrong guess.

End-to-end:

```
det_conf < 0.25                                 → dropped at detector
det_conf in [0.25, 0.40) AND prior to v2 tuning → persisted, topk=[] (UNUSED post-v2)
det_conf >= 0.25 AND top1_softmax < 0.55        → persisted, topk=[] (shown as "unknown")
det_conf >= 0.25 AND top1_softmax >= 0.55       → species shown
```

### Video quality knobs

Three knobs stack to determine the final image quality on the live
overlay. Stronger = lower latency, lower bandwidth, lower quality:

| Knob | What it controls | Current default | Range |
|---|---|---|---|
| Axis `compression=` in `VIDEO_SOURCE` URL | Camera-side H.264 compression. **0 = best quality, 100 = most compressed.** Axis docs call this "compression," counter-intuitively. | 50 | 0-100 |
| `JPEG_QUALITY` | Worker re-encode quality for the live MJPEG + saved frames | 92 | 1-100 |
| Axis `resolution=` in URL | Source resolution | 1280x720 | up to 1920x1080 |

Per-frame pickle cost scales with frame size. 1080p produces 2.25× the
bytes of 720p — even with the shared-memory hop in place (which is
zero-copy and unaffected), the cv2 raw-JPEG encode for the
`/stream_raw.mjpeg` MJPEG stream is per-frame and bottlenecks the
FrameTap thread above 720p. **Do not raise to 1080p without also
verifying steady-state fps doesn't collapse.** Measure with:

```bash
g1=$(curl -s "http://127.0.0.1:8081/stats" | python3 -c "import json,sys;print(json.load(sys.stdin)['frames_seen'])")
sleep 30
g2=$(curl -s "http://127.0.0.1:8081/stats" | python3 -c "import json,sys;print(json.load(sys.stdin)['frames_seen'])")
echo "fps over 30s: $(echo "scale=2;($g2-$g1)/30" | bc)"
```

Should be 15.0+ for the SEAHIVECAM (its native rate) and similar for
the pier cam (which is set to 15 fps by camera config too).

---

## Public tunnel lifecycle

The current tunnel is a **quick tunnel** (no Cloudflare account,
random `*.trycloudflare.com` URL). The URL **changes on every
cloudflared restart**, including DGX reboots. After a restart, fetch
the new URL:

```bash
grep -aEo 'https://[a-z0-9-]+\.trycloudflare\.com' logs/app/cloudflared.log | head -1
```

…and re-share with anyone using it.

**Lab policy:** the DGX cannot be a permanent public host. The
Cloudflare quick tunnel is intentionally ephemeral so the dashboard
is reachable for live demos but the URL doesn't get circulated as a
durable resource. Cloud migration (AWS) will land the permanent URL.

For a **persistent URL** (e.g. `wahoobay-fish.example.com`), upgrade to
a named tunnel: needs a Cloudflare account + a domain Cloudflare can
manage DNS for. About 10 minutes of setup. See
<https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/>.
Don't do this on the DGX — wait for the cloud migration.

---

## Admin endpoints

A few mutation endpoints exist beyond the reviewer-corrections flow:

### Polygon mask for false-positive geometry

`/admin/regions.html` — interactive UI for drawing per-camera
no-fish-zone polygons (the SEAHIVE holes, dock pilings, etc.). Pick a
camera, click on a frozen snapshot to add vertices, double-click to
commit, Save to write the JSON file. **Restart the workers after
saving** — they load the file once at subprocess start.

The companion API endpoints (read + write) are documented in
[`docs/architecture.md`](architecture.md). The page is not
network-authenticated; access it via SSH tunnel rather than through
the public tunnel.

### Force-refresh the Google Sheet

```bash
curl -s -X POST http://127.0.0.1:18080/api/admin/refresh_sheets \
  -H "Authorization: Bearer <DASHBOARD_WRITE_TOKEN>"
```

Pulls the sheet now (instead of waiting for the daily auto-poll),
re-runs the build scripts, and hot-reloads the catalogues. Use this
after a chatter has named a new fish and you want it live immediately.

---

## Common issues

### "Can't reach the dashboard from my laptop"

VS Code Remote-SSH normally auto-forwards 18080. Verify in the PORTS
panel. If it dropped: PORTS → "Forward a Port" → 18080. Or use a
manual SSH tunnel from a terminal: `ssh -L 18080:localhost:18080
dgx1`.

For the admin endpoints (`/admin/regions.html`, etc.) use the same
tunnel — do not route them through the public Cloudflare tunnel.

### "Dashboard loaded but everything is empty"

Hard-reload (`Cmd+Shift+R` / `Ctrl+Shift+R`). The static asset URLs
include a cache-busting `?v=<hash>` query param, so a normal reload
usually picks up changes — but a hard-reload is the guaranteed-clean
fix.

### "Worker isn't writing detections"

Check `/api/stats`:

- `running: false` → worker crashed; tail `logs/app/worker.log`.
- `autoswitch.is_dark: true` → the live camera is dark (sunset / lens
  blocked / cable issue), pipeline is in fallback mode and
  **deliberately not writing video data.** The dashboard banner says
  so. Resolves on its own when light returns.
- `frames_seen` not advancing → primary video source unreachable. Try
  the URL from the DGX with `ffprobe -rtsp_transport tcp -i '<url>'`
  to verify. Camera credentials live in `.env`; if they've rotated,
  Nigel Cardwell is the point of contact for the City of Pompano
  Beach (pier cam) and Wahoo Bay program staff for SEAHIVECAM.
- `frames_seen` advancing but `frames_inferred` flat → inference
  subprocess crashed; tail the log for tracebacks. Common causes:
  classifier checkpoint file moved / corrupted, GPU OOM (check
  `nvidia-smi`).

### "Drift panel is empty"

The drift baseline needs ~24 h of `frame_stats` rows to become
meaningful. Right after a fresh start there's nothing yet; the SLO
rules dependent on the drift view will be silent (not alerting),
which is correct.

### "Postgres won't start"

```bash
ls pgdata/PG_VERSION                     # cluster initialised?
cat logs/app/postgres.log | tail         # last error?
pg_ctl -D pgdata status                  # is it actually running?
```

If the cluster is corrupted or you want to start fresh:

```bash
./scripts/dev/stop_postgres.sh
mv pgdata pgdata.broken-$(date +%s)
./scripts/dev/start_postgres.sh
```

This wipes everything; only do it if you've exhausted recovery and the
data isn't precious (it usually is — be careful).

### "Cloudflare tunnel is dead but the dashboard is still up"

`cloudflared` exited but the dashboard is fine — restart just the
tunnel:

```bash
nohup ~/.local/bin/cloudflared tunnel --url http://localhost:18080 \
  --no-autoupdate > logs/app/cloudflared.log 2>&1 &
```

Get the new URL from the log.

### "Throughput dropped below the camera's native fps"

Camera streams at 15 fps but workers are only inferring at 13 fps? Three
candidate culprits, in order of likelihood:

1. **GPU contention** — another job on the same physical GPU. Check
   `nvidia-smi`; pin to a free GPU.
2. **JPEG re-encode bottleneck on the raw stream** — at high
   `JPEG_QUALITY` and high resolution, the cv2 imencode call inside
   FrameTap can cap the grab rate. Lower `JPEG_QUALITY` or drop
   resolution.
3. **Slot starvation in the shm pool** — log line "save queue full"
   means inference is fast but disk I/O isn't keeping up; raise the
   queue or reduce save cadence.

### "v2 classifier load fails: missing checkpoint"

Worker log shows `FileNotFoundError: .../classifier_v2/checkpoint_best.pt`.
Confirm the file exists at the configured path (`CLASSIFIER_V2_CHECKPOINT`
in `.env`). For the in-repo path, the file is gitignored and not
distributed — copy it from `data/baseline_runs/<best_run>/checkpoint_best.pt`
or pull from your backup.

### "Negative-regions filter is doing nothing"

Worker log on startup should show:

```
negative-regions filter loaded: seahivecam=N@1280x720, pier_cam=M@1280x720
```

If it says "no polygons; pass-through," then the JSON file's `regions`
arrays are empty for those cameras. Draw polygons at
`/admin/regions.html`, Save, restart the workers.

### "Sheet poller flagged 'sheet_changed' but I didn't edit anything"

Google's web UI re-compresses embedded images on save, even when no
cell data changed. The poller sees the bytes change and dutifully
rebuilds, but the resulting JSON files are byte-identical. Not a bug —
just noise on the channel. Spot-check via:

```bash
diff <(git show HEAD:assets/species.xlsx | sha256sum) <(sha256sum assets/species.xlsx)
```

---

## Disk usage

The system writes:

- `frames/YYYY/MM/DD/HH/<frame>.{jpg, annotated.jpg, coco.json}` — at
  the configured save cadence. With default settings (timelapse 60 s,
  interesting-only on detections), grows about 1-2 GB/day **per
  camera**. With two cameras that's 2-4 GB/day.
- `logs/events/events-YYYY-MM-DD.jsonl` — one line per detection per
  frame, ~150-500 MB/day at typical activity.
- Postgres tables — `detection_events` is the largest. About 30 M
  rows / ~10 GB after a few weeks of continuous operation. Hourly
  matview refreshes add ~10 MB/day.

There is no automatic retention. To clean up old frames or events,
write a cron job or use the export pipeline to ship to S3 and delete
locally. None of this is set up yet.

For an emergency wipe of detection-side data while preserving reviewer
corrections + ingest pipelines:

```sql
-- inside psql -h 127.0.0.1 -U wahoobay wahoobay
BEGIN;
ALTER TABLE detection_corrections DROP CONSTRAINT IF EXISTS detection_corrections_event_id_fkey;
TRUNCATE TABLE detection_events, saved_frames, frame_stats RESTART IDENTITY;
ALTER TABLE detection_corrections
  ADD CONSTRAINT detection_corrections_event_id_fkey
  FOREIGN KEY (event_id) REFERENCES detection_events(id) ON DELETE CASCADE NOT VALID;
REFRESH MATERIALIZED VIEW species_sightings_mat;
REFRESH MATERIALIZED VIEW species_sightings_hourly_mat;
COMMIT;
```

The `NOT VALID` is intentional — existing orphan corrections (their
`event_id` no longer matches anything) survive; future inserts get the
constraint enforced.

Then delete the frames:

```bash
mv frames frames.deleting && mkdir frames && nohup rm -rf frames.deleting &
```

The `mv` + background `rm` keeps inference writing to a fresh tree
immediately; the deletion of ~10 M files takes ~10 min in the
background.

---

## Backups

Postgres has not been configured with point-in-time recovery yet. For
a manual snapshot:

```bash
mkdir -p backups
PGPASSWORD=wahoobay pg_dump -h 127.0.0.1 -U wahoobay wahoobay \
  > backups/wahoobay_$(date +%Y%m%dT%H%M%SZ).sql
```

`frames/` and `logs/` are append-only and easy to `rsync` somewhere
durable.

---

## Change auditing

Every detection_event carries:

- `model_version`, `detector_sha256`, `classifier_sha256` — identifies
  the weights that produced it.
- `config_hash` — 16-character hash of the runtime config (reproducible).
- `pipeline_git_sha` — commit SHA of the worker code.

Combined, you can fully reproduce any historical detection. Run
`/api/provenance/current` on the dashboard to see the current
fingerprint of running rows.

When you cut a new classifier checkpoint:

1. Drop the new `checkpoint_best.pt` + `classes.json` + `config.json`
   into `data/models/classifier_v2/`.
2. Restart the workers. They re-hash on boot, so the new
   `classifier_sha256` shows up on `/api/provenance/current` within
   ~30 s.
3. The old `classifier_sha256` is still on every historical row, so
   you can filter pre-/post-cutover detections in the DB by that hash.
