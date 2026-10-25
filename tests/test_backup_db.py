"""scripts/dev/backup_db.sh against the test database (docs/operations.md,
"Backups")."""

from __future__ import annotations

import csv
import gzip
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from review_fixtures import TEST_DSN, add_verdict, make_user, seed_frame

pytestmark = pytest.mark.anyio
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dev" / "backup_db.sh"


def _pg_restore() -> str:
    """pg_restore from PG_BIN, the PATH, or the DGX conda environment (where the
    backup cron runs); the backup script finds pg_dump the same way."""
    for candidate in (
        Path(os.environ["PG_BIN"]) / "pg_restore" if os.environ.get("PG_BIN") else None,
        Path(shutil.which("pg_restore")) if shutil.which("pg_restore") else None,
        Path("/raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/pg_restore"),
    ):
        if candidate and candidate.exists():
            return str(candidate)
    pytest.skip("pg_restore not found (set PG_BIN)")


PG_RESTORE = _pg_restore() if TEST_DSN else ""


def _run(tmp_path, **env):
    full = {
        **os.environ,
        "BACKUP_DATABASE_URL": TEST_DSN,
        "BACKUP_DIR": str(tmp_path / "main"),
        "BACKUP_COPY_DIR": str(tmp_path / "copy"),
        "PYTHON": sys.executable,
        **env,
    }
    return subprocess.run(
        ["bash", str(SCRIPT)], env=full, capture_output=True, text=True, timeout=300
    )


async def test_the_backup_keeps_labels_and_small_tables_but_not_the_camera_bulk(rdb, tmp_path):
    f = await seed_frame(
        rdb, tmp_path, boxes=[{"bbox": [10, 10, 110, 110], "species": "Seriola zonata"}]
    )
    who = await make_user(rdb, "rev")
    await add_verdict(rdb, f["event_ids"][0], who, name="Seriola zonata")
    r = _run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert " ok " in r.stdout and "labels=1 " in r.stdout
    dump = next((tmp_path / "main").glob("wahoobay-*.dump"))
    labels = next((tmp_path / "main").glob("labels-*.csv.gz"))
    for p in (dump, labels):
        assert stat.S_IMODE(p.stat().st_mode) == 0o600, "backups hold emails and password hashes"
        assert (tmp_path / "copy" / p.name).exists(), "a copy goes to the second disk"
    listing = subprocess.run(
        [PG_RESTORE, "--list", str(dump)], capture_output=True, text=True, timeout=60
    ).stdout
    for kept in ("detection_corrections", "users", "review_items", "sensor_readings"):
        assert re.search(rf"TABLE DATA public {kept}\b", listing), f"{kept} data must be kept"
    for bulk in ("detection_events", "saved_frames", "frame_stats"):
        assert not re.search(
            rf"TABLE DATA public {bulk}\b", listing
        ), f"{bulk} data is re-derivable"
        assert re.search(rf"TABLE public {bulk}\b", listing), f"{bulk}'s schema is kept"
    rows = list(csv.DictReader(gzip.open(labels, "rt", newline="")))
    assert len(rows) == 1
    assert rows[0]["event_id"] == str(f["event_ids"][0])
    assert rows[0]["source_name"] == f["source"] and rows[0]["image_path"]
    assert rows[0]["model_species"] == "Seriola zonata"


async def test_old_backups_are_pruned_from_both_places(rdb, tmp_path):
    old_time = time.time() - 40 * 86400
    for d in ("main", "copy"):
        (tmp_path / d).mkdir()
        old = tmp_path / d / "wahoobay-20200101-000000.dump"
        old.write_bytes(b"x")
        other = tmp_path / d / "notes.txt"
        other.write_text("not a backup")
        for p in (old, other):
            os.utime(p, (old_time, old_time))
    r = _run(tmp_path, BACKUP_KEEP_DAYS="30")
    assert r.returncode == 0, r.stderr
    for d in ("main", "copy"):
        assert not (tmp_path / d / "wahoobay-20200101-000000.dump").exists()
        assert (tmp_path / d / "notes.txt").exists(), "only backup files are pruned"
        assert len(list((tmp_path / d).glob("wahoobay-*.dump"))) == 1, "tonight's dump stays"


async def test_by_default_there_is_no_second_copy(rdb, tmp_path):
    # 2026-10-01: the home directory is on the nearly full root disk and must
    # hold no project data, so a copy is made only when BACKUP_COPY_DIR is set.
    env = {k: v for k, v in os.environ.items() if k != "BACKUP_COPY_DIR"}
    env.update(
        BACKUP_DATABASE_URL=TEST_DSN, BACKUP_DIR=str(tmp_path / "main"), PYTHON=sys.executable
    )
    r = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    assert r.stdout.rstrip().endswith(str(tmp_path / "main")), "no '+ <copy>' in the log line"
    assert "beegfs" not in SCRIPT.read_text()
