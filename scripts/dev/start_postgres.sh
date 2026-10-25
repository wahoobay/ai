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

# Schema: Alembic brings the database to the current revision. A database that
# predates Alembic (built by the old db/init.sql -- the DGX) is stamped at the
# baseline first, so only the newer revisions run. alembic comes with
# services/dashboard/requirements.txt.
LOCAL_DATABASE_URL="postgresql://wahoobay:wahoobay@127.0.0.1:$PORT/wahoobay"
if psql -h 127.0.0.1 -p "$PORT" -U wahoobay -d wahoobay -tAc \
     "SELECT to_regclass('alembic_version') IS NULL AND to_regclass('detection_events') IS NOT NULL" | grep -q t; then
  DATABASE_URL="$LOCAL_DATABASE_URL" alembic -c db/alembic.ini stamp 0001
fi
DATABASE_URL="$LOCAL_DATABASE_URL" alembic -c db/alembic.ini upgrade head
echo "postgres ready: postgresql://wahoobay:wahoobay@127.0.0.1:$PORT/wahoobay"
