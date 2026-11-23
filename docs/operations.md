# Operations runbook

How to check, restart, observe and change the fish-ID system where it runs: on AWS, in the stacks the
`wahoobay/infra` repository defines. The infra README is the runbook for the infrastructure itself (stopping and
restarting the site, settings and secrets, the stacks' own deploys); this file is the application's side. What
runs on the GPU host is in [`deploy/gpu-host/README.md`](../deploy/gpu-host/README.md).

Until 2026-11-15 the system ran on FAU's lab DGX. That machine is shut down for good: nothing runs there, and it
is not restarted. Its runbook (the bare-metal launchers in `scripts/dev/`, the Cloudflare tunnel, the nightly
backup) is kept in [`dgx-history.md`](dgx-history.md).

---

## Where it runs

Staging is the only environment running. `ai-wahoobay-prod` (`ai.wahoobay.net`) is defined in infra but not
deployed; it gets the GPU host at cutover (only one environment has one at a time).

| Piece | Staging (`ai-wahoobay-staging`, us-east-1) |
|---|---|
| Public entry | CloudFront at <https://staging-ai.wahoobay.net>: the dashboard, and the live video (`/hls/*`, `/stream/*`) from the GPU host |
| Dashboard | App Runner service `ai-wahoobay-staging-dashboard` (one or two instances), reachable only through CloudFront, which adds `X-Origin-Verify` |
| Worker | The GPU host `ai-wahoobay-staging-gpu`, a `g6.xlarge` (one NVIDIA L4), running `deploy/gpu-host/` under Docker Compose: the worker, MediaMTX, the NVENC encoder and Caddy. One worker, for the SEAHIVE camera (the DGX also ran one for the pier camera). Administered through SSM; there is no SSH |
| Periodic jobs | Lambda `ai-wahoobay-staging-jobs` on EventBridge rules, every 30 minutes: `refresh_matviews`, `alert_rules`, `archive_labels`. `seed`, `status` and `import_legacy` run on demand (README, "Deploying (AWS)") |
| Water and weather | Lambda `ai-wahoobay-staging-fetch` every 30 minutes, the SQS queue `ai-wahoobay-staging-readings`, Lambda `ai-wahoobay-staging-write` |
| Schema | Lambda `ai-wahoobay-staging-migrate`, run by every deploy before the dashboard rolls |
| Database | Aurora PostgreSQL Serverless v2, cluster `ai-wahoobay-staging-db` (0.5 to 2 ACU) |
| Frames | S3 `ai-wahoobay-staging-frames` |
| Weights, the sample dataset, recordings, label archives | S3 `platform-wahoobay-models`, shared by the environments |

**Staging plays a recording, not the camera.** `deploy/gpu-host/replay.conf` names the pier camera's clips of
2024-06-16 and 2024-07-11 for staging (Dan, 2026-11-04). MediaMTX plays the file in the camera's place, the worker
draws its boxes on it, and nothing is recorded (`PERSIST_ENABLED=false`): no detections, frame stats or saved
frames. The alert rules `pipeline_silence` and `frame_stats_stalled` stay on while it plays. Emptying the
`staging=` line and merging puts the camera back (`deploy/gpu-host/README.md`, "Replay").

---

## Quick health check

```bash
curl -s https://staging-ai.wahoobay.net/healthz     # the dashboard, through CloudFront

# from a wahoobay/infra checkout: the site as a visitor gets it
python -m scripts.verify_ai staging
```

The dashboard's banner lists any active alerts; an empty banner means every rule is satisfied.

The worker answers only inside the VPC. To look at it, open a session on the GPU host (its instance id:
`aws ec2 describe-instances --filters Name=tag:Name,Values=ai-wahoobay-staging-gpu`):

```bash
aws ssm start-session --region us-east-1 --target <instance id>
# then, on the host:
curl -s localhost:8081/healthz
curl -s localhost:8081/stats | python3 -m json.tool     # autoswitch state, fps, the current source
sudo docker compose -f /opt/wahoobay/deploy/gpu-host/docker-compose.yml ps
nvidia-smi                                               # the worker and the encoder share the L4
```

A worker is ready when its log shows `v2: classifier ready — arch=vit_small_patch14_dinov2, classes=223, …`.
On the GPU host the classifier is not compiled (`CLASSIFIER_V2_COMPILE=false`: the CUDA image has no C
compiler), so there is no warmup.

---

## Deploys, restarts and stops

- **Every push to `main` deploys staging**, a docs-only one too (README, "Deploying (AWS)"): new images, the
  Lambdas, the migrate Lambda, then the GPU host checks out the build and restarts its worker, and App Runner
  rolls the dashboard. The live picture and the worker's records pause while that happens, so merges to `main`
  are timed with Dan. Production is deployed by hand (`deploy-prod`).
- **Restart one container** on the GPU host, from an SSM session:
  `sudo docker compose -f /opt/wahoobay/deploy/gpu-host/docker-compose.yml restart worker` (or `mediamtx`,
  `encoder`, `caddy`). The smoother's in-memory state resets and track IDs restart at 1; persisted detections
  are unaffected.
- **Re-run the host's start-up** (after changing the cameras secret, say):
  `sudo /opt/wahoobay/deploy/gpu-host/boot.sh`. It rewrites `.env` from `/etc/wahoobay/host.env` and the
  environment's secrets, pulls the images and brings the stack up; the systemd unit `wahoobay-gpu-host` runs it at
  every boot. Never edit the host's `.env` by hand: the next boot or deploy overwrites it.
- **Restart the dashboard** without a code change: `aws apprunner start-deployment --service-arn <arn>` (the ARN
  is in the repository variables and the `AiStack` outputs).
- **Stop and start the whole site**: the cost dashboard's stop button puts up the maintenance page and stops what
  costs money; the infra README's "Restart the AI site after a stop" (`python -m scripts.restart_ai staging`)
  brings it back.

### What a change needs

| Change | On AWS |
|---|---|
| Dashboard code, templates, JS, CSS | A merge to `main`. Static files carry `?v=<hash>`; hard-reload if a page still looks stale |
| Worker code | A merge to `main` (the GPU host restarts the worker) |
| A new Alembic revision | A merge to `main`: the deploy's migrate Lambda applies it before the dashboard rolls (mind "Applying a schema change" below) |
| A setting (environment variable) | The dashboard's and the Lambdas' come from the AI stack in `wahoobay/infra` (`wbplatform/stacks/ai.py`, `locations/wahoobay.yaml`): change it there and deploy infra. The worker's come from the `.env` that `deploy/gpu-host/boot.sh` writes, so a new worker setting is a line in `boot.sh` and a merge |
| The camera's address or password | The secret `/ai/wahoobay/<env>/cameras` (key `seahivecam`), then re-run `boot.sh` |
| A recording in the camera's place | `deploy/gpu-host/replay.conf` and a merge |
| New model weights | Publish them to the models bucket (infra `scripts/publish_models.py`); `scripts/fetch_models.py` says how a worker chooses a version and when it replaces the copy it keeps under `/opt/wahoobay-data/models` |
| No-fish-zone polygons | Not wired on AWS yet (see "Polygon mask for false-positive geometry") |

---

## Logs

| What | CloudWatch log group (us-east-1) |
|---|---|
| The GPU host's containers (worker, MediaMTX, encoder, Caddy, replay) | `/ai/wahoobay/staging/gpu-host`, kept 30 days |
| The dashboard | `/aws/apprunner/ai-wahoobay-staging-dashboard/<service id>/application`; `…/service` has App Runner's own deploy events |
| Jobs, migrations, sensor fetch and write | `/aws/lambda/ai-wahoobay-staging-jobs`, `…-migrate`, `…-fetch`, `…-write`; each job run logs one JSON line and writes a row to `job_runs` |

The Lambda and App Runner groups keep everything (no retention set).

```bash
aws logs tail /ai/wahoobay/staging/gpu-host --region us-east-1 --since 1h --filter-pattern ERROR
aws logs tail /aws/lambda/ai-wahoobay-staging-jobs --region us-east-1 --since 2h
```

The worker's per-detection event log (`events-YYYY-MM-DD.jsonl`, a durable mirror of `detection_events`) is on the
GPU host's disk under `/opt/wahoobay-data/logs/events/`.

---

## Changes that touch the data

### Frames and models behind URLs

Frames are written and read through `FRAMES_URL` (`file:///data/frames` in a
container, `file://<repo>/frames` from `run_dashboard.sh` / `run_worker.sh`,
`s3://…` on AWS) under `<location>/YYYY/MM/DD/HH/<name>.jpg`;
`saved_frames.image_path` holds that key. Rows from before this change hold
absolute paths and still open. Model weights come from `MODELS_URL`
(`<model>/<version>/` + `SHA256SUMS`, published by the infra repository's
`scripts/publish_models.py`); a worker container fetches them at start with
`scripts/fetch_models.py` unless `/data/models` already holds files.

### Applying a schema change to the live database

The schema is owned by Alembic: `db/alembic.ini` and `db/migrations/versions/`.
`0001_baseline.sql` is the schema the old `db/init.sql` used to build (with
the `saved_frames` ALTERs placed after the table they alter, so a fresh
database gets every column); later revisions are small and additive.
`alembic` comes with `services/dashboard/requirements.txt`.

- **Fresh database**: `alembic -c db/alembic.ini upgrade head`. This is what
  `start_postgres.sh`, the compose `migrate` service (`make migrate`) and the
  AWS migrate Lambda run.
- **On AWS** the migrate Lambda runs `upgrade head` in every deploy, before
  the dashboard rolls; a failure stops the deploy there. The worker of the
  previous build is still writing while it runs, so the lock rules below
  apply to every revision. (The DGX database predated Alembic and was stamped
  at `0001` once; `start_postgres.sh` still does that for a bare-metal
  database that has the tables but no `alembic_version`.) Revision 0002
  (`location_id` on every data table) adds the column as a catalog-only
  change and builds its indexes `CONCURRENTLY`, so it can run while the
  workers write.
- **Writing a change**: `alembic -c db/alembic.ini revision -m "<what>"`,
  then fill in `upgrade()` / `downgrade()`. Keep statements idempotent
  (`IF NOT EXISTS`), build large indexes `CONCURRENTLY` inside
  `op.get_context().autocommit_block()` (0002 is the model), and never
  re-run the baseline against a live database.

Why the care: `CREATE OR REPLACE VIEW` takes an `ACCESS EXCLUSIVE` lock even
when the definition is unchanged. The dashboard reads the views constantly,
and the matview refresher runs `REFRESH MATERIALIZED VIEW CONCURRENTLY` over
a multi-GB view for anywhere from tens of seconds to minutes. A replacement
queues behind that refresh, and **every other query on that view queues
behind it** -- the public species endpoints stall for the length of the
refresh. Changing a foreign key that points at `detection_events` (adding is
fine; dropping or swapping one is not) needs a brief `ACCESS EXCLUSIVE` lock
on `detection_events`, which the matview refresher keeps busy almost
continuously: stop the dashboard, run the migration, start it again (a few
seconds of downtime). A revision that must do either should set
`lock_timeout` (`op.execute("SET lock_timeout = '5s'")`) so it gives up
rather than queue behind a long refresh; a timed-out migration changed
nothing and can be retried in a minute. On AWS the matview refresh is a job
every 30 minutes rather than a loop every minute, so the queueing is rarer,
not impossible.

Before touching production, see what a revision would do:

```bash
alembic -c db/alembic.ini current          # what the database is at
alembic -c db/alembic.ini history          # what exists
```

---

## Configuration

All knobs are env vars. `.env.example` documents every one. On AWS the
dashboard's and the Lambdas' values come from the AI stack in `wahoobay/infra`,
and the worker's from the `.env` that `deploy/gpu-host/boot.sh` writes ("What a
change needs" above). Major blocks:

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
| Site | `LOCATION_ID`, `SENSESTREAM_DEPLOYMENT_URI` (required), `TZ`, `EXPORT_PREFIX`, `CAMERA_METADATA_PATH`, `NEGATIVE_REGIONS_PATH` — the launchers export the Wahoo Bay values |
| Persistence | `DATABASE_URL` (or `DB_HOST`/`DB_PORT`/`DB_NAME`/`DB_USER`/`DB_PASSWORD`), `EVENTS_LOG_DIR`, `FRAMES_URL` (`FRAMES_DIR` still read as `file://`) |
| Storage / models | `MODELS_URL`, `MODEL_VERSIONS`, `SAMPLE_URL` |
| Features | `FEATURE_REVIEW_TOOL`, `LIVE_VIDEO_EMBED_URL`, `ROBOTS`, `ORIGIN_VERIFY_SECRET` |
| Mail | `MAIL_BACKEND` (`console` default), `MAIL_SMTP_HOST/PORT/USER/PASSWORD/STARTTLS`, `MAIL_FROM` (the `DASHBOARD_MAIL_*` / `DASHBOARD_SMTP_*` spellings still count) |
| Image saves (3 modes) | `SAVE_TIMELAPSE_SECONDS`, `SAVE_PER_DETECTION`, `SAVE_INTERESTING_ONLY`, `SAVE_INTERESTING_QUIET_SECONDS`, `SAVE_INTERESTING_MIN_CONF`, `SAVE_MIN_TRACK_GAP_SECONDS` |
| Negative regions | `NEGATIVE_REGIONS_PATH` |
| HTTP / dashboard | `WORKER_HTTP_PORT`, `WORKER_HTTP_PORT_PIER`, `WORKER_URL_PIER`, `DASHBOARD_PORT`, `DASHBOARD_HOST`, `JPEG_QUALITY`, `LIVE_STREAM_MAX_FPS` |
| Accounts / auth (see "Accounts and roles" below) | `DASHBOARD_COOKIE_SECURE`, `DASHBOARD_SESSION_TTL_H`, `DASHBOARD_SESSION_ABS_TTL_D`, `DASHBOARD_LOGIN_MAX_FAILS`, `DASHBOARD_LOGIN_LOCKOUT_MIN`, `DASHBOARD_REGISTRATION_OPEN`, `DASHBOARD_REGISTER_MAX_PER_IP_H`, `DASHBOARD_RESET_TTL_MIN`, `DASHBOARD_VERIFY_TTL_H`, `DASHBOARD_MAIL_BACKEND`, `DASHBOARD_SMTP_*`, `DASHBOARD_PUBLIC_BASE_URL` |
| Drift / model-ops / jobs | `FRAME_STATS_EVERY_N_FRAMES`, `JOBS_INTERVAL_S` (`run_jobs.sh`: matview refresh + alert rules) |
| Sheet poller | `SHEET_URL`, `SHEET_POLL_INTERVAL_HOURS` |
| SenseStream / Wahoo Bay API | `WAHOOBAY_API_URL`, `WAHOOBAY_API_ENABLED`, `WAHOOBAY_SITE_SONDE`, `WAHOOBAY_SITE_WEATHER`, `SENSESTREAM_*`, `OPEN_METEO_*`, `SENSESTREAM_SYNTHETIC` (default off), `SENSESTREAM_WEATHER_FETCH_URI`, `QUEUE_URL` (AWS) |
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
verifying steady-state fps doesn't collapse.** On AWS the camera's URL, with
its `resolution=` and `compression=`, is the `seahivecam` value in the cameras
secret; the worker itself reads MediaMTX. Measure where the worker runs (on the
GPU host, in an SSM session):

```bash
g1=$(curl -s "http://127.0.0.1:8081/stats" | python3 -c "import json,sys;print(json.load(sys.stdin)['frames_seen'])")
sleep 30
g2=$(curl -s "http://127.0.0.1:8081/stats" | python3 -c "import json,sys;print(json.load(sys.stdin)['frames_seen'])")
echo "fps over 30s: $(echo "scale=2;($g2-$g1)/30" | bc)"
```

Should be 15.0+ for the SEAHIVECAM (its native rate) and similar for
the pier cam (which is set to 15 fps by camera config too).

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
[`docs/architecture.md`](architecture.md). Saving requires an admin
session (see "Accounts and roles" below).

**Not wired on AWS.** The worker image does not carry
`data/camera_negative_regions.json`, so the worker on the GPU host logs
`negative-regions filter: config /data/camera_negative_regions.json not
found, disabled` and drops nothing (seen 2026-11-23). The page saves into
the dashboard's own App Runner container, which the worker never reads and
the next deploy replaces. Until the worker gets the file (in its image or
on the host), the filter is off on AWS.

### Force-refresh the Google Sheet

`POST /api/admin/refresh_sheets` requires an admin session cookie plus
the `X-Requested-With: wahoobay` CSRF header. From a shell, log in and
reuse the cookie jar:

```bash
curl -s -c /tmp/wb_cookies -X POST https://staging-ai.wahoobay.net/api/auth/login \
  -H "Content-Type: application/json" -H "X-Requested-With: wahoobay" \
  -d '{"username":"<admin username>","password":"<admin password>"}'

curl -s -b /tmp/wb_cookies -X POST https://staging-ai.wahoobay.net/api/admin/refresh_sheets \
  -H "X-Requested-With: wahoobay"
```

Pulls the sheet now (instead of waiting for the daily auto-poll),
re-runs the build scripts, and hot-reloads the catalogues. Use this
after a chatter has named a new fish and you want it live immediately.

---

## Accounts and roles

**The whole site is behind sign-in** (Wahoo Bay's decision, 2026-09-30).
Signed out, only the sign-in, create-account and forgot-password pages
answer, plus the email links they send (`/reset-password`,
`/verify-email`), `/api/auth/*`, the static files and `/healthz`. Every
other page redirects to `/login?next=…`, and every other API route answers
401. One app-wide dependency (`site_gate` in `main.py`, allowlist
`OPEN_PATHS` in `auth.py`) does this, so a new route is closed unless it is
added to that list. `tests/test_site_gate.py` walks every route.

Three roles:
- `viewer`: the dashboard, including the live cameras, charts and data
  downloads. New accounts are approved as viewers unless the admin picks
  another role.
- `reviewer`: the dashboard and the review site (`/review`).
- `admin`: everything, including accounts (`/admin/users.html`), alerts,
  the sheet refresh and the no-fish-zone regions page.

A `pending` account (registered, not yet approved) can sign in but sees only
`/waiting`: its pages redirect there and its API calls answer 403 "your
account is waiting for approval". A `disabled` account can't sign in. See
`effective_role()` in `services/dashboard/app/auth.py`.

**Creating the first admin.** Self-registration always creates a
`pending` account, and approving one requires an admin who doesn't
exist yet on a fresh database. On AWS, `seed-staging` creates it from
the app secret's `admin_password` (README, "Deploying (AWS)"). Anywhere
with access to the database, bootstrap it from the command line instead
— this writes the account active and pre-approved, and never accepts the
password as a CLI argument (it prompts with `getpass`, so the password
never shows up in `ps` or the shell history):

```bash
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

**Mail.** On AWS the dashboard sends sign-up, verification and reset
mail through Amazon SES (`MAIL_BACKEND=ses`, from `no-reply@wahoobay.net`,
set by the AI stack). Locally the default, `console`, writes each message,
link included, to the log.

**The manual mail backend.** `MAIL_BACKEND=manual` (the old default,
from when no SMTP relay was configured) never actually sends the verification/reset email; it logs that a message was held
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
dashboard process (on AWS: a value in the AI stack's App Runner
environment, in `wahoobay/infra`, and an infra deploy). Existing accounts are unaffected either way.

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

### The dashboard's older correction form

`POST /api/corrections` (the endorse / correct / not-a-fish buttons on the
dashboard) is saved through the review tool's rules. It is never spot-checked. It answers
403 when another volunteer already answered the box; 409 while someone holds it, or "a
second opinion is pending on this box" while a spot-check on it is waiting (for anyone
with no answer of their own on it); and 400 for a name outside the species list,
`choose a life stage for <name> on the review page` (a species whose catalogue entries
all carry a stage, more than one), or a detection whose frame was not saved (most live
detections).

Rows the form wrote before this change can carry raw or misspelled names. To see what
would be mapped to accepted names, run:

    python scripts/dev/remap_legacy_corrections.py --env .env

Then run it with `--apply` to make the changes in one transaction. Names it can't map
are listed and left alone.

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
`review pool:` in the dashboard's log (on AWS, its App Runner
application log group); each run logs what it
added, how many probes it used, and any timeouts or errors.

- **Status:** `GET /api/review/status` (admin) shows the last run, item
  counts broken down by lane/pass/status, how many open items still
  lack model hints. The Browse tab shows the same to admins.
- **Progress and browse under load:** `GET /api/review/progress`
  answers with `"stale": true` when the pool's own progress refresh
  (training-set baseline + verified-box counts) hits its statement
  timeout — the counts served are the last good ones rather than a 500.
  If computing the progress summary itself times out, or a
  `GET /api/review/browse` query times out, the endpoint returns 503
  instead of hanging or 500ing; retry shortly.
- **Turn it off:** set `REVIEW_POOL_ENABLED=false` in the dashboard's
  environment (on AWS, the AI stack in `wahoobay/infra`) and restart the dashboard (`0`, `no` and `off` work too, in any case; any other
  value leaves it on). Queued boxes keep being served; nothing new is
  added.
- **Date window:** each reviewer can limit *their own* queue to a range of
  days with the From/To pickers in the review page's header. The choice
  lives in that browser tab (session storage) and travels with each claim
  request (`GET /api/review/next?date_from=…&date_to=…`, local days,
  inclusive); nothing is stored on the server, nothing is retired, and
  other reviewers are unaffected. Until 2026-10-27 the window was one
  shared setting that retired boxes outside it for everyone; migration
  0004 reopened what it had retired.
- **Before enabling on production:** run
  `scripts/dev/explain_review_probes.py --env <path to .env>` against
  the live database first. It `EXPLAIN`s (never executes) the pool's
  probe queries; every plan must use an index scan on
  `detection_events` / `saved_frames`, not a sequential scan over
  hundreds of millions of rows.
- **Settings:** see the `REVIEW_*` block in `.env.example`.

### Schema

The review tool's tables and columns are in
`db/migrations/2026-09-24-review-tool.sql` (part of the Alembic baseline,
`db/migrations/versions/0001_baseline.sql`, so a fresh database has them).
It was applied to the old DGX database by hand (`psql -1 -f` of that file, 2026-09-26) before Alembic; a fresh database, staging's included, gets it from the baseline, so there is nothing to apply any more. It is idempotent. It changes `detection_corrections.event_id` to
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

## Guided tours and booth mode

Both pages have a **Take the tour** button (`static/tour.js`, drawn by the vendored driver.js): six steps on
the home page for a first-time visitor (the camera, what it sees now, who lives here, the named regulars, the
water, then "now it's yours"), six on `/review` for a first-time reviewer (how experts check the AI, pick a
box, is the AI right, can't identify or no animal, next frame, your count). The home tour leaves the visitor
on the dashboard; it does not send anyone to the review page.

**Booth mode** is for a browser at an event where visitors try the site on their own. Sign that browser in to
a **reviewer** account (never an admin one: a visitor could reach the accounts page), then open any page
with `?booth=1` once; the browser remembers it, and `?booth=0` turns it off. In booth mode:

- the home page opens with a welcome card ("Start the tour" / "or just look around"), once per visitor;
- the "Try reviewing →" button beside "Take the tour" (there for every account that can review, booth or
  not) lights up 30 seconds after the tour ends (or the welcome card is closed), with a card under it: "Want to see how our experts
  confirm that the AI is correctly labeling our fish?" ("Show me →" opens `/review?tour=review`, "Not now"
  closes it). No overlay, once per visitor;
- 90 seconds without a touch after someone has used it sends the browser back to the home page and the card;
- the video never takes app.js's four-hour "still watching?" stop;
- the frame-rate numbers, the operators' alerts, "Input drift", the account chip (and its sign-out) and the
  theme picker are hidden, and on `/review` so are Browse, the date window, drawing, single boxes and the
  keyboard hints; the review page always opens on whole frames;
- on staging (`ENV=staging`) the review page says its answers never reach the real data: staging's database
  is its own, and only production's answers train the AI.

While `deploy/gpu-host/replay.conf` names a recording for the environment, the page and the tours say so
(historical footage, the pier) instead of "live". A replaying environment records nothing, so its 24-hour
panels empty a day after the recording starts; the home tour uses the Reports panel over 7 days.

---

## Common issues

### "The site shows a maintenance page"

The site was stopped (the cost dashboard's stop button) or is still coming
back. Start it with the infra README's "Restart the AI site after a stop".
If AWS has no GPU instance free, `restart_ai` says so and changes nothing;
retry every 15 to 30 minutes.

### "Dashboard loaded but everything is empty"

Hard-reload (`Cmd+Shift+R` / `Ctrl+Shift+R`). The static asset URLs
include a cache-busting `?v=<hash>` query param, so a normal reload
usually picks up changes — but a hard-reload is the guaranteed-clean
fix.

On a fresh install there is nothing to show until a worker writes or the
sample is loaded (`make seed` with compose, `python -m app.seed` on bare
metal). The fish counter (`species_sightings_mat`) only moves when the
`refresh_matviews` job runs (every 30 minutes on AWS staging; `run_jobs.sh`
on bare metal), since the dashboard holds no refreshers of its own. While
staging plays a recording nothing is recorded, so its 24-hour panels are
empty a day after the recording starts.

### "Worker isn't writing detections"

Check `/api/stats`:

- a recording is playing (`deploy/gpu-host/replay.conf` names one for the
  environment) → nothing is written, by design ("Where it runs").
- `running: false` → worker crashed; read its log (the GPU host's log
  group on AWS).
- `autoswitch.is_dark: true` → the live camera is dark (sunset / lens
  blocked / cable issue), pipeline is in fallback mode and
  **deliberately not writing video data.** The dashboard banner says
  so. Resolves on its own when light returns.
- `frames_seen` not advancing → primary video source unreachable. On
  AWS MediaMTX pulls the camera, so its lines in the GPU host's log group
  say so first. Try the URL from the GPU host with
  `ffprobe -rtsp_transport tcp -i '<url>'` (the `encoder` container has
  `ffprobe`). The camera's URL and credentials live in the secret
  `/ai/wahoobay/<env>/cameras` (key `seahivecam`); if they've rotated,
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

### "Throughput dropped below the camera's native fps"

Camera streams at 15 fps but workers are only inferring at 13 fps? Three
candidate culprits, in order of likelihood:

1. **GPU contention** — on the GPU host the worker shares the L4 with
   the NVENC encoder only (9 % GPU and 1 % NVENC at bring-up); check
   `nvidia-smi` there. (On the shared DGX it was other people's jobs.)
2. **JPEG re-encode bottleneck on the raw stream** — at high
   `JPEG_QUALITY` and high resolution, the cv2 imencode call inside
   FrameTap can cap the grab rate. Lower `JPEG_QUALITY` or drop
   resolution.
3. **Slot starvation in the shm pool** — log line "save queue full"
   means inference is fast but disk I/O isn't keeping up; raise the
   queue or reduce save cadence.

### "v2 classifier load fails: missing checkpoint"

Worker log shows `FileNotFoundError: .../classifier_v2/checkpoint_best.pt`.
On AWS the worker's entrypoint fetches the weights from the models bucket
(`scripts/fetch_models.py`) into `/opt/wahoobay-data/models` on the host;
a failed fetch or checksum shows in its log just before this error. Locally:
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
`/admin/regions.html`, Save, restart the workers. On AWS the worker logs
`config … not found, disabled` instead: see "Polygon mask for
false-positive geometry".

### "Sheet poller flagged 'sheet_changed' but I didn't edit anything"

Google's web UI re-compresses embedded images on save, even when no
cell data changed. The poller sees the bytes change and dutifully
rebuilds, but the resulting JSON files are byte-identical. Not a bug —
just noise on the channel. Spot-check via:

```bash
diff <(git show HEAD:assets/species.xlsx | sha256sum) <(sha256sum assets/species.xlsx)
```

---

## Storage and backups

- **Frames**: S3 `ai-wahoobay-staging-frames`, keys
  `<location>/YYYY/MM/DD/HH/<name>.jpg`. The bucket has no lifecycle rule,
  so nothing expires (checked 2026-11-23).
- **Database**: Aurora's automated backups, kept 7 days, with
  point-in-time restore inside that window (checked 2026-11-23). A restore
  makes a new cluster; pointing the stack at it is an infra change.
- **Reviewers' work**: the `archive_labels` job (every 30 minutes) copies
  the verdicts, drawn boxes, skips and frames of the accounts infra lists in
  `ai.label_archive_accounts` to
  `s3://platform-wahoobay-models/labels/<env>/<account slug>/`, with a new
  snapshot only when something changed.
- **On the GPU host**: the weights under `/opt/wahoobay-data/models`
  (fetched again if lost), the event log under
  `/opt/wahoobay-data/logs/events/`, recordings under
  `/opt/wahoobay-data/replay/`.

The DGX's nightly `pg_dump` and labels CSV stopped with the DGX
([`dgx-history.md`](dgx-history.md), "Backups").

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

When you cut a new classifier checkpoint on AWS, publish it to the models
bucket and have the worker fetch it, as `scripts/fetch_models.py`
describes; it re-hashes when it starts. On bare metal:

1. Drop the new `checkpoint_best.pt` + `classes.json` + `config.json`
   into `data/models/classifier_v2/`.
2. Restart the workers. They re-hash on boot, so the new
   `classifier_sha256` shows up on `/api/provenance/current` within
   ~30 s.
3. The old `classifier_sha256` is still on every historical row, so
   you can filter pre-/post-cutover detections in the DB by that hash.
