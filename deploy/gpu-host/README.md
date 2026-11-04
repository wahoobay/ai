# The GPU host

What runs on the AWS GPU host (`ai-wahoobay-<env>-gpu`, a `g6.xlarge` the infra stack creates when
`ai.gpu_host.<env>` is on): the camera worker, on the host's GPU, MediaMTX, the encoder, and Caddy as the stream
proxy CloudFront reaches. The design is the infra repository's
`docs/superpowers/specs/2026-10-25-phase1-gpu-and-migration-design.md` §4.1.

| File | Role |
|---|---|
| `docker-compose.yml` | The worker (image `ai-wahoobay-worker:<env>` from ECR, the one CI builds), MediaMTX, the encoder and Caddy; logs to CloudWatch `/ai/<location>/<env>/gpu-host` |
| `Caddyfile` | Port 8080 for CloudFront's origin on the host (a public origin on the host's address, authenticated by the header): 403 without the `X-Origin-Verify` header, `/stream/*` to the worker unbuffered, `/hls/*` to MediaMTX (prefix stripped) |
| `encoder.sh` | The encoder's loop: ffmpeg reads the worker's `/stream.mjpeg`, encodes with `h264_nvenc`, publishes `seahive_boxes`; restarts 5 s after ffmpeg ends |
| `mediamtx.yml` | MediaMTX: RTSP and HLS settings and the two paths (below) |
| `boot.sh` | Reads `/etc/wahoobay/host.env` (written by the stack) and three secrets, writes `.env` (mode 600), ECR login, `compose up`, waits for `/healthz` |
| `update.sh <ref>` | `git checkout <ref>` then `boot.sh`; what `scripts/deploy_env.sh` runs here through SSM |
| `replay.conf` | Per environment, the recording that plays in the camera's place, or nothing for the camera ("Replay" below) |
| `wahoobay-gpu-host.service` | systemd unit: `boot.sh` at every boot, retried on failure |

| Service | Ports | Reached by |
|---|---|---|
| `mediamtx` | 8554 RTSP, 8888 HLS | compose network only (the worker, the encoder, Caddy); no host ports |
| `worker` | 8081 | the dashboard, the Lambdas and Caddy (security group) |
| `encoder` | none | publishes to `mediamtx` (RTSP over TCP); reads the worker on 8081 |
| `caddy` | 8080 | CloudFront's origin on the host |
| `replay` | none | only while `replay.conf` names a recording: publishes it to `mediamtx` as `seahive` |

The camera is pulled once, by MediaMTX (path `seahive`, source from `SEAHIVE_SOURCE` in `.env`). The worker
reads `rtsp://mediamtx:8554/seahive` and nothing else. The path `seahive_boxes` is published by the encoder.
Viewers fetch plain HLS at `/hls/<path>/index.m3u8` (through CloudFront and Caddy). `boot.sh` ends with an
`hls: <path> ready` or `hls: <path> not yet` line per path (ready means a 200), and `hls: seahive_boxes
skipped` when `seahive` is not ready; it is a report, never a failure.

MediaMTX serves HLS in CDN mode. Since 1.21.1 it gives each viewer its own HLS session, carried by a cookie
and a redirect, and those cannot cross a cache that forwards no cookies or query strings, as CloudFront's
policies here do. A request with `Authorization: Bearer <hlsCDNSecret>` instead shares one session per path,
with plain cacheable responses; Caddy adds that bearer on `/hls/*`. The secret (`HLS_CDN_SECRET`) is derived
on the host by `boot.sh` from the origin secret (its SHA-256) and lives only in `.env` and the environments of
the `mediamtx` (`MTX_HLSCDNSECRET`) and `caddy` containers.

Nothing here holds a secret at rest except `.env`, which `boot.sh` rewrites at every run from Secrets
Manager (the database secret the stack generated, `/ai/<location>/<env>/cameras` that Dan created, the
origin-verify secret). Administration is `aws ssm start-session --target <instance id>`; there is no SSH.
The host is not reachable from the internet: the dashboard and the jobs Lambda reach the worker's API
inside the VPC, and viewers reach the stream through CloudFront.

## The encoder

