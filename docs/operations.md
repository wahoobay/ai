# Operations runbook

Everything you need to start, stop, recover, and observe the live
system on the DGX.

## Currently running (snapshot)

| Service | URL | Process model |
|---|---|---|
| Postgres | `127.0.0.1:5432` | Conda binary, cluster at `./pgdata`, daemonised by `pg_ctl` |
| Worker | `127.0.0.1:8081` | `python -m app.main` from `services/worker/`, GPU-pinned via `CUDA_VISIBLE_DEVICES` |
| Dashboard | `127.0.0.1:18080` | `python -m app.main` from `services/dashboard/` |
| SenseStream poller | `127.0.0.1:8082` | `python -m app.main` from `services/sensestream_poller/` |
| Cloudflare tunnel | random `*.trycloudflare.com` URL | `~/.local/bin/cloudflared tunnel --url http://localhost:18080` |

All three Python services are launched via `scripts/dev/run_*.sh` from
disowned bash background jobs. They survive logout but **not a DGX
reboot** — see "Recovery from a DGX reboot" below.

## Quick health check

```bash
# from the DGX (or via SSH tunnel)
curl -s http://127.0.0.1:8081/healthz   # → {"ok":true}
curl -s http://127.0.0.1:18080/healthz  # → {"ok":true}
curl -s http://127.0.0.1:8082/healthz   # → {"ok":true}
curl -s http://127.0.0.1:8081/stats     # full worker stats incl. autoswitch.is_dark
```

The dashboard's banner across the top of the page lists any active SLO
alerts. If the banner is empty, all six health rules are satisfied.

## Starting from cold

Order matters: postgres → worker → dashboard → poller → tunnel.

All runtime knobs (RTSP URLs + creds, write token, GPU pin, autoswitch
thresholds, etc.) live in a single `.env` file at the repo root. Each
`scripts/dev/run_*.sh` auto-sources it, so cold-start is just "run the
script." If `.env` doesn't exist, copy `.env.example` and fill in the
secrets — the file is gitignored and should be mode 600
(`chmod 600 .env`).

```bash
conda activate wahoobay
cd /raid/scratch/dzimmerman2021/wahoobay

# 1. postgres
./scripts/dev/start_postgres.sh
# (idempotent: starts the cluster if not running, applies db/init.sql)

# 2. worker
./scripts/dev/run_worker.sh > logs/app/worker.log 2>&1 &
disown

# 3. dashboard
./scripts/dev/run_dashboard.sh > logs/app/dashboard.log 2>&1 &
disown

# 4. SenseStream poller
./scripts/dev/run_poller.sh > logs/app/poller.log 2>&1 &
disown

# 5. public tunnel (optional)
~/.local/bin/cloudflared tunnel --url http://localhost:18080 \
  --no-autoupdate > logs/app/cloudflared.log 2>&1 &
disown
# the public URL is logged by cloudflared:
grep -aEo 'https://[a-z0-9-]+\.trycloudflare\.com' logs/app/cloudflared.log | head -1
```

Override a knob for a single run by setting it inline in front of the
script — inline env wins over `.env` because `.env` is sourced inside the
script, but only with `${VAR:-default}` semantics for the explicit knobs
the script references. To change a knob persistently, edit `.env` and
restart the relevant service.

### GPU pinning on the shared DGX

The DGX is shared with other researchers; one neighbour saturating GPU 0
will spike `last_infer_ms` from ~10 ms to several hundred. Pin to a
specific physical GPU via `CUDA_VISIBLE_DEVICES` in `.env` (e.g.
`CUDA_VISIBLE_DEVICES=4`) and leave `DEVICE=cuda:0`. Don't set
`DEVICE=cuda:4` directly — ultralytics YOLO clobbers
`CUDA_VISIBLE_DEVICES` on init when given a non-zero device index, which
breaks the subsequent classifier `torch.jit.load` with "device_count() is
1." Pre-flight check before restarting:

