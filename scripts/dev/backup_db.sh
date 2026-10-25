#!/usr/bin/env bash
# Nightly backup of the data that can't be re-derived from the cameras
# (docs/operations.md, "Backups"):
#   wahoobay-<stamp>.dump   pg_dump -Fc: the whole schema, and every table's data
#                           except the camera-derived bulk (detection_events,
#                           saved_frames, frame_stats; matview data is never
#                           dumped). Accounts, verdicts, review tables, alerts,
#                           sensor and weather history: ~10 MB.
#   labels-<stamp>.csv.gz   every verdict joined with its detection and saved
#                           frame, so volunteers' work stays usable even if
#                           detection_events itself were lost.
# Files are 0600 (emails, password hashes). Set BACKUP_COPY_DIR to also copy
# them somewhere else; by default there is no second copy (the home directory
# is on the nearly full root disk and must hold no data, 2026-10-01). Files
# older than BACKUP_KEEP_DAYS are pruned. One log line per run.
#
# Cron (the user's crontab): 30 2 * * * <repo>/scripts/dev/backup_db.sh >> <repo>/logs/app/backup.log 2>&1
# Overrides (tests): BACKUP_DATABASE_URL, BACKUP_DIR, BACKUP_COPY_DIR (empty: no copy),
# BACKUP_KEEP_DAYS.
set -euo pipefail
umask 077

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
BIN=/raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin
if [[ -n "${BACKUP_DATABASE_URL:-}" ]]; then
  DSN="$BACKUP_DATABASE_URL"
else
  DSN="$(grep '^DATABASE_URL=' "$REPO/.env" | cut -d= -f2- | tr -d "\"'")"
fi
DIR="${BACKUP_DIR:-/raid/scratch/dzimmerman2021/backups/wahoobay-db}"
COPY="${BACKUP_COPY_DIR:-}"
KEEP="${BACKUP_KEEP_DAYS:-30}"
STAMP="$(date +%Y%m%d-%H%M%S)"

mkdir -p "$DIR"
DUMP="$DIR/wahoobay-$STAMP.dump"
LABELS="$DIR/labels-$STAMP.csv.gz"
trap 'rm -f "$DUMP.part" "$LABELS.part"' EXIT

"$BIN/pg_dump" "$DSN" --format=custom --no-owner \
  --exclude-table-data=detection_events \
  --exclude-table-data=saved_frames \
  --exclude-table-data=frame_stats \
  --file="$DUMP.part"
mv "$DUMP.part" "$DUMP"
"$BIN/pg_restore" --list "$DUMP" > /dev/null          # the catalog reads back

"$BIN/psql" "$DSN" -X -q -v ON_ERROR_STOP=1 -c "
  COPY (
    SELECT c.*, de.source_name, de.ts AS event_ts, de.frame_id, de.bbox AS model_bbox,
           de.best_species_id AS model_species, de.best_accuracy AS model_p,
           sf.image_path
      FROM detection_corrections c
      LEFT JOIN detection_events de ON de.id = c.event_id
      LEFT JOIN LATERAL (
            SELECT image_path FROM saved_frames s
             WHERE s.source_name = de.source_name AND s.ts = de.ts
               AND s.frame_id = de.frame_id
             ORDER BY s.id LIMIT 1) sf ON true
     ORDER BY c.id
  ) TO STDOUT WITH (FORMAT csv, HEADER)" | gzip > "$LABELS.part"
mv "$LABELS.part" "$LABELS"

# Count CSV records (notes may hold newlines, so not lines) against the table.
ROWS="$("$BIN/python" -c 'import csv, gzip, sys
print(sum(1 for _ in csv.reader(gzip.open(sys.argv[1], "rt", newline=""))) - 1)' "$LABELS")"
EXPECT="$("$BIN/psql" "$DSN" -X -A -t -c "SELECT count(*) FROM detection_corrections")"
if [[ "$ROWS" != "$EXPECT" ]]; then
  echo "$(date -Is) FAILED labels has $ROWS rows, detection_corrections has $EXPECT" >&2
  exit 1
fi

if [[ -n "$COPY" ]]; then
  mkdir -p "$COPY"
  cp -p "$DUMP" "$LABELS" "$COPY/"
fi
for d in "$DIR" ${COPY:+"$COPY"}; do
  find "$d" -maxdepth 1 -type f \( -name 'wahoobay-*.dump' -o -name 'labels-*.csv.gz' \) \
    -mtime +"$KEEP" -delete
done

echo "$(date -Is) ok dump=$(du -h "$DUMP" | cut -f1) labels=$ROWS -> $DIR${COPY:+ + $COPY}"
