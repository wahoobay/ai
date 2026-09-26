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
# Starts the cluster if not running, then applies ALL of db/init.sql —
# on every run, even when postgres was already up. Safe here because
# nothing is reading the database yet. Do NOT run it against a live,
# in-use cluster "to be safe": see "Applying a schema change to the
# live database" below for why, and for what to do instead.

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
| `db/init.sql` | none directly | **No** — but the new DDL must also reach the running DB. Apply *only the new statements*, never the whole file — see "Applying a schema change to the live database" below |
| `data/camera_negative_regions.json` (polygon mask) | worker(s) | **Yes** — workers load the file at subprocess startup |
| `assets/celebrities.json` | dashboard | **No** — hot-reloaded automatically by the sheet poller; manual force: `POST /api/admin/refresh_sheets` |
| `assets/species_manifest.json` (common-name fallbacks) | dashboard | **No** — same hot-reload path |
| Curated `COMMON_NAMES` dict in `common_names.py` | dashboard | **Yes** — Python code change, restart |
| New classifier checkpoint at `data/models/classifier_v2/checkpoint_best.pt` | worker(s) | **Yes** — restart worker(s); torch.compile re-warms (~25 s) |

### Applying a schema change to the live database

`db/init.sql` is **idempotent** — re-running it converges the schema —
but that is not the same as **safe to re-run against a database that
is serving traffic**. Don't `psql -f db/init.sql` against the live
cluster, and don't run `start_postgres.sh` "to be safe" while the
dashboard and workers are up (it applies the whole file too).

Why: the file contains four `CREATE OR REPLACE VIEW` statements
(`frame_stats_hourly`, `frame_stats_drift`, `species_sightings`,
`species_counts_hourly`). Replacing a view takes an `ACCESS EXCLUSIVE`
lock even when the definition is unchanged. The dashboard reads those
views constantly, and the matview refresher runs
`REFRESH MATERIALIZED VIEW CONCURRENTLY` over a multi-GB view for
anywhere from tens of seconds to minutes. The replacement queues behind
that refresh — and while it queues, **every other query on that view
queues behind it**. The public species endpoints stall for the length
of the refresh.

**Changing a foreign key that points at `detection_events`** (adding
is fine; dropping or swapping one is not) needs a brief `ACCESS EXCLUSIVE`
lock on `detection_events`, and the dashboard's matview refresher keeps
that table busy almost continuously. With the dashboard running, the
statement just hits its `lock_timeout` (the review-tool migration did,
2026-09-26). Stop the dashboard, apply the migration, then start it
again: a few seconds of downtime.

Instead, apply only the statements you added, as one guarded
transaction:

```bash
# new_ddl.sql = just your new statements, prefixed with:
#   SET lock_timeout = '5s';
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -1 -f new_ddl.sql
```

- `-1` wraps the file in one transaction, and `ON_ERROR_STOP=1` makes
  any failure roll the whole thing back — no half-applied schema.
  (Without it, `psql -f` carries on past errors and still exits 0.)
- `lock_timeout` makes it give up after 5 s rather than queue behind a
  long refresh and block every reader behind it. If it times out,
  nothing changed; retry in a minute.
- Add the same statements to `db/init.sql` so a fresh deploy gets them.

Before touching production, check what would actually be created — a
statement in `init.sql` whose object already exists is a no-op, and one
whose object is missing will do real work:

```sql
-- objects init.sql declares that this database lacks
SELECT indexname FROM pg_indexes WHERE schemaname = 'public'
UNION SELECT matviewname FROM pg_matviews
UNION SELECT tablename FROM pg_tables WHERE schemaname = 'public';
```

