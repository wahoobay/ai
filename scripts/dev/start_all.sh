#!/usr/bin/env bash
# Bring the stack up from cold: postgres → workers → dashboard → poller,
# the order docs/operations.md gives. Run by the @reboot crontab entry,
# and safe to re-run by hand: a service whose port is already listening
# is left alone.
#
# The public tunnel is deliberately not started. Each start mints a new
# trycloudflare URL and exposes the lab machine, so it stays a by-hand
# step (see "Public tunnel lifecycle" in docs/operations.md).
set -euo pipefail
cd "$(dirname "$0")/../.."

# cron gives a bare environment: no conda, and possibly no $USER.
export USER="${USER:-$(id -un)}"
CONDA_SH="${CONDA_SH:-/raid/scratch/dzimmerman2021/miniconda3/etc/profile.d/conda.sh}"
# shellcheck disable=SC1090
source "$CONDA_SH"
conda activate wahoobay

mkdir -p logs/app
echo "== start_all $(date '+%F %T %Z')"

# Right after boot the NVIDIA driver can lag cron; the workers need it.
for _ in $(seq 60); do
  nvidia-smi -L >/dev/null 2>&1 && break
  sleep 5
done

listening() { ss -ltn 2>/dev/null | grep -q ":$1 "; }

# start_postgres.sh runs the schema migrations (alembic upgrade head), which
# should not run against a live cluster, so only call it when postgres is down.
if listening 5432; then
  echo "postgres: already running"
else
  ./scripts/dev/start_postgres.sh
fi

launch() {  # port, launcher script, log file
  if listening "$1"; then
    echo "$2: already running on :$1"
  else
    # >> not >: the logs carry months of history.
    nohup "./scripts/dev/$2" >> "logs/app/$3" 2>&1 &
    echo "$2: started (pid $!) on :$1"
  fi
}

launch 8081  run_worker.sh      worker.log
launch 8083  run_worker_pier.sh worker_pier.log
launch 18080 run_dashboard.sh   dashboard.log
launch 8082  run_poller.sh      poller.log
