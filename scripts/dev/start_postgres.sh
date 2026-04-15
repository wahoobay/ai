#!/usr/bin/env bash
# Start a local postgres cluster under ./pgdata using the conda-installed binaries.
# Idempotent: creates the cluster + role + db on first run, boots server on each run.
set -euo pipefail

cd "$(dirname "$0")/../.."

ROOT="$(pwd)"
PGDATA="$ROOT/pgdata"
LOG="$ROOT/logs/app/postgres.log"
PORT="${PGPORT:-5432}"

mkdir -p "$(dirname "$LOG")"

if ! command -v initdb >/dev/null 2>&1; then
  echo "initdb not on PATH. Activate the wahoobay conda env first." >&2
  exit 1
fi

if [[ ! -f "$PGDATA/PG_VERSION" ]]; then
  echo "initializing cluster at $PGDATA"
  initdb -D "$PGDATA" -U "$USER" -A trust --encoding=UTF8 --locale=C >/dev/null
  echo "unix_socket_directories = '$ROOT/pgdata'" >> "$PGDATA/postgresql.conf"
  echo "port = $PORT" >> "$PGDATA/postgresql.conf"
  echo "listen_addresses = '127.0.0.1'" >> "$PGDATA/postgresql.conf"
fi

if pg_ctl -D "$PGDATA" status >/dev/null 2>&1; then
  echo "postgres already running"
else
  pg_ctl -D "$PGDATA" -l "$LOG" -o "-p $PORT -h 127.0.0.1 -k $ROOT/pgdata" start
fi

export PGHOST=127.0.0.1 PGPORT="$PORT"

# Ensure role + database exist
psql -h 127.0.0.1 -p "$PORT" -d postgres -tc "SELECT 1 FROM pg_roles WHERE rolname='wahoobay'" | grep -q 1 \
  || psql -h 127.0.0.1 -p "$PORT" -d postgres -c "CREATE ROLE wahoobay LOGIN PASSWORD 'wahoobay' SUPERUSER"

psql -h 127.0.0.1 -p "$PORT" -d postgres -tc "SELECT 1 FROM pg_database WHERE datname='wahoobay'" | grep -q 1 \
  || psql -h 127.0.0.1 -p "$PORT" -d postgres -c "CREATE DATABASE wahoobay OWNER wahoobay"

# Apply schema (idempotent)
psql -h 127.0.0.1 -p "$PORT" -U wahoobay -d wahoobay -f db/init.sql >/dev/null
echo "postgres ready: postgresql://wahoobay:wahoobay@127.0.0.1:$PORT/wahoobay"