**Known issue — fresh databases.** Three `ALTER TABLE saved_frames ADD
COLUMN IF NOT EXISTS ...` statements (model_version, config_hash,
pipeline_git_sha) appear *above* the `CREATE TABLE saved_frames` further
down the file, so on an empty database they fail. `start_postgres.sh`
applies the file without `ON_ERROR_STOP`, so the error scrolls past and
the rest succeeds — leaving `saved_frames` **without those three
columns**. Production has them only because the table already existed
when those lines were added. Anyone standing up a fresh deployment
should check `\d saved_frames` afterwards until the file is reordered.

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
| HTTP / dashboard | `WORKER_HTTP_PORT`, `WORKER_HTTP_PORT_PIER`, `WORKER_URL_PIER`, `DASHBOARD_PORT`, `DASHBOARD_HOST`, `JPEG_QUALITY`, `LIVE_STREAM_MAX_FPS` |
| Accounts / auth (see "Accounts and roles" below) | `DASHBOARD_COOKIE_SECURE`, `DASHBOARD_SESSION_TTL_H`, `DASHBOARD_SESSION_ABS_TTL_D`, `DASHBOARD_LOGIN_MAX_FAILS`, `DASHBOARD_LOGIN_LOCKOUT_MIN`, `DASHBOARD_REGISTRATION_OPEN`, `DASHBOARD_REGISTER_MAX_PER_IP_H`, `DASHBOARD_RESET_TTL_MIN`, `DASHBOARD_VERIFY_TTL_H`, `DASHBOARD_MAIL_BACKEND`, `DASHBOARD_SMTP_*`, `DASHBOARD_PUBLIC_BASE_URL` |
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
[`docs/architecture.md`](architecture.md). Saving now requires an
authenticated admin session (see "Accounts and roles" below) — the
page itself still isn't network-authenticated, so access it via SSH
tunnel rather than through the public tunnel.

### Force-refresh the Google Sheet

`POST /api/admin/refresh_sheets` requires an admin session cookie plus
the `X-Requested-With: wahoobay` CSRF header. From a shell, log in and
reuse the cookie jar:

```bash
curl -s -c /tmp/wb_cookies -X POST http://127.0.0.1:18080/api/auth/login \
  -H "Content-Type: application/json" -H "X-Requested-With: wahoobay" \
  -d '{"username":"<admin username>","password":"<admin password>"}'

curl -s -b /tmp/wb_cookies -X POST http://127.0.0.1:18080/api/admin/refresh_sheets \
  -H "X-Requested-With: wahoobay"
```

Pulls the sheet now (instead of waiting for the daily auto-poll),
re-runs the build scripts, and hot-reloads the catalogues. Use this
after a chatter has named a new fish and you want it live immediately.

---

## Accounts and roles

Three roles: `viewer` (read-only — the default for a freshly-approved
or self-registered account), `reviewer` (can submit corrections),
`admin` (can also acknowledge alerts, refresh the sheet, save
no-fish-zone regions, and manage other accounts at
`/admin/users.html`). A `pending` account (awaiting approval) or a
`disabled` one can read the public dashboard but every mutation route
returns 401/403 regardless of its underlying role — see
`effective_role()` in `services/dashboard/app/auth.py`.

