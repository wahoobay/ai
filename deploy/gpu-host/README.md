# The GPU host

What runs on the AWS GPU host (`ai-wahoobay-<env>-gpu`, a `g6.xlarge` the infra stack creates when
`ai.gpu_host.<env>` is on): the camera worker, on the host's GPU, MediaMTX, and Caddy as the stream
proxy CloudFront reaches. The design is the infra repository's
`docs/superpowers/specs/2026-10-25-phase1-gpu-and-migration-design.md` §4.1.

| File | Role |
|---|---|
| `docker-compose.yml` | The worker (image `ai-wahoobay-worker:<env>` from ECR, the one CI builds) MediaMTX and Caddy; logs to CloudWatch `/ai/<location>/<env>/gpu-host` |
| `Caddyfile` | Port 8080 for CloudFront's VPC origin: 403 without the `X-Origin-Verify` header, `/stream/*` to the worker unbuffered, `/hls/*` to MediaMTX (prefix stripped) |
| `mediamtx.yml` | MediaMTX: RTSP and HLS settings and the two paths (below) |
| `boot.sh` | Reads `/etc/wahoobay/host.env` (written by the stack) and three secrets, writes `.env` (mode 600), ECR login, `compose up`, waits for `/healthz` |
| `update.sh <ref>` | `git checkout <ref>` then `boot.sh`; what `scripts/deploy_env.sh` runs here through SSM |
| `wahoobay-gpu-host.service` | systemd unit: `boot.sh` at every boot, retried on failure |

| Service | Ports | Reached by |
|---|---|---|
| `mediamtx` | 8554 RTSP, 8888 HLS | compose network only (the worker, the encoder, Caddy); no host ports |
| `worker` | 8081 | the dashboard, the Lambdas and Caddy (security group) |
| `caddy` | 8080 | CloudFront's VPC origin |

The camera is pulled once, by MediaMTX (path `seahive`, source from `CAMERA_SEAHIVE_URL` in `.env`). The worker
reads `rtsp://mediamtx:8554/seahive` and nothing else. The path `seahive_boxes` is published by the encoder.
Viewers fetch plain HLS at `/hls/<path>/index.m3u8` (through CloudFront and Caddy). `boot.sh` ends with an
`hls: <path> ready` or `hls: <path> not yet` line per path; it is a report, never a failure.

Nothing here holds a secret at rest except `.env`, which `boot.sh` rewrites at every run from Secrets
Manager (the database secret the stack generated, `/ai/<location>/<env>/cameras` that Dan created, the
origin-verify secret). Administration is `aws ssm start-session --target <instance id>`; there is no SSH.
The host is not reachable from the internet: the dashboard and the jobs Lambda reach the worker's API
inside the VPC, and viewers reach the stream through CloudFront.
