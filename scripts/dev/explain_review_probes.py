#!/usr/bin/env python
"""Print the query plans of the review pool's probes against a database.

Run against the live database before switching the pool on (plan Task
14). Plain EXPLAIN: nothing is executed. Every plan must use index scans
on detection_events and saved_frames; a sequential scan on either table
means a probe would read hundreds of millions of rows.

    python scripts/dev/explain_review_probes.py --env /raid/scratch/dzimmerman2021/wahoobay/.env
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "dashboard"))

from app.review_pool import (  # noqa: E402
    CHECK_PROBE_SQL,
    IDENTIFY_BOXES_SQL,
    IDENTIFY_FRAMES_SQL,
)


def _dsn(env_path: Path) -> str:
    for line in env_path.read_text().splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip().strip("'\"")
    raise SystemExit(f"DATABASE_URL not found in {env_path}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--env", type=Path, required=True, help=".env file holding DATABASE_URL")
    ap.add_argument("--species", default="Abudefduf saxatilis")
    args = ap.parse_args()
    now = datetime.now(UTC)
    t, start = now - timedelta(days=30), now - timedelta(days=200)
    probes = [
        ("check lane", CHECK_PROBE_SQL, (args.species, t, start)),
        ("identify lane: frames", IDENTIFY_FRAMES_SQL, (t, start)),
        ("identify lane: boxes", IDENTIFY_BOXES_SQL, ([1], ["seahivecam"], [t], [1], 0.4)),
    ]
    bad = False
    with psycopg.connect(_dsn(args.env)) as conn:
        # Setting this by SET, after connecting, would run it as the first
        # statement of an already-open read-write transaction -- too late
        # to make that transaction read-only. read_only must be set on the
        # connection before anything else executes on it.
        conn.read_only = True
        status = conn.execute("SHOW transaction_read_only").fetchone()[0]
        if status != "on":
            print(
                f"RESULT: read-only guard failed (transaction_read_only={status}); "
                "refusing to run EXPLAIN against this database"
            )
            return 1
        for title, sql, params in probes:
            plan = "\n".join(r[0] for r in conn.execute("EXPLAIN " + sql, params))
            print(f"== {title}\n{plan}\n")
            if "Seq Scan on detection_events" in plan or "Seq Scan on saved_frames" in plan:
                bad = True
    print("RESULT:", "SEQUENTIAL SCAN FOUND: do not enable the pool" if bad else "index scans only")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
