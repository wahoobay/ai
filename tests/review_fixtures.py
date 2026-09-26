"""Fixtures and seeding helpers for the review-tool tests.

tests/conftest.py imports the fixtures so every test module can use
them; test modules import the helpers directly. Nothing here imports
the dashboard's review modules at import time, because conftest imports
this file before those modules exist (Task 1 lands before Task 4)."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "dashboard"))

TEST_DSN = os.environ.get("TEST_DATABASE_URL")
CSRF = {"X-Requested-With": "wahoobay"}
PASSWORD = "a long enough passphrase"
T0 = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)

TRUNCATE_SQL = """
    TRUNCATE review_skips, review_frames, review_added_boxes, review_settings,
             review_items, detection_corrections, detection_events, saved_frames,
             user_sessions, user_tokens, user_audit, users
    RESTART IDENTITY CASCADE
"""

# ---- a small, deliberately awkward species catalogue --------------------
# Pomatomus is misspelled in both the Species Log and the classifier, the
# way the real data is; Neopomacentrus is in the classifier only; Anilocra
# is a Species Log entry WoRMS cannot resolve; French Angelfish has two
# life stages; one Species Log row has no binomial at all.
SPECIES_MANIFEST = [
    {"binomial": "Seriola zonata", "common_name": "Banded Rudderfish",
     "life_stage": "unknown", "family": "Carangidae"},
    {"binomial": "Pomacanthus paru", "common_name": "Adult French Angelfish",
     "life_stage": "adult", "family": "Pomacanthidae"},
    {"binomial": "Pomacanthus paru", "common_name": "Juvenile French Angelfish",
     "life_stage": "juvenile", "family": "Pomacanthidae"},
    {"binomial": "Pomatomus saltarix", "common_name": "Bluefish",
     "life_stage": "unknown", "family": "Pomatomidae"},
    {"binomial": "Acanthurus coeruleus", "common_name": "Blue Tang",
     "life_stage": "unknown", "family": "Acanthuridae"},
    {"binomial": "Anilocra ./Renocila", "common_name": "Fish louse",
     "life_stage": "unknown", "family": "Cymothoidae"},
    {"binomial": None, "common_name": "TBC", "life_stage": "unknown", "family": None},
]
CLASSES = ["Acanthurus_coeruleus", "Neopomacentrus_cyanomos", "Pomacanthus_paru",
           "Pomatomus_saltarix", "Seriola_zonata"]
# raw_name, aphia_id, accepted_name, rank, match_type, family
TAXONOMY = [
    ("Acanthurus coeruleus", 900001, "Acanthurus coeruleus", "Species", "exact", "Acanthuridae"),
    ("Neopomacentrus cyanomos", 900002, "Neopomacentrus cyanomos", "Species", "exact",
     "Pomacentridae"),
    ("Pomacanthus paru", 900003, "Pomacanthus paru", "Species", "exact", "Pomacanthidae"),
    ("Pomatomus saltarix", 900004, "Pomatomus saltatrix", "Species", "phonetic", "Pomatomidae"),
    ("Seriola zonata", 900005, "Seriola zonata", "Species", "exact", "Carangidae"),
]
TAXONOMY_FIELDS = ["raw_name", "aphia_id", "accepted_name", "authority", "rank", "match_type",
                   "status", "species", "genus", "family", "order", "class", "phylum",
                   "kingdom", "lowest"]
INAT = [
    {"binomial": "Seriola zonata", "photo_id": 11, "common_name": "Banded Rudderfish",
     "license": "cc-by-nc", "attribution": "(c) tester, some rights reserved (CC BY-NC)",
     "local_path": "assets/inat_photos/Seriola_zonata/11.jpg"},
    {"binomial": "Acanthurus coeruleus", "photo_id": 12, "common_name": "Blue Tang",
     "license": "cc-by", "attribution": "(c) elsewhere", "local_path": "../outside/12.jpg"},
    {"binomial": "Neopomacentrus cyanomos", "photo_id": 13, "common_name": "Regal Demoiselle",
     "license": "cc0", "attribution": "(c) tester2",
     "local_path": "assets/inat_photos/Neopomacentrus_cyanomos/13.jpg"},
]
CELEBRITIES = {"celebrities": [
    {"id": "bobby", "nickname": "Bobby", "binomial": "Acanthurus coeruleus"}]}
BASELINE = ([{"label": "Seriola zonata", "source_kind": "drive:video"}] * 3
            + [{"label": "Pomatomus saltarix", "source_kind": "inat:image"}]
            + [{"label": "Acanthurus coeruleus", "source_kind": "inat:image"}] * 10
            + [{"label": "Seriola zonata", "source_kind": "review:video"}])


def jpeg(path: Path, color=(30, 60, 90), size=(1280, 720)) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, quality=90)
    return str(path)


def write_review_assets(root: Path) -> dict:
    """Write the fixture catalogue under `root` and return its paths plus
    the environment variables that point the dashboard at them."""
    import csv

    assets = root / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "species_manifest.json").write_text(json.dumps(SPECIES_MANIFEST))
    (assets / "inat_manifest.json").write_text(json.dumps(INAT))
    (assets / "celebrities.json").write_text(json.dumps(CELEBRITIES))
    with (assets / "taxonomy.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TAXONOMY_FIELDS)
        w.writeheader()
        for raw, aphia, accepted, rank, match, family in TAXONOMY:
            genus, _, epithet = accepted.partition(" ")
            w.writerow({"raw_name": raw, "aphia_id": aphia, "accepted_name": accepted,
                        "authority": "Tester, 1900", "rank": rank, "match_type": match,
                        "status": "accepted", "species": epithet, "genus": genus,
                        "family": family, "kingdom": "Animalia", "lowest": "species"})
    jpeg(assets / "inat_photos" / "Seriola_zonata" / "11.jpg", (200, 180, 40), (64, 48))
    jpeg(assets / "inat_photos" / "Neopomacentrus_cyanomos" / "13.jpg", (40, 40, 200), (64, 48))
    classes = root / "classes.json"
    classes.write_text(json.dumps(CLASSES))
    baseline = root / "_dataset_manifest.json"
    baseline.write_text(json.dumps(BASELINE))
    env = {
        "REVIEW_ASSETS_DIR": str(assets),
        "REVIEW_TAXONOMY_CSV": str(assets / "taxonomy.csv"),
        "CLASSIFIER_V2_CLASSES_JSON": str(classes),
        "REVIEW_BASELINE_MANIFEST": str(baseline),
    }
    return {"root": root, "assets": assets, "classes": classes, "baseline": baseline,
            "env": env}


def catalogue_paths(info: dict):
    from app.review_catalogue import CataloguePaths

    a = info["assets"]
    return CataloguePaths(
        species_manifest=a / "species_manifest.json", classes_json=info["classes"],
        taxonomy_csv=a / "taxonomy.csv", inat_manifest=a / "inat_manifest.json",
        celebrities_json=a / "celebrities.json", photo_root=info["root"])


@pytest.fixture
def review_assets(tmp_path):
    return write_review_assets(tmp_path / "catalogue")


@pytest.fixture
async def rdb():
    if not TEST_DSN:
        pytest.skip("TEST_DATABASE_URL not set")
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(TEST_DSN, min_size=1, max_size=4, open=False)
    await pool.open()
    async with pool.connection() as conn:
        await conn.execute(TRUNCATE_SQL)
    yield pool
    await pool.close()


@pytest.fixture
def rclient(rdb, review_assets, monkeypatch):
    from app.main import build_app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    for key, value in review_assets["env"].items():
        monkeypatch.setenv(key, value)
    with TestClient(build_app()) as client:
        yield client


async def seed_frame(db, tmp_path, *, ts=T0, source="seahivecam", frame_id=1000, boxes=(),
                     save=True, color=(30, 60, 90), num_fish=None) -> dict:
    """Insert one frame and its detections. Each box is a dict with `bbox`
    and optionally `species` (a classifier class name, spaces not
    underscores, as the worker writes it), `p`, `det_conf`, `track_id`.
    With save=False the detections exist but their frame was never saved."""
    from psycopg.types.json import Jsonb

    boxes = [dict(b) for b in boxes]
    image = jpeg(tmp_path / "frames" / f"{source}_{frame_id}_{int(ts.timestamp())}.jpg", color)
    async with db.connection() as conn:
        saved_frame_id = None
        if save:
            cur = await conn.execute(
                """INSERT INTO saved_frames (ts, frame_id, source_name, reason, image_path,
                                             coco_path, num_fish)
                   VALUES (%s, %s, %s, 'interesting:high_conf', %s, %s, %s) RETURNING id""",
                (ts, frame_id, source, image, image + ".coco.json",
                 len(boxes) if num_fish is None else num_fish))
            saved_frame_id = (await cur.fetchone())[0]
        event_ids = []
        for b in boxes:
            species = b.get("species")
            p = b.get("p", 0.62) if species else None
            topk = [{"name": species, "species_id": species, "accuracy": p}] if species else []
            cur = await conn.execute(
                """INSERT INTO detection_events (ts, frame_id, source_name, det_conf, bbox, topk,
                                                 best_name, best_species_id, best_accuracy,
                                                 track_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (ts, frame_id, source, b.get("det_conf", 0.8), list(b["bbox"]), Jsonb(topk),
                 species, species, p, b.get("track_id")))
            event_ids.append((await cur.fetchone())[0])
    return {"saved_frame_id": saved_frame_id, "event_ids": event_ids, "image_path": image,
            "ts": ts, "frame_id": frame_id, "source": source, "boxes": boxes}


