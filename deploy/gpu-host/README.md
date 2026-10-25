# The GPU host

What runs on the AWS GPU host (`ai-wahoobay-<env>-gpu`, a `g6.xlarge` the infra stack creates when
`ai.gpu_host.<env>` is on): the camera worker, on the host's GPU, and Caddy as the stream proxy
CloudFront reaches. The design is the infra repository's
`docs/superpowers/specs/2026-10-25-phase1-gpu-and-migration-design.md` §4.1.

| File | Role |
|---|---|
| `docker-compose.yml` | The worker (image `ai-wahoobay-worker:<env>` from ECR, the one CI builds) and Caddy; logs to CloudWatch `/ai/<location>/<env>/gpu-host` |
| `Caddyfile` | Port 8080 for CloudFront's VPC origin: 403 without the `X-Origin-Verify` header, `/stream/*` to the worker unbuffered |
| `boot.sh` | Reads `/etc/wahoobay/host.env` (written by the stack) and three secrets, writes `.env` (mode 600), ECR login, `compose up`, waits for `/healthz` |
| `update.sh <ref>` | `git checkout <ref>` then `boot.sh`; what `scripts/deploy_env.sh` runs here through SSM |
| `wahoobay-gpu-host.service` | systemd unit: `boot.sh` at every boot, retried on failure |

Nothing here holds a secret at rest except `.env`, which `boot.sh` rewrites at every run from Secrets
Manager (the database secret the stack generated, `/ai/<location>/<env>/cameras` that Dan created, the
origin-verify secret). Administration is `aws ssm start-session --target <instance id>`; there is no SSH.
The host is not reachable from the internet: the dashboard and the jobs Lambda reach the worker's API
inside the VPC, and viewers reach the stream through CloudFront.