```bash
nvidia-smi --query-gpu=index,utilization.gpu,memory.used --format=csv,noheader
```

Pick a GPU at 0 % util and below ~50 GB memory used.

## Stopping

```bash
# kill by listening port — robust against pid-file drift
kill $(ss -ltnp 2>/dev/null | awk '/:8081|:18080|:8082/' | grep -oE 'pid=[0-9]+' | cut -d= -f2)

# stop the cloudflared tunnel
pkill -f 'cloudflared tunnel'

# stop postgres
./scripts/dev/stop_postgres.sh
```

## Recovery from a DGX reboot

The whole stack does not currently auto-start on boot. After a reboot:

```bash
conda activate wahoobay
cd /raid/scratch/dzimmerman2021/wahoobay
# follow "Starting from cold" above
```

To make this survive reboots, the documented fix is `systemd --user`
units (one per service); writing those is on the deferred-work list in
`docs/PLAN.md`. For now, restart by hand.

## Logs

All long-running processes write to `logs/app/`:

```
logs/app/worker.log       # worker pipeline + provenance + smoother + autoswitch
logs/app/dashboard.log    # FastAPI access log + SLO checker
logs/app/poller.log       # SenseStream probe results, idle-mode messages
logs/app/cloudflared.log  # tunnel status + the assigned public URL
logs/app/postgres.log     # Postgres server log
logs/events/events-YYYY-MM-DD.jsonl  # per-detection JSONL audit trail
```

There is no log rotation configured at the moment — if disk fills,
manually trim or run logrotate.

## Configuration

All knobs are env vars. `.env.example` documents every one. Major
blocks:

| Block | Vars |
|---|---|
| Video source | `VIDEO_SOURCE`, `VIDEO_LOOP`, `PLAYLIST_SHUFFLE`, `REALTIME_PACING` |
| Autoswitch fallback | `FALLBACK_VIDEO_SOURCE`, `AUTOSWITCH_DARK_THRESHOLD`, `AUTOSWITCH_LIGHT_THRESHOLD`, `AUTOSWITCH_SAMPLE_EVERY_N_FRAMES`, `AUTOSWITCH_WINDOW_SAMPLES` |
| Models | `DETECTOR_PATH`, `CLASSIFIER_PATH`, `DEVICE`, `DET_CONF_THRESHOLD`, `CLASSIFIER_TOPK`, … |
| Smoother | `TRACKER_ENABLED`, `TRACKER_WINDOW_FRAMES`, `TRACKER_CENTER_ALPHA`, `TRACKER_VELOCITY_ALPHA`, `TRACKER_MAX_AGE`, … |
| Persistence | `DATABASE_URL`, `EVENTS_LOG_DIR`, `FRAMES_DIR` |
| Image saves (3 modes) | `SAVE_TIMELAPSE_SECONDS`, `SAVE_PER_DETECTION`, `SAVE_INTERESTING_ONLY`, `SAVE_INTERESTING_QUIET_SECONDS`, `SAVE_INTERESTING_MIN_CONF` |
| HTTP / dashboard | `WORKER_HTTP_PORT`, `DASHBOARD_PORT`, `DASHBOARD_WRITE_TOKEN`, `JPEG_QUALITY`, `LIVE_STREAM_MAX_FPS` |
| SenseStream poller | `SENSESTREAM_BASE_URL`, `SENSESTREAM_AUTH_TOKEN`, `SENSESTREAM_DEPLOYMENT_URI`, `SENSESTREAM_POLL_INTERVAL_S`, `SENSESTREAM_BACKFILL_MINUTES` |
| PTZ poller | `PTZ_POLL_ENABLED`, `PTZ_POLL_URL`, `PTZ_POLL_BACKEND`, `PTZ_POLL_INTERVAL_S` |
| Drift / model-ops | `FRAME_STATS_EVERY_N_FRAMES` |
| Logging | `LOG_LEVEL` |

## Public tunnel lifecycle