async def make_user(db, username, role="reviewer", status="active") -> int:
    from app.auth import hash_password

    async with db.connection() as conn:
        cur = await conn.execute(
            """INSERT INTO users (username, display_name, email, password_hash, role, status)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (username, username.title(), f"{username}@example.org", hash_password(PASSWORD),
             role, status))
        return (await cur.fetchone())[0]


def login(client, username) -> None:
    client.cookies.clear()
    r = client.post("/api/auth/login", json={"username": username, "password": PASSWORD},
                    headers=CSRF)
    assert r.status_code == 200, r.text


def as_user(uid, *, role="reviewer", session_id=None, name="Rev"):
    from app.auth import CurrentUser

    return CurrentUser(id=uid, username=name.lower(), display_name=name, role=role,
                       status="active", session_id=session_id,
                       last_seen_at=datetime.now(UTC))


async def add_item(db, frame, index=0, *, lane="check", pass_=1, predicted=None,
                   predicted_p=None, route=None, hints=None, status="open",
                   claimed_by=None, claimed_until=None, exclude_user_id=None,
                   first_correction_id=None, added_at=None) -> int:
    from psycopg.types.json import Jsonb

    async with db.connection() as conn:
        cur = await conn.execute(
            """INSERT INTO review_items (event_id, pass, lane, saved_frame_id, source_name, ts,
                                         bbox, predicted, predicted_p, hints, route_species,
                                         status, claimed_by, claimed_until, exclude_user_id,
                                         first_correction_id, added_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       COALESCE(%s, now()))
               RETURNING id""",
            (frame["event_ids"][index], pass_, lane, frame["saved_frame_id"], frame["source"],
             frame["ts"], list(frame["boxes"][index]["bbox"]), predicted, predicted_p,
             Jsonb(hints) if hints is not None else None, route, status, claimed_by,
             claimed_until, exclude_user_id, first_correction_id, added_at))
        return (await cur.fetchone())[0]


async def add_verdict(db, event_id, user_id, *, name=None, not_a_fish=False,
                      unidentifiable=False, item_id=None, session_id=None,
                      is_adjudication=False, confidence="certain", label_source=None,
                      bbox=None, created_at=None, notes=None, reviewer=None) -> int:
    async with db.connection() as conn:
        cur = await conn.execute(
            """INSERT INTO detection_corrections
                   (event_id, corrected_name, corrected_species_id, not_a_fish, unidentifiable,
                    review_item_id, user_id, session_id, is_adjudication, confidence,
                    label_source, corrected_bbox, reviewer, notes, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       COALESCE(%s, now()))
               RETURNING id""",
            (event_id, name, name, not_a_fish, unidentifiable, item_id, user_id, session_id,
             is_adjudication, confidence, label_source, bbox, reviewer, notes, created_at))
        return (await cur.fetchone())[0]


async def wait_until_blocked(db, *, timeout_s: float = 5.0) -> None:
    """Return once some session in this database is waiting on a lock, read
    from pg_blocking_pids -- instead of sleeping and hoping the other task
    got there. Fails the test after timeout_s.

    Each poll ends its own implicit transaction with a rollback: a pooled
    connection defaults to autocommit=False, and left open across polls
    the backend-status view this reads back stays frozen at the first
    poll's snapshot -- it would never see the other session go on to
    block, no matter how long this waited."""
    deadline = time.monotonic() + timeout_s
    async with db.connection() as conn:
        while True:
            cur = await conn.execute(
                """SELECT count(*) FROM pg_stat_activity
                    WHERE datname = current_database()
                      AND cardinality(pg_blocking_pids(pid)) > 0""")
            found = (await cur.fetchone())[0]
            await conn.rollback()
            if found:
                return
            if time.monotonic() > deadline:
                raise AssertionError("no session became blocked")
            await asyncio.sleep(0.02)