**Creating the first admin.** Self-registration always creates a
`pending` account, and approving one requires an admin who doesn't
exist yet on a fresh database. Bootstrap it from the command line
instead — this writes the account active and pre-approved, and never
accepts the password as a CLI argument (prompts with `getpass`, since
this is a shared machine where `ps` shows other users' command lines):

```bash
conda activate wahoobay
python scripts/create_user.py --username admin --display-name "Your Name" \
  --email you@example.org --role admin
```

**Approving a registration.** New self-registrations show up under
"Awaiting approval" at `/admin/users.html` (or
`GET /api/admin/users?status=pending`). Approving sets `status=active`
and assigns a role in one step —
`POST /api/admin/users/{id}/approve` with `{"role": "reviewer"}` (or
`"viewer"`/`"admin"`). Approval does not require the account's email
to be verified first; verification and approval are independent
checks.

**The manual mail backend.** `DASHBOARD_MAIL_BACKEND=manual` (the
default — there's no SMTP relay configured on the DGX) never actually
sends the verification/reset email; it logs that a message was held
and moves on, without ever printing the raw token or link (raw tokens
never appear in logs), so the link isn't retrievable after the fact.
In practice this means: don't rely on the email-verification step in
manual mode — approve reviewers directly from `/admin/users.html` as
above regardless of whether they've verified their email.

For a forgotten password, an administrator mints the reset link
directly: the "Reset link" button next to each account on
`/admin/users.html` (or `POST /api/admin/users/{id}/reset-link`) mints
a fresh, single-use password-reset token the same way
`/api/auth/forgot-password` does and returns the full link — pass it
to the account holder out of band (chat, in person, however). The
account's existing password is untouched until the link is actually
redeemed. This is the intended recovery path in manual mode; there is
deliberately no way to read back a *previously*-issued link (or set a
password directly), since the raw token is never stored — only its
hash.

Set `DASHBOARD_MAIL_BACKEND=smtp` and fill in the `DASHBOARD_SMTP_*`
block in `.env` to send real mail instead; a misconfigured or
unreachable SMTP relay degrades to `manual` automatically rather than
500ing a request. Before flipping that switch, see the two
preconditions noted next to `DASHBOARD_MAIL_BACKEND` in `.env.example`
(forgot-password isn't rate limited yet, and the STARTTLS context
fix needs to be in place).

**Closing signups.** `DASHBOARD_REGISTRATION_OPEN=false` closes
`/register` and `POST /api/auth/register` (403) without a code change
or restart-time flag — just flip the env var and restart the
dashboard process. Existing accounts are unaffected either way.

---

## Review tool (/review)

Volunteers with the reviewer or admin role label boxes at `/review`. The
design is `docs/superpowers/specs/2026-09-24-reviewer-annotation-dataset-design.md`.

The page opens in **whole-frame mode** (spec amendment
`docs/superpowers/specs/2026-09-25-review-frame-first-design.md`). The queue
still picks a box (the volunteer's lane, thinnest species first), and the
page opens that box's frame with it selected. Volunteers click any box to
answer it; **S / "Next frame"** skips the boxes they didn't answer, for them
only, and frees them for everyone else; "Mark frame complete" stays
optional. **F** switches to single boxes, and the choice is remembered in
that browser. Spot-checks always appear as blind single boxes, and
"change your answer" opens the single-box view. Leaving a frame any other
way (another tab, closing the page) releases its claims without skipping
them (`POST /api/review/frames/{id}/release`).

**Skips expire.** A box a volunteer skipped (S, or "Next frame") is hidden from them for
`REVIEW_SKIP_TTL_DAYS` days (default 30), then it can be served to them again; skipping it
again restarts the clock.

### The pool task

A background task in the dashboard keeps `review_items` topped up, every
`REVIEW_POOL_TOPUP_INTERVAL_S` (300 s). Each run is capped at
`REVIEW_POOL_MAX_PROBES` index lookups and `REVIEW_POOL_MAX_RUN_S`
seconds, and every statement it issues — the big-table probes, the
window read, the retire update, the small per-box lookups, and the
progress refresh — runs under its own 5 s statement timeout scoped to
its own transaction (never a session-level `SET`, which would linger on
the pooled connection and could kill the matview refresher's 47-144 s
`REFRESH`). A run never overlaps the previous one. A probe that times
out backs that species off for an hour rather than the day-long skip a
genuinely empty probe earns — a timeout means "we don't know," not
"nothing there"; the identify lane has no per-species key to back off
by, so a timed-out identify probe is simply retried, still bounded by
the run's own probe/time budget. Its log lines start with
`review pool:` in `logs/app/dashboard.log`; each run logs what it
added, how many probes it used, and any timeouts or errors.

- **Status:** `GET /api/review/status` (admin) shows the last run, item
  counts broken down by lane/pass/status, how many open items still
  lack model hints, and the date window. The Browse tab shows the same
  to admins.
- **Progress and browse under load:** `GET /api/review/progress`
  answers with `"stale": true` when the pool's own progress refresh
  (training-set baseline + verified-box counts) hits its statement
  timeout — the counts served are the last good ones rather than a 500.
  If computing the progress summary itself times out, or a
  `GET /api/review/browse` query times out, the endpoint returns 503
  instead of hanging or 500ing; retry shortly.
- **Turn it off:** set `REVIEW_POOL_ENABLED=false` in `.env` and restart
  the dashboard (`0`, `no` and `off` work too, in any case; any other
  value leaves it on). Queued boxes keep being served; nothing new is
  added.
- **Date window:** admins can limit the queue to a range of days on the
  Browse tab (for example 2026-05-17 to 2026-06-25, the period with live
  sonde readings) via `PUT /api/review/settings/window`. Open pass-1
  boxes outside the window are retired at once (pass-2 spot-checks are
  unaffected). **Narrowing the window retires boxes permanently:** the
  pool never re-adds an event that already has a `review_items` row,
  retired or not, so widening the window back out later does not bring
  those boxes back — only newly-probed events fill the queue back in.
- **Before enabling on production:** run
  `scripts/dev/explain_review_probes.py --env <path to .env>` against
  the live database first. It `EXPLAIN`s (never executes) the pool's
  probe queries; every plan must use an index scan on
  `detection_events` / `saved_frames`, not a sequential scan over
  hundreds of millions of rows.
- **Settings:** see the `REVIEW_*` block in `.env.example`.

### Schema

The review tool's tables and columns are in
`db/migrations/2026-09-24-review-tool.sql` (mirrored in `db/init.sql`).
Apply it to the live database with the procedure in "Applying a schema
change to the live database" above:

    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -1 -f db/migrations/2026-09-24-review-tool.sql

It is idempotent. It changes `detection_corrections.event_id` to
`ON DELETE RESTRICT`: deleting a detection that has a human verdict now
fails instead of silently deleting the verdict.

**Never delete `review_items` rows**, not even to tidy up. Whether a
verdict is a spot-check (pass 2) is recorded only through its
`review_item_id` → `review_items.pass`, and that foreign key is
`ON DELETE SET NULL`. Deleting a pass-2 item turns its verdict into a
pass-1 verdict, which silently rewrites how the box resolves: an agreed
or disputed box becomes a single answer, possibly with a different
label. To stop an item being served, retire it instead
(`status = 'retired'`, with a `retired_reason`).

### Adjudication is final

An adjudication cannot be changed from the page or the API. The
adjudicate route (`POST /api/review/disputes/{event_id}/adjudicate`)
refuses any box that is not currently disputed, and nothing outranks an
adjudication when a box is resolved, so once a box is adjudicated it is
never disputed again. A wrong adjudication can only be corrected in SQL:
delete that box's `detection_corrections` row with
`is_adjudication = true`, and the box is disputed again, ready to
adjudicate from the Browse tab.

### Trying changes before deploying

`scripts/dev/seed_review_demo.py` copies an evenly spread sample of real
frames and detections into the test database (reading production in a
read-only session) and creates two demo accounts, whose passwords go to
`.superpowers/demo_credentials`. `--serve` then runs a throwaway
dashboard on `127.0.0.1:18090` against that database, with the sheet
poller, workers and mail off. Forward the port in VS Code to use it. The
test suite truncates the same database, so stop the demo before running
tests.

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
written to `/raid/scratch/dzimmerman2021/backups/wahoobay-db/` and
copied to `/mnt/beegfs/home/dzimmerman2021/backups/wahoobay-db/` on the
DGX's other disk array. Files older than 30 days are pruned from both.
Both copies are on this machine; an off-machine copy waits for the cloud
move. Run it by hand at any time: `scripts/dev/backup_db.sh`.

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