The current tunnel is a **quick tunnel** (no Cloudflare account, random
`*.trycloudflare.com` URL). The URL **changes on every cloudflared
restart**, including DGX reboots. After a restart, fetch the new URL:

```bash
grep -aEo 'https://[a-z0-9-]+\.trycloudflare\.com' logs/app/cloudflared.log | head -1
```

…and re-share with anyone using it.

To get a **persistent URL** (e.g. `wahoobay-fish.example.com`), upgrade
to a named tunnel: needs a Cloudflare account + a domain Cloudflare can
manage DNS for. About 10 minutes of setup. See
<https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/>.

## Common issues

### "Can't reach the dashboard from my laptop"

VS Code Remote-SSH normally auto-forwards 18080. Verify in the PORTS
panel. If it dropped: PORTS → "Forward a Port" → 18080. Or use a manual
SSH tunnel from a terminal: `ssh -L 18080:localhost:18080 dgx1`.

### "Dashboard loaded but everything is empty"

Hard-reload (`Cmd+Shift+R` / `Ctrl+Shift+R`). The static asset
URLs include a cache-busting `?v=<hash>` query param, so a normal
reload usually picks up changes — but a hard-reload is the
guaranteed-clean fix.

### "Worker isn't writing detections"

Check `/api/stats`:

- `running: false` → worker crashed; tail `logs/app/worker.log`.
- `autoswitch.is_dark: true` → the live camera is dark (sunset / lens
  blocked / cable issue), pipeline is in fallback mode and **deliberately
  not writing video data.** The dashboard banner says so. Resolves on
  its own when light returns.
- `frames_seen` not advancing → primary video source unreachable. Try
  the URL from the DGX with `curl -kI <url>` to verify.

### "Dashboard's drift panel is empty"

The drift baseline needs ~24 h of `frame_stats` rows to become
meaningful. Right after a fresh start there's nothing yet; the SLO
rules dependent on the drift view will be silent (not alerting), which
is correct.

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
~/.local/bin/cloudflared tunnel --url http://localhost:18080 \
  --no-autoupdate > logs/app/cloudflared.log 2>&1 &
disown
```

Get the new URL from the log.

## Disk usage

The system writes:

- `frames/YYYY/MM/DD/HH/<frame>.{jpg, annotated.jpg, coco.json}` — at
  the configured save cadence. With default settings (timelapse 60 s,
  interesting-only on detections), grows about 1–2 GB/day per camera.
- `logs/events/events-YYYY-MM-DD.jsonl` — one line per detection per
  frame, ~150–500 MB/day at typical activity.
- Postgres tables — `detection_events` is the largest. About 30 M rows
  / ~10 GB after a few weeks of continuous operation.

There is no automatic retention. To clean up old frames or events,
write a cron job or use the export pipeline to ship to S3 and delete
locally. None of this is set up yet.

## Restarting just one service

Restart the worker without touching anything else:

```bash
wpid=$(ss -ltnp 2>/dev/null | awk '/:8081/' | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
[[ -n "$wpid" ]] && kill "$wpid" && sleep 2

# (re-run the worker from "Starting from cold" — same env)
```

Same pattern works for the dashboard (port 18080) and poller (8082).
The DB persists; the smoother resets (which is fine — its in-memory
state isn't precious).

## Backups

Postgres has not been configured with point-in-time recovery yet. For a
manual snapshot:

```bash
PGPASSWORD=wahoobay pg_dump -h 127.0.0.1 -U wahoobay wahoobay \
  > backups/wahoobay_$(date +%Y%m%dT%H%M%SZ).sql
```

`frames/` and `logs/` are append-only and easy to `rsync` somewhere
durable.

## Change auditing

Every detection_event carries:

- `model_version`, `detector_sha256`, `classifier_sha256` — identifies
  the weights that produced it.
- `config_hash` — 16-character hash of the runtime config (reproducible).
- `pipeline_git_sha` — commit SHA of the worker code.

Combined, you can fully reproduce any historical detection. Run
`/api/provenance/current` on the dashboard to see the current
fingerprint of running rows.
