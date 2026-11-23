# The DGX install (retired)

**Nothing runs here any more.** From its start until 2026-11-15 the fish-ID system ran 24/7 on FAU's lab DGX
(an NVIDIA H200, checkout `/raid/scratch/dzimmerman2021/wahoobay`), public only through a Cloudflare quick tunnel.
Dan shut it down on 2026-11-15 when the system moved to AWS; don't restart it. The live system's runbook is
[`operations.md`](operations.md).

This file keeps the DGX's runbook as it was, moved here unchanged from `operations.md`, because the bare-metal
launchers it describes (`scripts/dev/*.sh`) are still in the repository and still work on a conda install
(README, Quick start C), and for the record. Where it says "the live system", "production" or "the public demo",
read "the DGX, until 2026-11-15".

---

## What ran (snapshot)

| Service | URL | Process model |
|---|---|---|
| Postgres | `127.0.0.1:5432` | Conda binary, cluster at `./pgdata`, daemonised by `pg_ctl` |
| Worker — SEAHIVECAM | `127.0.0.1:8081` | `python -m app.main` from `services/worker/`, primary cam, GPU-pinned via `CUDA_VISIBLE_DEVICES` |
| Worker — pier cam | `127.0.0.1:8083` | Same code, different env (`*_PIER` overrides). Runs on a separate GPU so neither cam contends with the other |
| Dashboard | `127.0.0.1:18080` | `python -m app.main` from `services/dashboard/` |
| SenseStream poller | `127.0.0.1:8082` | `python -m app.main` from `services/sensestream_poller/` |
| Cloudflare tunnel | random `*.trycloudflare.com` URL | `~/.local/bin/cloudflared tunnel --url http://localhost:18080` |

All five Python processes are launched via `scripts/dev/run_*.sh` from
disowned bash background jobs. They survive logout, and an `@reboot`
crontab entry brings all but the tunnel back after a DGX reboot — see
"Recovery from a DGX reboot" below.

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

All runtime knobs (RTSP URLs + creds, account/session settings, GPU pin,
autoswitch thresholds, classifier backend, etc.) live in a single `.env` file at
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
# Starts the cluster if not running, then brings the schema to the current
# Alembic revision (`alembic upgrade head`; a database that predates Alembic
# is stamped at the baseline first). Safe here because nothing is reading
# the database yet. Do NOT run it against a live, in-use cluster "to be
# safe": see "Applying a schema change to the live database" below.

# 2. workers (one per camera)
nohup ./scripts/dev/run_worker.sh      > logs/app/worker.log      2>&1 &
nohup ./scripts/dev/run_worker_pier.sh > logs/app/worker_pier.log 2>&1 &

# 3. dashboard
nohup ./scripts/dev/run_dashboard.sh > logs/app/dashboard.log 2>&1 &

# 4. SenseStream poller
nohup ./scripts/dev/run_poller.sh > logs/app/poller.log 2>&1 &

# 4b. jobs loop: matview refresh + alert rules every 60 s (no port; the
#     dashboard holds no refreshers). ./scripts/dev/start_all.sh starts
#     everything above, this included.
nohup ./scripts/dev/run_jobs.sh > logs/app/jobs.log 2>&1 &

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
## Upgrading the DGX checkout (one-time, after the October 2026 platform work)

The code the DGX runs became the code that also runs on a laptop and on AWS.
One maintenance window of a few minutes, in this order, from the repo root
with the `wahoobay` conda env active. Stop first: `start_all.sh` only starts
what is not already listening, and the schema change below must not run while
the old dashboard still refreshes the matviews.

```bash
# 1. touch logs/app/MAINTENANCE (pauses the five-minute health check), then stop
#    the dashboard, the workers and the poller (the "Stopping" section; leave
#    postgres up). The public demo is down from here to step 6: ~3 min.
git pull                                                       # 2. the code
git show dgx-2026-10-24:data/models/classifier_v2/checkpoint_best.pt \
  > data/models/classifier_v2/checkpoint_best.pt               # 3. the checkpoint git no longer tracks (ignored from now on)
pip install -r services/dashboard/requirements.txt \
            -r services/sensestream_poller/requirements.txt    # 4. alembic, SQLAlchemy, boto3, pillow, ... -- not the worker's
                                                               #    requirements: torch stays exactly as installed
./scripts/dev/start_postgres.sh                                # 5. postgres is up, so this only runs the migrations: stamp 0001,
                                                               #    then 0002 and 0003. 0002 builds an index CONCURRENTLY over all of
                                                               #    detection_events: ~7 min for the DGX's 668M rows (280 GB). The
                                                               #    script prints no alembic lines; step 7 is the check.
./scripts/dev/start_all.sh                                     # 6. workers, dashboard, poller and the new jobs loop
alembic -c db/alembic.ini current                              # 7. check: 0003 (head)
curl -s localhost:18080/healthz && curl -s localhost:18080/robots.txt
tail -n 3 logs/app/jobs.log                                    #    ok=True lines every minute
```

