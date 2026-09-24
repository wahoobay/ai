#!/usr/bin/env python
"""Try the review page on real footage without touching production.

Copies a small, evenly spread sample of saved frames and their detections
from the live database (a genuinely read-only session -- see `seed`) into
the test database, creates two demo accounts, and writes their passwords
to a mode-600 file. The frames stay where they are on disk; only rows are
copied. The target must not be the production database, and nothing is
ever written back to it.

    python scripts/dev/seed_review_demo.py --frames 300      # seed
    python scripts/dev/seed_review_demo.py --serve           # run a throwaway dashboard

--serve runs the dashboard in this process on 127.0.0.1:18090 against the
test database, with the sheet poller, workers and mail switched off, and
the pool topping up every 60 s. Stop it with Ctrl-C (or a plain `kill` of
its PID). Running the test suite truncates the same database, so do not
run both at once.
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb

REPO = Path(__file__).resolve().parents[2]
MAIN_CHECKOUT = Path("/raid/scratch/dzimmerman2021/wahoobay")
sys.path.insert(0, str(REPO / "services" / "dashboard"))


def _env_value(path: Path, key: str) -> str:
    for line in path.read_text().splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip().strip("'\"")
    raise SystemExit(f"{key} not found in {path}")


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, text.encode())
    os.close(fd)


def _target_dsn(dsn_file: Path) -> str:
    dsn = dsn_file.read_text().strip()
    if conninfo_to_dict(dsn).get("dbname") == "wahoobay":
        raise SystemExit("refusing to use the production database as the demo target")
    return dsn


def seed(source_env: Path, target: str, n_frames: int) -> None:
    from app.auth import hash_password

    cols = ("id", "ts", "frame_id", "source_name", "reason", "image_path", "coco_path",
            "num_fish")
    # The source connection must be genuinely read-only, not merely asked
    # nicely: `read_only` has to be set before this connection runs any
    # statement at all (psycopg refuses to change it mid-transaction), so
    # that the *first* statement -- the SHOW below, which the code then
    # checks rather than trusting -- already runs inside a read-only
    # transaction. (A bare `SET default_transaction_read_only = on` as the
    # first statement would not do this: that statement's own transaction,
    # already begun, is unaffected, and only transactions started after it
    # would be read-only.) Never writes here, and never prints this DSN.
    with psycopg.connect(_env_value(source_env, "DATABASE_URL")) as src:
        src.read_only = True
        status = src.execute("SHOW transaction_read_only").fetchone()[0]
        if status != "on":
            raise SystemExit(
                f"refusing to read the source database: transaction_read_only={status!r}, not 'on'")
        src.execute("SET statement_timeout = '60s'")
        lo, hi = src.execute("SELECT min(ts), max(ts) FROM saved_frames").fetchone()
        step = (hi - lo) / max(1, n_frames)
        frames, seen = [], set()
        for i in range(n_frames):
            row = src.execute(f"""SELECT {', '.join(cols)} FROM saved_frames
                                   WHERE ts <= %s AND num_fish > 0
                                   ORDER BY ts DESC LIMIT 1""", (lo + step * (i + 1),)).fetchone()
            if row and row[0] not in seen:
                seen.add(row[0])
                frames.append(dict(zip(cols, row, strict=True)))
        events = []
        for f in frames:
            events += src.execute(
                """SELECT id, ts, frame_id, source_name, det_conf, bbox, topk, best_name,
                          best_species_id, best_accuracy, track_id
                     FROM detection_events
                    WHERE source_name = %s AND ts = %s AND frame_id = %s""",
                (f["source_name"], f["ts"], f["frame_id"])).fetchall()
    print(f"sampled {len(frames)} frames with {len(events)} detections")

    passwords = {}
    with psycopg.connect(target) as dst:
        dst.execute("""TRUNCATE review_skips, review_frames, review_added_boxes, review_settings,
                                review_items, detection_corrections, detection_events,
                                saved_frames RESTART IDENTITY CASCADE""")
        with dst.cursor() as cur:
            cur.executemany(
                f"""INSERT INTO saved_frames ({', '.join(cols)})
                    VALUES ({', '.join('%(' + c + ')s' for c in cols)})""", frames)
            cur.executemany(
                """INSERT INTO detection_events (id, ts, frame_id, source_name, det_conf, bbox,
                                                 topk, best_name, best_species_id,
                                                 best_accuracy, track_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                [(*e[:6], Jsonb(e[6]), *e[7:]) for e in events])
        for username, role in (("demo_reviewer", "reviewer"), ("demo_admin", "admin")):
            passwords[username] = secrets.token_urlsafe(12)
            dst.execute(
                """INSERT INTO users (username, display_name, email, password_hash, role, status)
                   VALUES (%s, %s, %s, %s, %s, 'active')
                   ON CONFLICT (username) DO UPDATE
                      SET password_hash = EXCLUDED.password_hash, role = EXCLUDED.role,
                          status = 'active', failed_logins = 0, locked_until = NULL""",
                (username, username.replace("_", " ").title(), f"{username}@example.org",
                 hash_password(passwords[username]), role))
        dst.commit()
    creds = REPO / ".superpowers" / "demo_credentials"
    _write_private(creds, "".join(f"{u}: {p}\n" for u, p in passwords.items()))
    print(f"demo accounts written to {creds} (mode 600)")


def serve(target: str, port: int) -> None:
    os.environ.update({
        "DATABASE_URL": target,
        "DASHBOARD_HOST": "127.0.0.1",
        "DASHBOARD_PORT": str(port),
        "DASHBOARD_COOKIE_SECURE": "false",
        "SHEET_URL": "",
        "WORKER_URL": "http://127.0.0.1:1",
        "POLLER_URL": "http://127.0.0.1:1",
        "DASHBOARD_MAIL_BACKEND": "manual",
        "REVIEW_POOL_TOPUP_INTERVAL_S": "60",
        "REVIEW_ASSETS_DIR": str(MAIN_CHECKOUT / "assets"),
        "REVIEW_TAXONOMY_CSV": str(REPO / "assets" / "taxonomy.csv"),
        "CLASSIFIER_V2_CLASSES_JSON": str(MAIN_CHECKOUT / "data/models/classifier_v2/classes.json"),
        "REVIEW_BASELINE_MANIFEST": str(MAIN_CHECKOUT / "data/fine_tune/_dataset_manifest.json"),
    })
    from app.main import main as run_dashboard

    run_dashboard()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source-env", type=Path, default=MAIN_CHECKOUT / ".env")
    ap.add_argument("--target-dsn-file", type=Path, default=REPO / ".superpowers" / "test_dsn")
    ap.add_argument("--frames", type=int, default=300)
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--port", type=int, default=18090)
    args = ap.parse_args()
    target = _target_dsn(args.target_dsn_file)
    if args.serve:
        serve(target, args.port)
    else:
        seed(args.source_env, target, args.frames)
    return 0


if __name__ == "__main__":
    sys.exit(main())
