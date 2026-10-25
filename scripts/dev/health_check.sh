#!/usr/bin/env bash
# Is the stack up? If not, start what is missing. Run every five minutes from
# the user crontab:
#
#   */5 * * * * <repo>/scripts/dev/health_check.sh >> <repo>/logs/app/health_check.log 2>&1
#
# It replaces the @reboot entry, which ran once per boot and only if cron had
# this crontab loaded at that moment (after the October 2026 restore it did
# not, and nothing noticed for a week). A recurring entry gets a new chance
# every five minutes, and its heartbeat shows whether cron runs it at all.
#
# Quiet when everything is up: it rewrites logs/app/health_check.last (the
# heartbeat; its mtime is the last time cron ran this) and prints nothing.
# When a port is down it prints what was missing and runs start_all.sh, which
# starts only what is not listening and appends to logs/app/start_all.log.
#
# Before stopping anything by hand (docs/operations.md: "Stopping",
# "Restarting one service", an upgrade), pause it:
#
#   touch logs/app/MAINTENANCE     # health_check.sh only heartbeats while this exists
#   rm logs/app/MAINTENANCE        # when the stack is back
#
# It never kills a process: a dashboard that listens but fails /healthz is
# reported here, and restarting it stays a by-hand decision.
set -euo pipefail
cd "$(dirname "$0")/../.."
mkdir -p logs/app

# One run at a time: start_all.sh can wait minutes for the GPU driver after boot.
# A skip is logged, never silent: a lock that is never released (a service that
# inherited fd 9, see 9>&- below) would otherwise look exactly like "all is well".
exec 9>logs/app/health_check.lock
flock -n 9 || { echo "$(date -Is) skipped: another run holds logs/app/health_check.lock"; exit 0; }

date -Is > logs/app/health_check.last

[[ -e logs/app/MAINTENANCE ]] && exit 0

listening() { ss -ltn 2>/dev/null | grep -q ":$1 "; }

down=()
listening 5432  || down+=("postgres:5432")
listening 8081  || down+=("worker:8081")
listening 8083  || down+=("worker_pier:8083")
listening 18080 || down+=("dashboard:18080")
listening 8082  || down+=("poller:8082")
# -x with -f: the whole command line must match, so a shell, editor or pgrep
# whose arguments merely mention the path does not count as the jobs loop.
pgrep -x -f '(\S*/)?bash (\S*/)?scripts/dev/run_jobs\.sh' >/dev/null 2>&1 || down+=("run_jobs")

if listening 18080 && ! curl -sf -m 10 http://127.0.0.1:18080/healthz | grep -q '"ok":true'; then
  echo "$(date -Is) dashboard listens on :18080 but /healthz failed; not restarting it"
fi

[[ ${#down[@]} -eq 0 ]] && exit 0

echo "$(date -Is) down: ${down[*]} -> start_all.sh"
# 9>&- closes the lock fd for start_all.sh and everything it launches with nohup;
# a service that inherited it would hold the lock for as long as it lives.
if ./scripts/dev/start_all.sh 9>&- >> logs/app/start_all.log 2>&1; then
  echo "$(date -Is) start_all.sh: ok"
else
  echo "$(date -Is) start_all.sh: exit $? (see logs/app/start_all.log)"
fi