If step 5 stops with `canceling statement due to lock timeout`, something was
still reading `detection_events`; nothing changed -- stop it and run step 5
again. An interrupted index build leaves an INVALID index, which the revision
drops and rebuilds on the retry (`SELECT indexrelid::regclass FROM pg_index
WHERE NOT indisvalid` should print nothing afterwards).

The checkout must be on `main` for `git pull` to fast-forward (the October
2026 upgrade found it on a feature branch that `main` already contained). While
the dashboard runs, `git status` shows `assets/species.xlsx` modified: the sheet
poller rewrites that tracked file on every sync. `git checkout -- assets/species.xlsx`
before a `git pull`; the next poll rewrites it.

Nothing in `.env` has to change: the launchers export the Wahoo Bay values
(`SENSESTREAM_DEPLOYMENT_URI=wahoo_2`, `TZ=America/New_York`,
`FEATURE_REVIEW_TOOL=true`, `SENSESTREAM_SYNTHETIC=true`) and derive
`FRAMES_URL` from `FRAMES_DIR` the same way in the worker and the dashboard
launchers. The old `DASHBOARD_MAIL_*` names still work; with no mail setting
the dashboard now logs sign-up links to `logs/app/dashboard.log` instead of
holding them. New frames land under `<FRAMES_DIR>/wahoobay/YYYY/MM/DD/HH/`;
old rows keep their absolute paths and still open.

**Trying changes on the DGX.** Production owns the ports (5432, 8081-8083,
18080) and the database, so a second checkout there is for `pytest` (with a
scratch database: `TEST_DATABASE_URL` pointing at a database that is not
`wahoobay`) and `ruff`, not for a second compose stack. Run the compose
stack (`make up`, `make seed`) on a laptop, where the ports are free.

## Stopping

Pause the five-minute health check first (`touch logs/app/MAINTENANCE`,
see "Recovery from a DGX reboot"), or it restarts whatever you stop.

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

Graceful shutdown gives each subprocess up to ~5 s *per background
task* to drain in-flight work (save queue, frame-stats flush, pg
connection close) before that task is abandoned — not a flat ~5 s for
the whole process. The dashboard alone cancels five background tasks
(SLO checker, matview refresher, sheet poller, session pruner, review
pool) in sequence on shutdown, then closes its DB pool (its own 5 s
default timeout), so its worst case is ~25-30 s, not "a few seconds" — this
matters because a cancelled task's in-flight Postgres statement can be
slow to unwind (the matview refresher's REFRESH has been observed
taking 47 s, 75 s, and 144 s) and psycopg has to let that drain before
honouring the cancellation; it's bounded now (each task gets at most
~5 s before shutdown moves on and abandons it) rather than blocking on
the slowest one indefinitely, which is what it did before. If you
`kill -9` you'll get warnings about un-released semaphores in the log;
harmless but ugly.

---
## Recovery from a DGX reboot

Every five minutes, this entry in the user crontab runs
`scripts/dev/health_check.sh`:

```
*/5 * * * * /raid/scratch/dzimmerman2021/wahoobay/scripts/dev/health_check.sh >> /raid/scratch/dzimmerman2021/wahoobay/logs/app/health_check.log 2>&1
```

The check looks at the five ports (5432, 8081, 8083, 18080, 8082) and the
jobs loop. When everything is up it only rewrites `logs/app/health_check.last`
(the heartbeat: its mtime is the last time cron ran it) and prints nothing.
When something is down it writes what was missing to
`logs/app/health_check.log` and runs `start_all.sh`, which follows "Starting
from cold" above (postgres, both workers, dashboard, poller, jobs loop) and
skips any service whose port is already listening — so it is also the
quickest by-hand restart:

```bash
./scripts/dev/start_all.sh
```

Before stopping anything on purpose, pause the check, or it brings the
service back within five minutes:

```bash
touch logs/app/MAINTENANCE   # health_check.sh only heartbeats while this exists
rm logs/app/MAINTENANCE      # when the stack is back
```

It never kills a process: a dashboard that listens but fails `/healthz` is
logged, and restarting it stays a by-hand decision ("Restarting one service").

Why not `@reboot`: that entry (2026-10-07 to 2026-10-25) ran once per boot,
and only if cron had this crontab loaded at that moment. After the October
2026 `/raid` restore it did not — probably because the account is resolved
through SSSD, which started two seconds before cron — and the stack stayed
down for eight days with nothing in the log to show for it. A recurring entry
gets a new chance every five minutes, and a stale heartbeat is the signal
that cron itself is not running the crontab: re-save it with
`crontab -l | crontab -`.

Neither starts the public tunnel. A restarted tunnel has a new URL that has
to be sent to the people using it, and the lab machine should only be
exposed while demoing. Start it by hand (step 5 above) when you need it.

After a reboot, check `logs/app/start_all.log` and the quick health check.
The workers use the GPUs pinned in `.env`; if a neighbour has taken them by
then, see "GPU pinning" above.

---
## Restarting one service