`encoder` (`jrottenberg/ffmpeg:7.1.2-nvidia2404`) runs `encoder.sh`: one NVENC encode of the worker's annotated
MJPEG, published to `rtsp://mediamtx:8554/seahive_boxes` at a constant 15 fps with one IDR per second. It shares
the L4 with the worker (`capabilities: [gpu, video]`, `NVIDIA_DRIVER_CAPABILITIES: compute,video,utility`).
The frames are stamped from the wall clock in the filter graph, not by the demuxer: ffmpeg's MJPEG demuxer calls
every stream 25 fps whatever the arrival rate, and through decode and encode that clock wins (the first bring-up
saw the worker's 15 fps play at 0.63× real time in 2.4-second segments). `boot.sh` writes the hash of each
bind-mounted file into `.env` (`ENCODER_SCRIPT_SHA`, `MEDIAMTX_CONFIG_SHA`, `CADDYFILE_SHA`) as a label on its
service, so compose recreates that container when the file changes. Without the label a changed file never
reaches the running container: git replaces the file, the single-file bind mount keeps the old inode, and a
running shell loop has parsed its script anyway (the bring-up's two-second segments did not apply until MediaMTX
was recreated by hand). Compose also recreates what depends on a recreated service (at the bring-up, compose 5.6
recreated the encoder along with the worker at every image deploy), so a changed `mediamtx.yml` can take the
worker and the encoder down with MediaMTX for the usual deploy minute; `boot.sh` waits for the worker's
`/healthz` as always, the worker reconnects to MediaMTX on its own (drill: six seconds) and the encoder's loop
reconnects to both.

Knobs (environment of the `encoder` service; defaults in the script): `ENCODER_FPS` (`15`, an integer; the GOP
follows it, so an IDR still comes every second, which divides any whole-second segment length; probed only at
15, so probe another value on the host before using it), `ENCODER_BITRATE` (`3000k`), `ENCODER_MAXRATE` (`4000k`), `ENCODER_BUFSIZE`
(`8000k`), `WORKER_URL` (`http://worker:8081`), `MEDIAMTX_RTSP` (`rtsp://mediamtx:8554`). `ENCODER_ONCE=1` is a
test hook; compose never sets it.

## Replay

An environment can show a recording instead of the camera. `replay.conf` names, per environment, an MP4 in the
models bucket (`platform-wahoobay-models`, under `replay/`); empty means the camera. With a recording named,
`boot.sh` fetches it to `/opt/wahoobay-data/replay/` (once: a recording keeps its name for life, and a new one
gets a new key), writes `SEAHIVE_SOURCE=publisher`, `COMPOSE_PROFILES=replay` and `PERSIST_ENABLED=false` into
`.env`, and the `replay` service (ffmpeg, `-re -stream_loop -1 -c copy`) publishes the file to the `seahive`
path, looped, at its own pace. Everything downstream is unchanged: the worker detects and the encoder burns
in the boxes, and both variants reach CloudFront as usual. The worker records nothing (no detections, frame stats,
saved frames or event log), because a recording's fish are not today's fish. The pages still say Live (Dan's choice,
2026-11-04). A recording that cannot be fetched fails the boot before anything changes, so the running stack stays
as it was. Emptying the line puts the camera back: MediaMTX pulls it again, and `boot.sh` removes the replay
container (Compose leaves a running service of an inactive profile alone, even with `--remove-orphans`).

The file should have the camera's shape, so the worker, the HLS segmenter and the 2 s segments behave as they do
live: H.264, 1280×720, 15 fps, no B-frames, an IDR every second, no audio. To make one from a Spot AI export
(`-ss` is the offset of the hour wanted within the export), with the replay service's own image:

```sh
docker run --rm --platform linux/amd64 -v "$PWD":/w -w /w jrottenberg/ffmpeg:7.1.2-ubuntu2404 \
  -ss 00:00:00 -i spot-export.mp4 -t 3600 -an -vf "scale=1280:720,fps=15" \
  -c:v libx264 -preset medium -crf 23 -maxrate 2000k -bufsize 4000k \
  -g 15 -keyint_min 15 -sc_threshold 0 -bf 0 -pix_fmt yuv420p -movflags +faststart seahive-2026-09-01.mp4
aws s3 cp seahive-2026-09-01.mp4 s3://platform-wahoobay-models/replay/seahive-2026-09-01.mp4
```

Upload first, then merge the `replay.conf` change; the deploy then runs `boot.sh`. While a recording plays, the
alert rules `pipeline_silence` and `frame_stats_stalled` fire on the dashboard, because nothing is written for them
to read. They send no mail and resolve once the camera is back. The loop's seam (the file's last frame jumping to its
first) shows once per loop; a recording that starts and ends on a quiet reef hides it.

## Segment length: the streaming-cost knob

`hlsSegmentDuration` in `mediamtx.yml` (**2 s** since 2026-10-31; segments are cut at the first IDR after it, and
the camera and the encoder both send one every second) sets what a viewer costs. CloudFront serves the stream
on the flat-rate Pro plan (50 TB and 10 M requests a month), and requests, not bytes, are the allowance: the
raw variant is 0.19 GB per viewer-hour and the boxed one about 1.1 GB, but an hls.js viewer fetches one
playlist and one segment per segment length. Measured at bring-up:

| Segments | Requests per viewer-hour | Viewer-hours in 10 M | Delay (2-segment buffer) |
|---|---|---|---|
| 1 s | 7,200 (ffmpeg client 9,600) | 1,400 | 3–4 s |
| 2 s | 3,600 | 2,800 | 5–6 s |
| 4 s | 1,800 | 5,600 | 9–10 s |
| 6 s | 1,200 | 8,300 | 13–14 s |

Longer segments change only time (delay, time to first picture, time to switch camera or boxes, recovery after
an outage), never smoothness or picture quality. The dashboard's player keeps two segments of buffer
(`liveSyncDurationCount` in `static/app.js`), stops loading, and stops the dashboard's pollers with it, while
its tab is hidden and after four hours without a pointer, key, touch or wheel ("still watching?"), so an
unattended tab costs nothing. The pollers matter: the live panel alone asks the API twice every half second,
about 14,000 requests an hour per open dashboard tab, four times the video. Exceeding the allowance costs
nothing either (AWS tolerates a 3× month, then emails at 50/80/100 % and, if it goes on for months, serves from
more distant edges); the infra stack alarms when a day passes the allowance's daily pace. **Dan's standing
instruction (2026-10-31): if streaming cost or the request allowance ever becomes an issue, this knob is raised
with him first, not changed quietly.**

Measure at bring-up, with the dashboard watching the boxed variant:

- `docker compose stats encoder` for its CPU and memory;
- `nvidia-smi dmon -s u` for encoder (`enc`) utilisation next to the worker's `sm`;
- the bitrate of each variant (`seahive`, `seahive_boxes`): size the segments fetched from `/hls/<path>/`.

Record the numbers in the plan's close-out before changing the knobs.
