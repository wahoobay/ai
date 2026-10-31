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
| `wahoobay-gpu-host.service` | systemd unit: `boot.sh` at every boot, retried on failure |

| Service | Ports | Reached by |
|---|---|---|
| `mediamtx` | 8554 RTSP, 8888 HLS | compose network only (the worker, the encoder, Caddy); no host ports |
| `worker` | 8081 | the dashboard, the Lambdas and Caddy (security group) |
| `encoder` | none | publishes to `mediamtx` (RTSP over TCP); reads the worker on 8081 |
| `caddy` | 8080 | CloudFront's origin on the host |

The camera is pulled once, by MediaMTX (path `seahive`, source from `CAMERA_SEAHIVE_URL` in `.env`). The worker
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
MJPEG, published to `rtsp://mediamtx:8554/seahive_boxes`, one keyframe per second. It shares the L4 with the
worker (`capabilities: [gpu, video]`, `NVIDIA_DRIVER_CAPABILITIES: compute,video,utility`).

Knobs (environment of the `encoder` service; defaults in the script): `ENCODER_BITRATE` (`3000k`),
`ENCODER_MAXRATE` (`4000k`), `ENCODER_BUFSIZE` (`8000k`), `WORKER_URL` (`http://worker:8081`),
`MEDIAMTX_RTSP` (`rtsp://mediamtx:8554`). `ENCODER_ONCE=1` is a test hook; compose never sets it.

Measure at bring-up, with the dashboard watching the boxed variant:

- `docker compose stats encoder` for its CPU and memory;
- `nvidia-smi dmon -s u` for encoder (`enc`) utilisation next to the worker's `sm`;
- the bitrate of each variant (`seahive`, `seahive_boxes`): size the segments fetched from `/hls/<path>/`.

Record the numbers in the plan's close-out before changing the knobs.