The pattern is the same for all of them: kill by listening port, then
re-run the launcher script. `touch logs/app/MAINTENANCE` first and `rm` it
afterwards, or the five-minute health check may race you for the port.
Common cases:

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
# Wait for the port to actually free rather than sleeping a fixed time:
# graceful shutdown cancels four background tasks with a 5 s bound each,
# so it can take ~20 s when a matview refresh is mid-flight. Relaunching
# while the old process still holds :18080 fails to bind and leaves the
# dashboard down.
while ss -ltn 2>/dev/null | grep -q ':18080 '; do sleep 1; done
# >> not >: the dashboard log is months of history; > truncates it.
nohup ./scripts/dev/run_dashboard.sh >> logs/app/dashboard.log 2>&1 &
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
| A new Alembic revision in `db/migrations/versions/` | none directly | **No** — but the running DB needs it: `alembic -c db/alembic.ini upgrade head` with the care described in "Applying a schema change to the live database" below |
| `data/camera_negative_regions.json` (polygon mask) | worker(s) | **Yes** — workers load the file at subprocess startup |
| `assets/celebrities.json` | dashboard | **No** — hot-reloaded automatically by the sheet poller; manual force: `POST /api/admin/refresh_sheets` |
| `assets/species_manifest.json` (common-name fallbacks) | dashboard | **No** — same hot-reload path |
| Curated `COMMON_NAMES` dict in `common_names.py` | dashboard | **Yes** — Python code change, restart |
| New classifier checkpoint at `data/models/classifier_v2/checkpoint_best.pt` | worker(s) | **Yes** — restart worker(s); torch.compile re-warms (~25 s) |

---

## The checkpoint git stopped tracking

**DGX, one-time after the pull that removed it from git:**
`data/models/classifier_v2/checkpoint_best.pt` is no longer tracked (it lives
in the models bucket as `classifier_v2/dgx-2026-10-24`), so `git pull` deletes
the working-tree copy. Restore it before the next worker restart:

    git show dgx-2026-10-24:data/models/classifier_v2/checkpoint_best.pt > data/models/classifier_v2/checkpoint_best.pt

It is ignored from then on. The running worker is unaffected (the weights are
in memory).

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
## Common issues on the DGX

### "Can't reach the dashboard from my laptop"

VS Code Remote-SSH normally auto-forwards 18080. Verify in the PORTS
panel. If it dropped: PORTS → "Forward a Port" → 18080. Or use a
manual SSH tunnel from a terminal: `ssh -L 18080:localhost:18080
dgx1`.

For the admin endpoints (`/admin/regions.html`, etc.) use the same
tunnel — do not route them through the public Cloudflare tunnel.

### "Postgres won't start"

```bash
ls pgdata/PG_VERSION                     # cluster initialised?
cat logs/app/postgres.log | tail         # last error?
pg_ctl -D pgdata status                  # is it actually running?
```

`FATAL: data directory "pgdata" has invalid permissions` means the
directory is not mode 0700 or 0750; a `/raid` snapshot restore left every
directory at 2755 in October 2026. `chmod 700 pgdata` and start again; the
data is intact.

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

`scripts/dev/backup_db.sh` runs nightly at 02:30 from the user's
crontab (`crontab -l`) and appends one line per run to
`logs/app/backup.log` (`… ok dump=… labels=N -> …` or `FAILED …`). It
keeps what can't be re-derived from the cameras:

- `wahoobay-<stamp>.dump`: `pg_dump --format=custom` of the whole schema
  and every table's data **except** the camera-derived bulk
  (`detection_events`, `saved_frames`, `frame_stats`; matview data is
  never dumped). That covers accounts, every verdict, the review tables,
  alerts, and the sensor and weather history, about 10 MB a night.
- `labels-<stamp>.csv.gz`: every row of `detection_corrections` joined
  with its detection (camera, timestamp, frame, model box and guess) and
  its saved frame's path. It is self-contained, so volunteers' work stays
  usable even if `detection_events` itself were lost.

Files are mode 0600 (they hold emails and password hashes). They're
written to `/raid/scratch/dzimmerman2021/backups/wahoobay-db/`, and files
older than 30 days are pruned. There is no second copy by default: the home
directory (`/mnt/beegfs/home/...`) is on the DGX's nearly full root disk and
must not hold project data (2026-10-01). Set `BACKUP_COPY_DIR` to add a
copy elsewhere; an off-machine copy waits for the cloud move. Run it by hand
at any time: `scripts/dev/backup_db.sh`.

To restore:

- **One table from last night** (someone deleted verdicts, say): restore
  the dump into a scratch database and copy the rows you need back.
  ```bash
  createdb wahoobay_restore
  pg_restore --no-owner -d wahoobay_restore <dump>    # FKs to detection_events will fail; the rows load
  ```
  Then `DROP DATABASE wahoobay_restore;` when done.
- **The labels alone**: the CSV loads anywhere
  (`\copy … FROM PROGRAM 'zcat labels-….csv.gz' WITH (FORMAT csv, HEADER)`).

`frames/` and `logs/` are append-only and easy to `rsync` somewhere
durable.
