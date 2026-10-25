# Reviewer Annotation Tool — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Wahoo Bay's volunteers a working review page at `/review`: a pre-built pool of boxes, two lanes served thinnest-species-first, one-key verdicts with drag-to-fix boxes, a blind one-in-ten spot-check, whole-frame mode, and a browse page, with every verdict attributable and resolved by one shared rule.

**Architecture:** New focused modules in `services/dashboard/app/` (`review_catalogue`, `review_labels`, `review_images`, `review_pool`, `review_queue`, `review_frames`, `review_browse`) hold all the rules and are tested without HTTP. One router factory, `routes_review.py`, exposes them. A background task inside the dashboard's existing lifespan keeps the pool topped up. The page is vanilla JavaScript split into four classic scripts that share a global `RV` object. Phases 2 (scorer) and 3 (dataset builder) are planned separately; phase 1 lands **all** of the spec's schema in one migration.

**Tech Stack:** Python 3.11, FastAPI 0.135.3, Starlette 1.0.0, psycopg 3.3.3 (async pool), Pillow 12.1.1, pytest 9.0.3 with the anyio plugin, vanilla JavaScript (no build step), PostgreSQL. No new dependencies.

**Spec:** [docs/superpowers/specs/2026-09-24-reviewer-annotation-dataset-design.md](../specs/2026-09-24-reviewer-annotation-dataset-design.md)

## Global Constraints

- **No new Python or JavaScript dependencies.** One conda env runs every production process.
- **Python style:** `from __future__ import annotations` first in every new module; ruff line-length 100, rules `E,F,W,I,B,UP`; `tests/**` ignores `B` and `F401`. Run `/raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/ruff check <files>` before every commit.
- **The interpreter is always called by absolute path:** `/raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python`. Do not rely on `conda activate` or `$(...)` command substitution.
- **Run pytest as the direct child of `timeout`**, never through a shell script (spec 1 decision log, Ruling 18):
  `TEST_DATABASE_URL_FILE=.superpowers/test_dsn timeout 900 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest <files> -q`
- **Never run tests against the production database `wahoobay`.** The tests truncate tables. The test database is `wahoobay_test`; `tests/conftest.py` refuses a DSN whose database is `wahoobay`.
- **Do not touch production** (the live database, the running dashboard, workers, tunnel) before Task 14, and in Task 14 only after the user says go.
- **Schema changes live in `db/migrations/2026-09-24-review-tool.sql`** and are appended verbatim to `db/init.sql`. Everything is `IF NOT EXISTS` or a guarded `DO` block. **No views** (`CREATE OR REPLACE VIEW` takes an ACCESS EXCLUSIVE lock on a live table).
- **Background-task SQL sets its timeout with `set_config('statement_timeout', '5000', true)` inside `async with conn.transaction()`**, never with a session-level `SET`. A session-level timeout would stay on the pooled connection and kill the matview refresher's 47–144 s `REFRESH`.
- **A box's frame is always found by exact camera + `frame_id` + `ts`.** `frame_id` and `track_id` restart at 1 whenever a worker restarts.
- **Every mutation** requires `require_role(...)` and `require_csrf` (header `X-Requested-With: wahoobay`). Admin endpoints use `require_role("admin")`.
- **A spot-check (pass-2) payload never carries the model's guess, the hints, the species it counts toward, or the earlier verdict.** The server builds it that way; the page hides nothing.
- **JavaScript:** classic scripts sharing a global `RV`; never `innerHTML` with data (build nodes, set `textContent`); `/raid/scratch/dzimmerman2021/miniconda3/envs/node/bin/node --check <file>` must pass.
- **Git:** stage explicit paths only, never `git add -A`. `assets/celebrities.json` and `assets/species.xlsx` are rewritten by the sheet poller and must never be staged by this plan. End every commit message with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.

## Review Focus

Five conditions the spec implies but no task's happy-path tests would exercise. Each has a test in the task that owns the code.

1. **The pool task's statement timeout leaking onto a pooled connection.** A plain `SET statement_timeout` survives on the connection after it returns to the pool; the next borrower could be the matview refresher, whose `REFRESH` then dies after 5 s and the public species pages go stale. After a pool run, a borrowed connection must report the same `statement_timeout` it had before. *(Task 6)*
2. **The model's guess leaking into a spot-check.** The obvious leaks are `model` and `hints`, but `counts_toward` names the route species (the guess) and whole-frame mode can show the first verdict. Search the whole pass-2 JSON for the guess and hint names; show a box with a pending spot-check as "answered" with no label. *(Tasks 7, 8)*
3. **A saved frame's JPEG missing on disk.** Frames can be deleted by hand. Serving such a box shows a broken image and a box nobody can answer. The item must be retired as `frame_missing`, and the next box served instead. *(Task 7)*
4. **The date window's end day.** An admin who sets the window to end on 2026-06-25 means "through the end of that day, local time". Store the end as local midnight of the *next* day, so a box captured at 2026-06-25 18:00 EDT is inside. *(Task 9)*
5. **A species name that has left the catalogue.** The Species Log changes daily. A page loaded before a sheet edit can submit a name that no longer exists. The server answers 400 "pick a species from the list"; the page shows that message and reloads the catalogue. *(Tasks 7, 10)*

---

## File Structure

| File | Responsibility |
|---|---|
| `db/migrations/2026-09-24-review-tool.sql` | All of spec §5, as one idempotent migration |
| `db/init.sql` | The same statements appended; `detection_corrections.event_id` becomes `ON DELETE RESTRICT` |
| `tests/conftest.py` | DSN-from-file, production guard, schema bootstrap, `REVIEW_POOL_ENABLED=false` for every test |
| `tests/review_fixtures.py` | Fixtures `review_assets`, `rdb`, `rclient`; seeding helpers |
| `scripts/enrich_taxonomy_from_worms.py` | Also resolves classifier class names; keeps AphiaID and accepted name; writes an unresolved list |
| `assets/taxonomy.csv`, `assets/taxonomy_unresolved.txt` | Regenerated output (committed) |
| `services/dashboard/app/review_catalogue.py` | What a volunteer can pick; raw-name → accepted-name mapping |
| `services/dashboard/app/review_labels.py` | The one definition of "verified"; agreement statistics |
| `services/dashboard/app/review_images.py` | Frame size, zoom region, JPEG rendering |
| `services/dashboard/app/review_pool.py` | Pool top-up task, date window, progress counts |
| `services/dashboard/app/review_queue.py` | Serving, claiming, verdicts, skips, spot-check scheduling, reopening |
| `services/dashboard/app/review_frames.py` | Whole-frame mode |
| `services/dashboard/app/review_browse.py` | Progress, browse, disputes, agreement, window, status |
| `services/dashboard/app/routes_review.py` | `build_review_router(cfg, state, templates)` |
| `services/dashboard/app/main.py` | Config, lifespan wiring, router mount, privacy fix, exact-timestamp lookups |
| `services/dashboard/app/exports.py`, `services/dashboard/app/data/README.md` | Identity-free public corrections exports; new `labeled_corrections` columns |
| `services/dashboard/app/templates/review.html` | The page shell |
| `services/dashboard/app/static/review.css` | Page styles |
| `services/dashboard/app/static/review-core.js` | API calls, catalogue search, species picker, box editor, keys, views |
| `services/dashboard/app/static/review-single.js` | Single-box view and recent-boxes strip |
| `services/dashboard/app/static/review-frame.js` | Whole-frame view |
| `services/dashboard/app/static/review-browse.js` | Browse view and admin panels |
| `scripts/dev/seed_review_demo.py` | Copies a real sample into the test database for trying the page |
| `scripts/dev/explain_review_probes.py` | Prints the pool's probe plans against a database |
| `docs/operations.md`, `README.md`, `.env.example` | Operating the pool task and the new page |

Test files: `tests/test_review_schema.py`, `tests/test_privacy_exports.py`, `tests/test_enrich_taxonomy.py`, `tests/test_review_catalogue.py`, `tests/test_review_labels.py`, `tests/test_review_pool.py`, `tests/test_review_queue.py`, `tests/test_review_routes.py`, `tests/test_review_frames.py`, `tests/test_review_browse.py`, `tests/test_review_page.py`, `tests/test_review_docs.py`.

**Test database.** `wahoobay_test` already exists on the local server with the full schema (spec 1). Task 1 writes its DSN to `.superpowers/test_dsn` (mode 600, gitignored) and every test command passes that file through `TEST_DATABASE_URL_FILE`.

**Working copy.** Implementation happens in a git worktree off the current `reports-panel-and-weather` HEAD. Several inputs are gitignored and therefore missing from a fresh worktree: `assets/species_manifest.json`, `assets/inat_manifest.json`, `assets/inat_photos/`, `data/models/classifier_v2/classes.json`, `data/fine_tune/_dataset_manifest.json`. Tests never read them (they use generated fixtures). Tasks 3 and 10 read them from the main checkout at `/raid/scratch/dzimmerman2021/wahoobay/` by absolute path.

---

### Task 1: Test harness and schema

**Files:**
- Create: `db/migrations/2026-09-24-review-tool.sql`
- Modify: `db/init.sql` (line 258 `ON DELETE CASCADE` → `RESTRICT`; append the migration body)
- Modify: `tests/conftest.py`
- Create: `tests/review_fixtures.py`
- Test: `tests/test_review_schema.py`

**Interfaces:**
- Consumes: the existing `wahoobay_test` database; `app.auth.hash_password`, `app.auth.CurrentUser`.
- Produces (in `tests/review_fixtures.py`, used by every later test file): constants `TEST_DSN`, `CSRF`, `PASSWORD`, `T0`, `TRUNCATE_SQL`; `write_review_assets(root: Path) -> dict`; `catalogue_paths(info: dict) -> CataloguePaths`; fixtures `review_assets`, `rdb` (async, an `AsyncConnectionPool` on a truncated test database), `rclient` (a `TestClient` wired to the test database and the fixture assets); helpers `jpeg(path, color=..., size=...) -> str`, `async seed_frame(db, tmp_path, *, ts=T0, source="seahivecam", frame_id=1000, boxes=(), save=True, color=..., num_fish=None) -> dict`, `async make_user(db, username, role="reviewer", status="active") -> int`, `login(client, username) -> None`, `as_user(uid, *, role="reviewer", session_id=None, name="Rev") -> CurrentUser`, `async add_item(db, frame, index=0, **kw) -> int`, `async add_verdict(db, event_id, user_id, **kw) -> int`.

- [ ] **Step 1: Write the DSN file for the test database**

Run from the worktree root. The production DSN is read from the main checkout's `.env` (a worktree has no `.env`), its database name is swapped for `wahoobay_test`, and the result is written to a mode-600 file. Nothing is printed.

```bash
mkdir -p .superpowers
/raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python - <<'EOF'
import os
from pathlib import Path
from psycopg.conninfo import make_conninfo
env = Path("/raid/scratch/dzimmerman2021/wahoobay/.env").read_text().splitlines()
prod = next(l.split("=", 1)[1].strip().strip("'\"") for l in env if l.startswith("DATABASE_URL="))
dsn = make_conninfo(prod, dbname="wahoobay_test")
fd = os.open(".superpowers/test_dsn", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
os.write(fd, dsn.encode()); os.close(fd)
print("wrote .superpowers/test_dsn")
EOF
```

- [ ] **Step 2: Write the failing schema tests**

```python
# tests/test_review_schema.py
"""Schema for the review tool (spec §5): tables, columns, constraints,
and that db/init.sql carries the same statements as the migration."""
from __future__ import annotations

import re
from pathlib import Path

import psycopg
import pytest
from review_fixtures import TEST_DSN

REPO = Path(__file__).resolve().parents[1]
MIGRATION = REPO / "db" / "migrations" / "2026-09-24-review-tool.sql"
needs_db = pytest.mark.skipif(not TEST_DSN, reason="TEST_DATABASE_URL not set")


def _body(sql: str) -> str:
    """The migration minus its header comments and the SET line, with
    whitespace collapsed, so it can be looked for inside init.sql."""
    lines = [ln for ln in sql.splitlines()
             if not ln.startswith("--") and not ln.startswith("SET lock_timeout")]
    return re.sub(r"\s+", " ", "\n".join(lines)).strip()


def test_migration_is_mirrored_in_init_sql():
    init = re.sub(r"\s+", " ", (REPO / "db" / "init.sql").read_text())
    assert _body(MIGRATION.read_text()) in init


def test_init_sql_creates_verdicts_with_restrict():
    init = (REPO / "db" / "init.sql").read_text()
    block = init[init.index("CREATE TABLE IF NOT EXISTS detection_corrections"):]
    block = block[: block.index(");")]
    assert "ON DELETE RESTRICT" in block and "ON DELETE CASCADE" not in block


@needs_db
def test_review_tables_and_columns_exist():
    with psycopg.connect(TEST_DSN) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
        assert {"review_items", "review_skips", "review_added_boxes",
                "review_frames", "review_settings"} <= tables

        def cols(table):
            return {r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                (table,))}

        assert {"review_item_id", "label_source", "unidentifiable", "life_stage",
                "is_adjudication"} <= cols("detection_corrections")
        assert {"credit_opt_in", "credit_name", "orcid"} <= cols("users")
        assert {"hints", "hints_model", "hinted_at", "route_species", "exclude_user_id",
                "first_correction_id"} <= cols("review_items")


@needs_db
def test_migration_is_idempotent():
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute(MIGRATION.read_text())
        conn.execute(MIGRATION.read_text())


@needs_db
def test_a_verdict_blocks_deleting_its_detection():
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute("TRUNCATE detection_events, detection_corrections RESTART IDENTITY CASCADE")
        eid = conn.execute(
            """INSERT INTO detection_events (frame_id, source_name, det_conf, bbox, topk)
               VALUES (1, 'seahivecam', 0.9, '{1,2,30,40}', '[]') RETURNING id""").fetchone()[0]
        conn.execute("INSERT INTO detection_corrections (event_id, not_a_fish) VALUES (%s, true)",
                     (eid,))
        conn.commit()
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute("DELETE FROM detection_events WHERE id = %s", (eid,))
        conn.rollback()


@needs_db
def test_a_detection_without_verdicts_can_still_be_deleted():
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute("TRUNCATE detection_events, detection_corrections RESTART IDENTITY CASCADE")
        eid = conn.execute(
            """INSERT INTO detection_events (frame_id, source_name, det_conf, bbox, topk)
               VALUES (1, 'seahivecam', 0.9, '{1,2,30,40}', '[]') RETURNING id""").fetchone()[0]
        conn.execute("DELETE FROM detection_events WHERE id = %s", (eid,))
        conn.commit()


@needs_db
def test_a_verdict_cannot_be_both_no_animal_and_unidentifiable():
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute("TRUNCATE detection_events, detection_corrections RESTART IDENTITY CASCADE")
        eid = conn.execute(
            """INSERT INTO detection_events (frame_id, source_name, det_conf, bbox, topk)
               VALUES (1, 'seahivecam', 0.9, '{1,2,30,40}', '[]') RETURNING id""").fetchone()[0]
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """INSERT INTO detection_corrections (event_id, not_a_fish, unidentifiable)
                   VALUES (%s, true, true)""", (eid,))
        conn.rollback()


@needs_db
def test_one_review_item_per_event_and_pass():
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute("TRUNCATE detection_events, saved_frames RESTART IDENTITY CASCADE")
        eid = conn.execute(
            """INSERT INTO detection_events (frame_id, source_name, det_conf, bbox, topk)
               VALUES (1, 'seahivecam', 0.9, '{1,2,30,40}', '[]') RETURNING id""").fetchone()[0]
        sf = conn.execute(
            """INSERT INTO saved_frames (frame_id, source_name, reason, image_path, coco_path,
                                         num_fish)
               VALUES (1, 'seahivecam', 'timelapse', '/x.jpg', '/x.coco.json', 1)
               RETURNING id""").fetchone()[0]
        ins = """INSERT INTO review_items (event_id, pass, lane, saved_frame_id, source_name, ts,
                                           bbox)
                 VALUES (%s, %s, 'check', %s, 'seahivecam', now(), '{1,2,30,40}')"""
        conn.execute(ins, (eid, 1, sf))
        conn.execute(ins, (eid, 2, sf))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(ins, (eid, 1, sf))
        conn.rollback()
```

- [ ] **Step 3: Update `tests/conftest.py`**

Add the DSN-file handling, the production guard, the schema bootstrap and the fixture import at the top of the module, directly under the existing `import pytest` line. Add one line to the existing `_isolate_network` fixture.

```python
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The test database DSN can be handed over in a file rather than the
# environment, so it never appears on a command line or needs shell
# expansion: TEST_DATABASE_URL_FILE=.superpowers/test_dsn pytest ...
# Test modules read TEST_DATABASE_URL when they are imported, which is
# after this conftest runs, so setting it here is early enough.
_dsn_file = os.environ.get("TEST_DATABASE_URL_FILE")
if _dsn_file and not os.environ.get("TEST_DATABASE_URL"):
    os.environ["TEST_DATABASE_URL"] = Path(_dsn_file).read_text().strip()

if os.environ.get("TEST_DATABASE_URL"):
    from psycopg.conninfo import conninfo_to_dict

    if conninfo_to_dict(os.environ["TEST_DATABASE_URL"]).get("dbname") == "wahoobay":
        # These tests TRUNCATE tables. The production database is named
        # 'wahoobay'. Refuse outright instead of trusting every caller.
        raise pytest.UsageError(
            "TEST_DATABASE_URL points at the production database 'wahoobay'")

from review_fixtures import rclient, rdb, review_assets  # noqa: E402,F401

MIGRATION = REPO_ROOT / "db" / "migrations" / "2026-09-24-review-tool.sql"


@pytest.fixture(scope="session", autouse=True)
def _review_schema():
    """Bring the test database up to the review-tool schema once per run.
    The migration is idempotent, so after the first run this is a no-op."""
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        return
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(MIGRATION.read_text())
```

Inside `_isolate_network`, after the three mail lines:

```python
    # The review pool's background task would fill review_items behind a
    # test's back. Tests that need a pool run call ReviewPool.run_once().
    monkeypatch.setenv("REVIEW_POOL_ENABLED", "false")
```

- [ ] **Step 4: Create `tests/review_fixtures.py`**

```python
"""Fixtures and seeding helpers for the review-tool tests.

tests/conftest.py imports the fixtures so every test module can use
them; test modules import the helpers directly. Nothing here imports
the dashboard's review modules at import time, because conftest imports
this file before those modules exist (Task 1 lands before Task 4)."""
from __future__ import annotations

import json
import os
import sys
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
```

- [ ] **Step 5: Run the schema tests to verify they fail**

Run: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn timeout 900 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest tests/test_review_schema.py -q`
Expected: ERROR at session start, because the `_review_schema` fixture cannot find `db/migrations/2026-09-24-review-tool.sql` (FileNotFoundError).

- [ ] **Step 6: Write the migration**

```sql
-- Reviewer annotation tool: every schema change for phases 1-3
-- (docs/superpowers/specs/2026-09-24-reviewer-annotation-dataset-design.md, section 5).
--
-- Apply to the live database as one transaction, this file only:
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -1 -f db/migrations/2026-09-24-review-tool.sql
-- Every statement is idempotent. The same statements are appended to
-- db/init.sql for fresh databases; tests/test_review_schema.py fails if
-- the two drift apart.
SET lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS review_items (
    id                  BIGSERIAL PRIMARY KEY,
    event_id            BIGINT      NOT NULL REFERENCES detection_events(id) ON DELETE CASCADE,
    pass                SMALLINT    NOT NULL DEFAULT 1 CHECK (pass IN (1, 2)),
    lane                TEXT        NOT NULL CHECK (lane IN ('check', 'identify')),
    saved_frame_id      BIGINT      NOT NULL REFERENCES saved_frames(id),
    source_name         TEXT        NOT NULL,
    ts                  TIMESTAMPTZ NOT NULL,
    bbox                INTEGER[]   NOT NULL,
    predicted           TEXT,
    predicted_p         REAL,
    hints               JSONB,
    hints_model         TEXT,
    hinted_at           TIMESTAMPTZ,
    route_species       TEXT,
    status              TEXT        NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'claimed', 'done', 'retired')),
    retired_reason      TEXT,
    claimed_by          BIGINT      REFERENCES users(id),
    claimed_until       TIMESTAMPTZ,
    exclude_user_id     BIGINT      REFERENCES users(id),
    first_correction_id BIGINT      REFERENCES detection_corrections(id),
    added_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    done_at             TIMESTAMPTZ,
    UNIQUE (event_id, pass)
);
CREATE INDEX IF NOT EXISTS review_items_open
    ON review_items (lane, pass, route_species) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS review_items_claimed
    ON review_items (claimed_until) WHERE status = 'claimed';
CREATE INDEX IF NOT EXISTS review_items_unscored
    ON review_items (id) WHERE hints IS NULL AND status IN ('open', 'claimed');
CREATE INDEX IF NOT EXISTS review_items_camera_ts
    ON review_items (source_name, ts);
CREATE INDEX IF NOT EXISTS review_items_frame
    ON review_items (saved_frame_id);

CREATE TABLE IF NOT EXISTS review_skips (
    item_id     BIGINT      NOT NULL REFERENCES review_items(id) ON DELETE CASCADE,
    user_id     BIGINT      NOT NULL REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (item_id, user_id)
);

CREATE TABLE IF NOT EXISTS review_added_boxes (
    id              BIGSERIAL PRIMARY KEY,
    saved_frame_id  BIGINT      NOT NULL REFERENCES saved_frames(id),
    bbox            INTEGER[]   NOT NULL,
    outcome         TEXT        NOT NULL CHECK (outcome IN ('species', 'unidentifiable')),
    label           TEXT,
    life_stage      TEXT,
    confidence      TEXT        NOT NULL DEFAULT 'certain'
                    CHECK (confidence IN ('certain', 'probable', 'uncertain')),
    celebrity_id    TEXT,
    user_id         BIGINT      NOT NULL REFERENCES users(id),
    session_id      BIGINT      REFERENCES user_sessions(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ,
    removed_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS review_added_boxes_frame
    ON review_added_boxes (saved_frame_id) WHERE removed_at IS NULL;

CREATE TABLE IF NOT EXISTS review_frames (
    saved_frame_id  BIGINT      PRIMARY KEY REFERENCES saved_frames(id),
    completed_by    BIGINT      NOT NULL REFERENCES users(id),
    session_id      BIGINT      REFERENCES user_sessions(id),
    completed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS review_settings (
    key         TEXT        PRIMARY KEY,
    value       JSONB       NOT NULL,
    updated_by  BIGINT      REFERENCES users(id),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE detection_corrections
    ADD COLUMN IF NOT EXISTS review_item_id  BIGINT REFERENCES review_items(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS label_source    TEXT
        CHECK (label_source IN ('model_guess', 'hint', 'search')),
    ADD COLUMN IF NOT EXISTS unidentifiable  BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS life_stage      TEXT,
    ADD COLUMN IF NOT EXISTS is_adjudication BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS detection_corrections_review_item
    ON detection_corrections (review_item_id) WHERE review_item_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS detection_corrections_user
    ON detection_corrections (user_id, created_at DESC) WHERE user_id IS NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'detection_corrections_outcome_chk') THEN
        ALTER TABLE detection_corrections
            ADD CONSTRAINT detection_corrections_outcome_chk
            CHECK (NOT (not_a_fish AND unidentifiable));
    END IF;
END $$;

-- Human verdicts must never disappear because a detection was pruned.
-- Replace the cascading foreign key once; on a database already using
-- RESTRICT (fresh init.sql, or a second run) this block does nothing.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint
                WHERE conname = 'detection_corrections_event_id_fkey'
                  AND conrelid = 'detection_corrections'::regclass
                  AND confdeltype = 'c') THEN
        ALTER TABLE detection_corrections
            ADD CONSTRAINT detection_corrections_event_id_restrict
            FOREIGN KEY (event_id) REFERENCES detection_events(id) ON DELETE RESTRICT NOT VALID;
        ALTER TABLE detection_corrections
            VALIDATE CONSTRAINT detection_corrections_event_id_restrict;
        ALTER TABLE detection_corrections DROP CONSTRAINT detection_corrections_event_id_fkey;
        ALTER TABLE detection_corrections
            RENAME CONSTRAINT detection_corrections_event_id_restrict
            TO detection_corrections_event_id_fkey;
    END IF;
END $$;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS credit_opt_in BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS credit_name   TEXT,
    ADD COLUMN IF NOT EXISTS orcid         TEXT;
```

- [ ] **Step 7: Mirror it in `db/init.sql`**

1. In the `CREATE TABLE IF NOT EXISTS detection_corrections` statement (line 258), change
   `event_id BIGINT NOT NULL REFERENCES detection_events(id) ON DELETE CASCADE,` to
   `event_id BIGINT NOT NULL REFERENCES detection_events(id) ON DELETE RESTRICT,`.
2. Append to the end of `db/init.sql` a header comment line, `-- Reviewer annotation tool (spec 2026-09-24): mirrors db/migrations/2026-09-24-review-tool.sql`, then every line of the migration from `CREATE TABLE IF NOT EXISTS review_items (` to the end of the file, copied verbatim. Do not copy the header comments or the `SET lock_timeout` line.

- [ ] **Step 8: Run the schema tests to verify they pass**

Run: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn timeout 900 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest tests/test_review_schema.py -q`
Expected: 8 passed.

- [ ] **Step 9: Confirm the existing suite still passes**

Run: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn timeout 900 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest tests -q`
Expected: every test passes (the 100 from spec 1 plus the 8 new ones).

- [ ] **Step 10: Commit**

```bash
git add db/migrations/2026-09-24-review-tool.sql db/init.sql tests/conftest.py tests/review_fixtures.py tests/test_review_schema.py
git commit -m "review: schema for the annotation tool, and a test harness that can reach it

One idempotent migration carries every table and column the three phases
need, so the live database is migrated once. Verdicts can no longer be
cascade-deleted with their detection. Tests read the test DSN from a
file, refuse the production database by name, and bring the test
database up to this schema at the start of each run.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Public exports without names, and exact-timestamp frame lookups

**Files:**
- Modify: `services/dashboard/app/main.py` (`/api/events` ~line 704, `/api/detections/{event_id}/roi.jpg` ~line 765, `/api/detections/{event_id}/frame.jpg` ~line 828, `/api/corrections` ~line 1315, `/api/export/corrections.csv` ~line 2213, `/api/export/labeled_corrections.csv` ~line 2280)
- Modify: `services/dashboard/app/exports.py` (`export_corrections`, `export_labeled_corrections`)
- Modify: `services/dashboard/app/data/README.md` (the `labeled_corrections.csv` and `corrections.csv` sections)
- Test: `tests/test_privacy_exports.py`

**Interfaces:**
- Consumes: `app.auth.make_optional_user`, `app.auth.effective_role`; fixtures from Task 1.
- Produces: `export_corrections(pool, hours, reviewer, include_identity=False)`, `export_labeled_corrections(pool, include_identity=False)`; `IDENTITY_COLUMNS = ("reviewer", "notes", "user_id", "session_id")` in `exports.py`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_privacy_exports.py
"""Public exports must not publish who labelled what (spec §12.1), and a
box's frame is the one captured at the box's own timestamp (spec §12.3)."""
from __future__ import annotations

import io
from datetime import timedelta

import pytest
from PIL import Image
from review_fixtures import T0, add_verdict, login, make_user, seed_frame

pytestmark = pytest.mark.anyio

IDENTITY = {"reviewer", "notes", "user_id", "session_id"}


async def _seed_named_verdict(rdb, tmp_path):
    frame = await seed_frame(rdb, tmp_path, boxes=[{"bbox": [100, 100, 300, 250],
                                                    "species": "Seriola zonata"}])
    uid = await make_user(rdb, "alice")
    await add_verdict(rdb, frame["event_ids"][0], uid, name="Seriola zonata",
                      reviewer="Alice Example", notes="secret note")
    return frame


def _header(text: str) -> set[str]:
    return set(text.splitlines()[0].split(","))


async def test_anonymous_labeled_corrections_has_labels_but_no_identity(rdb, rclient, tmp_path):
    await _seed_named_verdict(rdb, tmp_path)
    r = rclient.get("/api/export/labeled_corrections.csv")
    assert r.status_code == 200
    header = _header(r.text)
    assert not (IDENTITY & header)
    assert {"corrected_name", "corrected_bbox", "unidentifiable", "life_stage",
            "label_source", "review_item_id", "is_adjudication", "is_endorsement"} <= header
    assert "Alice Example" not in r.text and "secret note" not in r.text


async def test_admin_labeled_corrections_includes_identity(rdb, rclient, tmp_path):
    await _seed_named_verdict(rdb, tmp_path)
    await make_user(rdb, "boss", role="admin")
    login(rclient, "boss")
    r = rclient.get("/api/export/labeled_corrections.csv")
    assert IDENTITY <= _header(r.text)
    assert "Alice Example" in r.text


async def test_anonymous_corrections_csv_and_list_hide_identity(rdb, rclient, tmp_path):
    await _seed_named_verdict(rdb, tmp_path)
    csv_text = rclient.get("/api/export/corrections.csv").text
    assert not (IDENTITY & _header(csv_text)) and "Alice Example" not in csv_text
    rows = rclient.get("/api/corrections").json()
    assert rows and all("reviewer" not in row and "notes" not in row for row in rows)


async def test_filtering_by_reviewer_is_admin_only(rdb, rclient, tmp_path):
    await _seed_named_verdict(rdb, tmp_path)
    assert rclient.get("/api/corrections?reviewer=Alice%20Example").status_code == 403
    assert rclient.get("/api/export/corrections.csv?reviewer=x").status_code == 403
    await make_user(rdb, "boss", role="admin")
    login(rclient, "boss")
    rows = rclient.get("/api/corrections?reviewer=Alice%20Example").json()
    assert [r["reviewer"] for r in rows] == ["Alice Example"]


async def test_roi_uses_the_frame_saved_at_the_detections_own_timestamp(rdb, rclient, tmp_path):
    # A worker restart reused frame_id 500: the red frame is from before the
    # restart, the green one from after. The detection belongs to the red one.
    before = await seed_frame(rdb, tmp_path, ts=T0, frame_id=500, color=(220, 10, 10),
                              boxes=[{"bbox": [100, 100, 300, 250]}])
    await seed_frame(rdb, tmp_path, ts=T0 + timedelta(hours=2), frame_id=500,
                     color=(10, 220, 10), boxes=[{"bbox": [100, 100, 300, 250]}])
    r = rclient.get(f"/api/detections/{before['event_ids'][0]}/roi.jpg")
    assert r.status_code == 200
    red, green, _ = Image.open(io.BytesIO(r.content)).convert("RGB").resize((1, 1)).getpixel((0, 0))
    assert red > 150 and green < 80


async def test_detection_whose_own_frame_was_not_saved_gets_no_image(rdb, rclient, tmp_path):
    await seed_frame(rdb, tmp_path, ts=T0, frame_id=500, boxes=[{"bbox": [1, 1, 50, 50]}])
    orphan = await seed_frame(rdb, tmp_path, ts=T0 + timedelta(hours=1), frame_id=500,
                              save=False, boxes=[{"bbox": [1, 1, 50, 50]}])
    assert rclient.get(f"/api/detections/{orphan['event_ids'][0]}/roi.jpg").status_code == 404
    assert rclient.get(f"/api/detections/{orphan['event_ids'][0]}/frame.jpg").status_code == 404


async def test_labeled_corrections_frame_path_matches_the_timestamp(rdb, rclient, tmp_path):
    before = await seed_frame(rdb, tmp_path, ts=T0, frame_id=500,
                              boxes=[{"bbox": [1, 1, 50, 50]}])
    after = await seed_frame(rdb, tmp_path, ts=T0 + timedelta(hours=2), frame_id=500,
                             boxes=[{"bbox": [1, 1, 50, 50]}])
    uid = await make_user(rdb, "alice")
    await add_verdict(rdb, before["event_ids"][0], uid, not_a_fish=True)
    text = rclient.get("/api/export/labeled_corrections.csv").text
    assert before["image_path"] in text and after["image_path"] not in text
```

- [ ] **Step 2: Run them to verify they fail**

Run: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn timeout 900 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest tests/test_privacy_exports.py -q`
Expected: FAIL. Identity columns are present in the anonymous exports; the reviewer filter returns 200; the ROI comes from the green frame (the newest match); the orphan returns 200.

- [ ] **Step 3: Change `exports.py`**

Add below the `_fetch_all` helper:

```python
# Columns that say who labelled a box and what they wrote. Spec §12.1:
# since spec 1, `reviewer` is an account's display name, so these are
# returned to administrators only.
IDENTITY_COLUMNS = ("reviewer", "notes", "user_id", "session_id")


def _without_identity(columns: list[str], include_identity: bool) -> list[str]:
    if include_identity:
        return columns
    return [c for c in columns if c not in IDENTITY_COLUMNS]
```

Replace `export_corrections` with:

```python
def export_corrections(pool: AsyncConnectionPool, hours: int | None,
                       reviewer: str | None, include_identity: bool = False):
    where = []
    params: list = []
    if hours is not None:
        where.append("c.created_at >= NOW() - (%s::int || ' hours')::interval")
        params.append(hours)
    if reviewer:
        where.append("c.reviewer = %s")
        params.append(reviewer)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT c.id, c.created_at, c.event_id,
               c.corrected_name, c.corrected_species_id, c.not_a_fish,
               c.confidence, c.reviewer, c.notes, c.user_id, c.session_id,
               e.ts AS event_ts, e.source_name, e.frame_id,
               e.best_name AS original_name, e.best_species_id AS original_species_id,
               e.best_accuracy AS original_accuracy, e.bbox, e.image_path
          FROM detection_corrections c
          JOIN detection_events e ON e.id = c.event_id
          {where_sql}
         ORDER BY c.created_at DESC
    """
    cols = ["id", "created_at", "event_id",
            "corrected_name", "corrected_species_id", "not_a_fish",
            "confidence", "reviewer", "notes", "user_id", "session_id",
            "event_ts", "source_name", "frame_id",
            "original_name", "original_species_id",
            "original_accuracy", "bbox", "image_path"]
    return _stream(pool, sql, tuple(params), _without_identity(cols, include_identity))
```

Replace `export_labeled_corrections` with:

```python
def export_labeled_corrections(pool: AsyncConnectionPool, include_identity: bool = False):
    # The frame is matched on camera + frame_id + ts. frame_id restarts at 1
    # with every worker restart, so frame_id + camera alone can name a
    # different frame. When one frame was saved under several reasons, the
    # lowest id wins.
    sql = """
        SELECT c.id AS correction_id,
               c.created_at,
               c.event_id,
               c.corrected_name, c.corrected_species_id, c.not_a_fish, c.unidentifiable,
               c.is_endorsement, c.corrected_bbox, c.life_stage, c.label_source,
               c.review_item_id, c.is_adjudication,
               c.confidence, c.reviewer, c.notes, c.user_id, c.session_id,
               e.ts AS event_ts,
               e.source_name, e.frame_id, e.track_id,
               e.bbox, e.det_conf,
               e.best_name AS original_name,
               e.best_species_id AS original_species_id,
               e.best_accuracy   AS original_accuracy,
               e.topk            AS original_topk,
               COALESCE(sf.image_path, e.image_path) AS frame_image_path,
               sf.coco_path      AS frame_coco_path,
               e.model_version, e.detector_sha256, e.classifier_sha256,
               e.config_hash, e.pipeline_git_sha
          FROM detection_corrections c
          JOIN detection_events e ON e.id = c.event_id
          LEFT JOIN LATERAL (
              SELECT image_path, coco_path FROM saved_frames
               WHERE source_name = e.source_name AND frame_id = e.frame_id AND ts = e.ts
               ORDER BY id
               LIMIT 1
          ) sf ON true
         ORDER BY c.created_at DESC
    """
    cols = ["correction_id", "created_at", "event_id",
            "corrected_name", "corrected_species_id", "not_a_fish", "unidentifiable",
            "is_endorsement", "corrected_bbox", "life_stage", "label_source",
            "review_item_id", "is_adjudication",
            "confidence", "reviewer", "notes", "user_id", "session_id",
            "event_ts", "source_name", "frame_id", "track_id",
            "bbox", "det_conf",
            "original_name", "original_species_id", "original_accuracy",
            "original_topk",
            "frame_image_path", "frame_coco_path",
            "model_version", "detector_sha256", "classifier_sha256",
            "config_hash", "pipeline_git_sha"]
    return _stream(pool, sql, tuple(), _without_identity(cols, include_identity))
```

- [ ] **Step 4: Change `main.py`**

(a) In `/api/events`, `/api/detections/{event_id}/roi.jpg` and `/api/detections/{event_id}/frame.jpg`, replace each saved-frame subquery

```sql
(SELECT image_path FROM saved_frames sf
  WHERE sf.frame_id = de.frame_id
    AND sf.source_name = de.source_name
  ORDER BY sf.ts DESC LIMIT 1)
```

with

```sql
(SELECT image_path FROM saved_frames sf
  WHERE sf.frame_id = de.frame_id
    AND sf.source_name = de.source_name
    AND sf.ts = de.ts
  ORDER BY sf.id LIMIT 1)
```

and in `/api/events` replace the comment's last sentence, "Newest matching saved_frame wins.", with: "The timestamp is part of the match: frame_id restarts at 1 with every worker restart, so frame_id + camera alone can name a different frame. When one frame was saved under several reasons, the lowest id wins."

(b) Directly after `require_role = make_require_role(cfg, state)` (inside `build_app`), add:

```python
    from .auth import effective_role, make_optional_user
    optional_user = make_optional_user(state)

    def _is_admin(user) -> bool:
        return user is not None and effective_role(user.status, user.role) == "admin"
```

(c) Replace the `list_corrections` handler's signature and add the identity rules:

```python
    @app.get("/api/corrections")
    async def list_corrections(
        limit: int = Query(50, ge=1, le=500),
        reviewer: str | None = None,
        user=Depends(optional_user),
    ) -> Response:
        admin = _is_admin(user)
        if reviewer and not admin:
            return JSONResponse(
                {"error": "filtering by reviewer requires the admin role"}, status_code=403)
```

Keep the body as it is, and immediately before `return JSONResponse(rows)` add:

```python
        if not admin:
            # Spec §12.1: reviewer is an account's display name now.
            for r in rows:
                r.pop("reviewer", None)
                r.pop("notes", None)
```

(d) Replace the two export routes:

```python
    @app.get("/api/export/corrections.csv")
    async def export_corrections_csv(
        hours: int | None = Query(None, ge=1, le=24 * 365),
        reviewer: str | None = None,
        user=Depends(optional_user),
    ) -> Response:
        admin = _is_admin(user)
        if reviewer and not admin:
            return JSONResponse(
                {"error": "filtering by reviewer requires the admin role"}, status_code=403)
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        gen = export_corrections(pool, hours, reviewer, include_identity=admin)
        return _csv_response(gen, _csv_filename("corrections", hours=hours, reviewer=reviewer))
```

```python
    @app.get("/api/export/labeled_corrections.csv")
    async def export_labeled_corrections_csv(user=Depends(optional_user)) -> Response:
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        gen = export_labeled_corrections(pool, include_identity=_is_admin(user))
        return _csv_response(gen, _csv_filename("labeled_corrections"))
```

- [ ] **Step 5: Update the export README**

In `services/dashboard/app/data/README.md`, replace the `labeled_corrections.csv` table with:

```markdown
| Column | Meaning |
|---|---|
| `correction_id`, `created_at`, `confidence` | when the verdict was given and how sure the reviewer was |
| `corrected_name`, `corrected_species_id`, `not_a_fish`, `unidentifiable`, `life_stage` | the human label (`not_a_fish` = no animal in the box; `unidentifiable` = an animal whose species can't be told) |
| `is_endorsement`, `label_source` | whether the label matches the model's guess; how it was chosen (`model_guess`, `hint`, `search`) |
| `corrected_bbox` | the box as the reviewer fixed it, `x1,y1,x2,y2`; empty when they did not move it (use `bbox`) |
| `review_item_id`, `is_adjudication` | the review-queue item it answered; whether an administrator settled a disagreement |
| `bbox`, `det_conf`, `track_id` | the model's box and detection context |
| `original_name`, `original_species_id`, `original_accuracy`, `original_topk` | what the model said |
| `frame_image_path`, `frame_coco_path` | the saved frame, matched on camera, frame number and timestamp |
| `model_version`, `detector_sha256`, `classifier_sha256`, `config_hash`, `pipeline_git_sha` | provenance of the original prediction |
| `reviewer`, `notes`, `user_id`, `session_id` | **administrators only.** Omitted from the file for everyone else |
```

and add this sentence to the end of the `corrections.csv` section: "`reviewer`, `notes`, `user_id` and `session_id` appear only when an administrator downloads the file; filtering by `reviewer` also requires the admin role."

- [ ] **Step 6: Run the tests to verify they pass**

Run: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn timeout 900 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest tests/test_privacy_exports.py tests/test_auth_db.py -q`
Expected: all pass.

- [ ] **Step 7: Check that the old dataset builder still reads the export**

Run: `grep -n "reviewer\|notes" scripts/build_finetuning_dataset.py`
Expected: any use is `row.get(...)`-style or absent, so a missing column is harmless. If a line indexes `r["reviewer"]` or `r["notes"]` directly, change it to `r.get("reviewer")` / `r.get("notes")` in this task.

- [ ] **Step 8: Commit**

```bash
git add services/dashboard/app/main.py services/dashboard/app/exports.py services/dashboard/app/data/README.md tests/test_privacy_exports.py
git commit -m "exports: keep volunteers' names out of public files; match frames on timestamp

Since accounts landed, the reviewer column is a real person's name, and
three public endpoints were publishing it with their free-text notes.
Identity columns now go to administrators only, and filtering by
reviewer needs the admin role.

Four code paths found a box's frame by frame_id and camera alone.
frame_id restarts with every worker restart, so that could name a
different frame; they now match the timestamp too. labeled_corrections
also gains the corrected box and the new verdict columns: until now a
reviewer's box fix never left the database.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: WoRMS names for every Species Log entry and classifier class

**Files:**
- Modify: `scripts/enrich_taxonomy_from_worms.py`
- Regenerate: `assets/taxonomy.csv`, create `assets/taxonomy_unresolved.txt`
- Test: `tests/test_enrich_taxonomy.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `assets/taxonomy.csv` with header
  `raw_name,aphia_id,accepted_name,authority,rank,match_type,status,species,genus,family,order,class,phylum,kingdom,lowest`, one row per distinct raw name (Species Log `binomial` values and classifier class names with underscores replaced by spaces, whitespace collapsed). `accepted_name` is empty when WoRMS found nothing. Functions: `clean_query_name(raw) -> str | None`, `collect_raw_names(manifest_records, class_names) -> list[str]`, `resolve_names(raw_names, match_batch) -> dict[str, dict]`, `build_rows(resolved, manifest_records) -> list[dict]`, `unresolved_lines(resolved) -> list[str]`, `main(argv=None, match_batch=None) -> int`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_enrich_taxonomy.py
"""scripts/enrich_taxonomy_from_worms.py, against a fake WoRMS."""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import enrich_taxonomy_from_worms as enr  # noqa: E402


def _rec(name, aphia, *, valid=None, valid_id=None, status="accepted", match="exact",
         rank="Species", family="Fam"):
    genus = name.split()[0]
    return {"AphiaID": aphia, "scientificname": name, "authority": "Someone, 1900",
            "status": status, "rank": rank, "valid_AphiaID": valid_id or aphia,
            "valid_name": valid or name, "valid_authority": "Someone, 1900",
            "match_type": match, "kingdom": "Animalia", "phylum": "Chordata",
            "class": "Teleostei", "order": "Ord", "family": family, "genus": genus}


FAKE = {
    "Seriola zonata": _rec("Seriola zonata", 1),
    # a misspelling: WoRMS matches it phonetically to an unaccepted record
    # whose accepted name is the correct spelling
    "Pomatomus saltarix": _rec("Pomatomus saltarix", 2, valid="Pomatomus saltatrix",
                               valid_id=20, status="unaccepted", match="phonetic"),
    "Anamobaea": _rec("Anamobaea", 3, rank="Genus"),
    "Ghostus": _rec("Ghostus", 4, rank="Genus"),
}


def fake_batch(names):
    return {n: FAKE.get(n) for n in names}


def test_clean_query_name():
    assert enr.clean_query_name("Seriola zonata") == "Seriola zonata"
    assert enr.clean_query_name("Seriola_zonata") == "Seriola zonata"
    assert enr.clean_query_name("Anamobaea .") == "Anamobaea"
    assert enr.clean_query_name("Anilocra ./Renocila") == "Anilocra"
    assert enr.clean_query_name("TBC") is None
    assert enr.clean_query_name("  ") is None


def test_collect_raw_names_merges_log_and_classes():
    raws = enr.collect_raw_names(
        [{"binomial": "Seriola zonata"}, {"binomial": None}, {"binomial": " Anamobaea  . "}],
        ["Seriola_zonata", "Pomatomus_saltarix"])
    assert raws == ["Anamobaea .", "Pomatomus saltarix", "Seriola zonata"]


def test_resolve_names_keeps_the_accepted_name_and_its_id():
    out = enr.resolve_names(["Pomatomus saltarix", "Seriola zonata"], fake_batch)
    assert out["Pomatomus saltarix"]["accepted_name"] == "Pomatomus saltatrix"
    assert out["Pomatomus saltarix"]["aphia_id"] == 20
    assert out["Pomatomus saltarix"]["match_type"] == "phonetic"
    assert out["Seriola zonata"]["accepted_name"] == "Seriola zonata"


def test_resolve_names_falls_back_to_the_genus():
    out = enr.resolve_names(["Ghostus unknownii", "Nothing here"], fake_batch)
    assert out["Ghostus unknownii"]["accepted_name"] == "Ghostus"
    assert out["Ghostus unknownii"]["via"] == "genus"
    assert out["Nothing here"]["accepted_name"] is None


def test_unresolved_lines_flag_fuzzy_genus_only_and_missing():
    out = enr.resolve_names(["Pomatomus saltarix", "Ghostus unknownii", "Nothing here",
                             "Seriola zonata"], fake_batch)
    text = "\n".join(enr.unresolved_lines(out))
    assert "Pomatomus saltarix\tfuzzy match (phonetic)\tPomatomus saltatrix" in text
    assert "Ghostus unknownii\tgenus only\tGhostus" in text
    assert "Nothing here\tno match\t" in text
    assert "Seriola zonata" not in text


def test_main_writes_the_csv_and_the_unresolved_list(tmp_path):
    manifest = tmp_path / "species_manifest.json"
    manifest.write_text(json.dumps([{"binomial": "Seriola zonata", "family": "Carangidae",
                                     "genus": "Seriola"}]))
    classes = tmp_path / "classes.json"
    classes.write_text(json.dumps(["Pomatomus_saltarix"]))
    out_csv = tmp_path / "taxonomy.csv"
    unresolved = tmp_path / "taxonomy_unresolved.txt"
    code = enr.main(["--manifest", str(manifest), "--classes", str(classes),
                     "--out", str(out_csv), "--unresolved", str(unresolved)],
                    match_batch=fake_batch)
    assert code == 0
    rows = {r["raw_name"]: r for r in csv.DictReader(out_csv.open())}
    assert set(rows) == {"Seriola zonata", "Pomatomus saltarix"}
    assert rows["Pomatomus saltarix"]["accepted_name"] == "Pomatomus saltatrix"
    assert rows["Seriola zonata"]["family"] == "Fam"
    assert list(rows["Seriola zonata"])[:3] == ["raw_name", "aphia_id", "accepted_name"]
    assert "Pomatomus saltarix" in unresolved.read_text()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `timeout 300 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest tests/test_enrich_taxonomy.py -q`
Expected: FAIL with `AttributeError: module 'enrich_taxonomy_from_worms' has no attribute 'clean_query_name'`.

- [ ] **Step 3: Rewrite the script around raw names**

In `scripts/enrich_taxonomy_from_worms.py`:

1. Replace the module docstring's first paragraph and the "CSV schema" paragraph with:

```python
"""Map every species name the project uses to its WoRMS record.

Inputs: the Species Log manifest (``assets/species_manifest.json``) and
the classifier's ``classes.json``. Both contain names as typed by
people, misspellings included. Every distinct name becomes one row of
``assets/taxonomy.csv``, keyed by ``raw_name``, carrying the WoRMS
AphiaID and accepted name, so labels can be stored under the accepted
name while training folders keep their original spelling.

CSV schema (order is fixed):
    raw_name,aphia_id,accepted_name,authority,rank,match_type,status,
    species,genus,family,order,class,phylum,kingdom,lowest

``accepted_name`` is empty when WoRMS found nothing. The last eight
columns keep their earlier meaning (``species`` is the epithet;
``lowest`` is the most specific rank resolved). Names that WoRMS could
not resolve, matched only fuzzily, or matched only to a genus are
written to ``assets/taxonomy_unresolved.txt`` for the Species Log's
maintainers.
```

Keep the remaining docstring paragraphs about `marine_only=false` and running idempotently.

2. Add `import argparse`, `import os` and `from collections.abc import Callable` to the imports, and add to the constants:

```python
DEFAULT_CLASSES = Path(os.environ.get(
    "CLASSIFIER_V2_CLASSES_JSON",
    str(REPO_ROOT / "data" / "models" / "classifier_v2" / "classes.json")))
OUT_UNRESOLVED = REPO_ROOT / "assets" / "taxonomy_unresolved.txt"
FIELDNAMES = ["raw_name", "aphia_id", "accepted_name", "authority", "rank", "match_type",
              "status", "species", "genus", "family", "order", "class", "phylum",
              "kingdom", "lowest"]
```

3. Delete `fetch_taxonomy` and the old `main`. Keep `pick_best_match`, `worms_match_batch`, `_flatten`, `_carry_down`, `_species_epithet` and `_epithet_from_scientificname` unchanged. Add:

```python
MatchBatch = Callable[[list[str]], dict[str, dict | None]]


def clean_query_name(raw: str | None) -> str | None:
    """The name to send to WoRMS: 'Genus epithet', or just 'Genus' when the
    epithet is a placeholder ('sp.', '.', '-'), or None when the text is
    not a Latin name at all ('TBC')."""
    if not raw:
        return None
    parts = raw.replace("_", " ").split()
    if not parts or not LATIN_GENUS_RE.match(parts[0]):
        return None
    if len(parts) >= 2 and LATIN_EPITHET_RE.match(parts[1]):
        return f"{parts[0]} {parts[1]}"
    return parts[0]


def collect_raw_names(manifest_records: list[dict], class_names: list[str]) -> list[str]:
    raws = set()
    for r in manifest_records:
        b = r.get("binomial")
        if b and b.strip():
            raws.add(" ".join(b.split()))
    for c in class_names:
        raws.add(" ".join(c.replace("_", " ").split()))
    return sorted(raws)


def _accepted_fields(rec: dict | None) -> dict:
    if not rec:
        return {"aphia_id": None, "accepted_name": None, "authority": None, "rank": None,
                "match_type": None, "status": None}
    return {
        "aphia_id": rec.get("valid_AphiaID") or rec.get("AphiaID"),
        "accepted_name": rec.get("valid_name") or rec.get("scientificname"),
        "authority": rec.get("valid_authority") or rec.get("authority"),
        "rank": rec.get("rank"),
        "match_type": rec.get("match_type"),
        "status": rec.get("status"),
    }


def _batched(match_batch: MatchBatch, names: list[str]) -> dict[str, dict | None]:
    found: dict[str, dict | None] = {}
    for i in range(0, len(names), WORMS_BATCH_SIZE):
        found.update(match_batch(names[i:i + WORMS_BATCH_SIZE]))
    return found


def resolve_names(raw_names: list[str], match_batch: MatchBatch) -> dict[str, dict]:
    """Resolve each raw name: first as written (cleaned), then, for a
    binomial with no match, by its genus alone."""
    queries = {raw: clean_query_name(raw) for raw in raw_names}
    found = _batched(match_batch, sorted({q for q in queries.values() if q}))
    genera = sorted({q.split()[0] for q in found if found[q] is None and " " in q})
    by_genus = _batched(match_batch, genera) if genera else {}
    out: dict[str, dict] = {}
    for raw, query in queries.items():
        rec, via = None, None
        if query:
            rec = found.get(query)
            via = "name" if rec else None
            if rec is None and " " in query:
                rec = by_genus.get(query.split()[0])
                via = "genus" if rec else None
        out[raw] = {"raw_name": raw, "query": query, "via": via, "record": rec,
                    **_accepted_fields(rec)}
    return out


def build_rows(resolved: dict[str, dict], manifest_records: list[dict]) -> list[dict]:
    families = {" ".join(r["binomial"].split()): r.get("family")
                for r in manifest_records if r.get("binomial")}
    rows = []
    for raw in sorted(resolved):
        res = resolved[raw]
        rec = res["record"]
        if rec:
            # Take the epithet from the accepted name, not the matched one: a
            # misspelled name matches an unaccepted record carrying the typo.
            accepted = {**rec, "scientificname": res["accepted_name"] or rec.get("scientificname")}
            ranks = _carry_down(_flatten(
                accepted, lowest="species" if res["via"] == "name" else "genus",
                fallback_species=_species_epithet(raw)))
        else:
            query = res["query"] or ""
            ranks = {"kingdom": None, "phylum": None, "class": None, "order": None,
                     "family": families.get(raw), "genus": query.split()[0] if query else None,
                     "species": _species_epithet(query) if query else None,
                     "lowest": "unknown"}
        row = {k: res.get(k) for k in ("raw_name", "aphia_id", "accepted_name", "authority",
                                        "rank", "match_type", "status")}
        row.update({k: ranks.get(k) for k in ("species", "genus", "family", "order", "class",
                                              "phylum", "kingdom", "lowest")})
        rows.append(row)
    return rows


def unresolved_lines(resolved: dict[str, dict]) -> list[str]:
    """Tab-separated: raw name, what is wrong, WoRMS's accepted name if any."""
    lines = []
    for raw in sorted(resolved):
        res = resolved[raw]
        if res["record"] is None:
            problem = "no match"
        elif res["via"] == "genus" or (res["query"] and " " in res["query"]
                                       and (res.get("rank") or "").lower() == "genus"):
            problem = "genus only"
        elif res.get("match_type") and res["match_type"] != "exact":
            problem = f"fuzzy match ({res['match_type']})"
        else:
            continue
        lines.append(f"{raw}\t{problem}\t{res.get('accepted_name') or ''}")
    return lines


def _live_match_batch() -> MatchBatch:
    client = httpx.Client()

    def match(names: list[str]) -> dict[str, dict | None]:
        print(f"  worms: {len(names)} names")
        result = worms_match_batch(client, names)
        time.sleep(0.5)   # polite pause between batches
        return result

    return match


def main(argv: list[str] | None = None, match_batch: MatchBatch | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    ap.add_argument("--classes", type=Path, default=DEFAULT_CLASSES)
    ap.add_argument("--out", type=Path, default=OUT_CSV)
    ap.add_argument("--unresolved", type=Path, default=OUT_UNRESOLVED)
    args = ap.parse_args(argv)

    if not args.manifest.exists():
        print(f"missing manifest: {args.manifest} (run build_species_manifest.py first)",
              file=sys.stderr)
        return 1
    records = json.loads(args.manifest.read_text())
    classes = json.loads(args.classes.read_text()) if args.classes.exists() else []
    if not classes:
        print(f"note: no classifier classes read from {args.classes}", file=sys.stderr)

    raw_names = collect_raw_names(records, classes)
    print(f"resolving {len(raw_names)} distinct names via WoRMS")
    resolved = resolve_names(raw_names, match_batch or _live_match_batch())

    rows = build_rows(resolved, records)
    with args.out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()
        for row in rows:
            w.writerow({k: ("" if row.get(k) is None else row[k]) for k in FIELDNAMES})
    problems = unresolved_lines(resolved)
    args.unresolved.write_text(
        "# raw_name\tproblem\taccepted_name  (fix these names in the Species Log)\n"
        + "".join(line + "\n" for line in problems))
    print(f"wrote {args.out} ({len(rows)} rows); {len(problems)} names need attention "
          f"(see {args.unresolved})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `timeout 300 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest tests/test_enrich_taxonomy.py -q`
Expected: 6 passed.

- [ ] **Step 5: Regenerate the real taxonomy against WoRMS**

The inputs are gitignored, so read them from the main checkout. This makes about a dozen read-only requests to `www.marinespecies.org`.

```bash
timeout 600 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python scripts/enrich_taxonomy_from_worms.py \
  --manifest /raid/scratch/dzimmerman2021/wahoobay/assets/species_manifest.json \
  --classes /raid/scratch/dzimmerman2021/wahoobay/data/models/classifier_v2/classes.json \
  --out assets/taxonomy.csv --unresolved assets/taxonomy_unresolved.txt
```

Expected: "wrote assets/taxonomy.csv (N rows)" with N roughly 230–270 (the Species Log's distinct names plus the handful of classifier classes it lacks), and a count of names needing attention. Check the result:

```bash
head -3 assets/taxonomy.csv
grep -c "" assets/taxonomy_unresolved.txt
grep -i "^Pomatomus" assets/taxonomy.csv
```

Expected: the header starts `raw_name,aphia_id,accepted_name`; the `Pomatomus saltarix` row has `accepted_name` `Pomatomus saltatrix`. If WoRMS is unreachable (HTTP errors in the output), stop and report it; do not commit a CSV with empty `accepted_name` columns.

- [ ] **Step 6: Commit**

```bash
git add scripts/enrich_taxonomy_from_worms.py assets/taxonomy.csv assets/taxonomy_unresolved.txt tests/test_enrich_taxonomy.py
git commit -m "taxonomy: resolve every Species Log name and classifier class through WoRMS

Labels will be stored under WoRMS accepted names, but the Species Log
and the classifier's class list both carry misspellings (Pomatomus
saltarix) and placeholders. taxonomy.csv is now keyed by the raw name as
written, with the AphiaID and accepted name alongside, and the names
that need a human go to taxonomy_unresolved.txt for the sheet's
maintainers.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Species catalogue

**Files:**
- Create: `services/dashboard/app/review_catalogue.py`
- Test: `tests/test_review_catalogue.py`

**Interfaces:**
- Consumes: `assets/taxonomy.csv` in Task 3's format; `tests/review_fixtures.write_review_assets`, `catalogue_paths`.
- Produces: `normalize_raw(raw: str) -> str`; `slug(name: str) -> str`; `CataloguePaths(species_manifest, classes_json, taxonomy_csv, inat_manifest, celebrities_json, photo_root)` with `.watched() -> tuple[Path, ...]`; `CatalogueEntry` (fields `name, life_stage, common_name, aphia_id, rank, family, resolved, raw_names, class_dir, in_species_log, in_classifier, photo_path, photo_credit, celebrity_ids`, property `key == f"{name}|{life_stage or ''}"`); `Catalogue` with `entries`, `celebrities_by_id`, `accepted(raw) -> str | None`, `entry(name, life_stage) -> CatalogueEntry | None`, `has_name(name) -> bool`, `names() -> list[str]`, `class_dir(name) -> str | None`, `common_name(name) -> str | None`, `celebrity_ids(name) -> set[str]`, `photo(slug) -> tuple[Path, str | None] | None`, `to_json() -> list[dict]`; `load_catalogue(paths) -> Catalogue`; `CatalogueStore(paths)` with `.get() -> Catalogue`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_review_catalogue.py
"""The species catalogue (spec §9): Species Log + classifier classes +
WoRMS names + photos + named individuals."""
from __future__ import annotations

import json
import os
import time

import pytest
from review_fixtures import catalogue_paths, write_review_assets

from app.review_catalogue import (
    CataloguePaths,
    CatalogueStore,
    load_catalogue,
    normalize_raw,
    slug,
)


@pytest.fixture
def cat(tmp_path):
    return load_catalogue(catalogue_paths(write_review_assets(tmp_path)))


def test_normalize_raw():
    assert normalize_raw("  Pomatomus_saltarix ") == "Pomatomus saltarix"


def test_misspelled_names_map_to_the_accepted_name(cat):
    assert cat.accepted("Pomatomus saltarix") == "Pomatomus saltatrix"
    assert cat.accepted("Pomatomus_saltarix") == "Pomatomus saltatrix"
    assert cat.accepted("Unheard of") == "Unheard of"
    assert cat.accepted(None) is None
    assert cat.class_dir("Pomatomus saltatrix") == "Pomatomus_saltarix"
    e = cat.entry("Pomatomus saltatrix", None)
    assert e.resolved and e.aphia_id == 900004 and e.in_species_log and e.in_classifier


def test_life_stages_are_separate_entries(cat):
    assert cat.entry("Pomacanthus paru", "adult").common_name == "Adult French Angelfish"
    assert cat.entry("Pomacanthus paru", "juvenile").common_name == "Juvenile French Angelfish"
    assert cat.entry("Pomacanthus paru", None) is None
    assert cat.entry("Seriola zonata", None) is not None     # 'unknown' stage means none
    assert cat.class_dir("Pomacanthus paru") == "Pomacanthus_paru"


def test_classifier_only_and_unresolved_entries(cat):
    neo = cat.entry("Neopomacentrus cyanomos", None)
    assert neo.in_classifier and not neo.in_species_log
    assert neo.common_name == "Regal Demoiselle"              # from the iNat manifest
    ani = cat.entry("Anilocra ./Renocila", None)
    assert ani is not None and not ani.resolved and not ani.in_classifier
    assert len(cat.entries) == 7                               # the row with no binomial is skipped


def test_photos_carry_credit_and_never_leave_the_photo_root(cat):
    path, credit = cat.photo(slug("Seriola zonata"))
    assert path.name == "11.jpg" and "CC BY-NC" in credit
    assert cat.photo(slug("Acanthurus coeruleus")) is None     # its local_path escapes the root


def test_named_individuals_attach_to_their_species(cat):
    assert cat.celebrity_ids("Acanthurus coeruleus") == {"bobby"}
    js = next(x for x in cat.to_json() if x["name"] == "Acanthurus coeruleus")
    assert js["celebrities"] == [{"id": "bobby", "nickname": "Bobby"}]


def test_json_shape(cat):
    js = {x["key"]: x for x in cat.to_json()}
    s = js["Seriola zonata|"]
    assert s["photo_url"] == "/api/review/species/Seriola_zonata/photo.jpg"
    assert s["common"] == "Banded Rudderfish" and s["aphia_id"] == 900005
    assert js["Anilocra ./Renocila|"]["photo_url"] is None


def test_store_rebuilds_when_a_source_file_changes(tmp_path):
    info = write_review_assets(tmp_path)
    store = CatalogueStore(catalogue_paths(info))
    first = store.get()
    assert store.get() is first
    manifest = info["assets"] / "species_manifest.json"
    data = json.loads(manifest.read_text())
    data.append({"binomial": "Caranx crysos", "common_name": "Blue Runner",
                 "life_stage": "unknown"})
    manifest.write_text(json.dumps(data))
    later = time.time_ns() + 10**9
    os.utime(manifest, ns=(later, later))
    second = store.get()
    assert second is not first and second.entry("Caranx crysos", None) is not None


def test_missing_files_give_an_empty_working_catalogue(tmp_path):
    paths = CataloguePaths(tmp_path / "a.json", tmp_path / "b.json", tmp_path / "c.csv",
                           tmp_path / "d.json", tmp_path / "e.json", photo_root=tmp_path)
    cat = load_catalogue(paths)
    assert cat.entries == [] and cat.accepted("X y") == "X y"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `timeout 300 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest tests/test_review_catalogue.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.review_catalogue'`.

- [ ] **Step 3: Write `review_catalogue.py`**

```python
"""What a volunteer can pick as a label, and how raw names map to it.

Built from files that are only read here:

* the Species Log manifest (`species_manifest.json`, rebuilt daily by
  the sheet poller): common names and life stages;
* the classifier's `classes.json`: the training classes, some spelled
  the way the sheet once spelled them;
* `taxonomy.csv` (scripts/enrich_taxonomy_from_worms.py): every raw
  name above mapped to its WoRMS accepted name;
* `inat_manifest.json` and `celebrities.json`: reference photos with
  their attribution, and named individuals.

Labels are stored under the accepted name, so a misspelled entry and a
correctly spelled one are the same species. `class_dir` remembers the
training folder that species lives in, because folders keep the
original spelling. CatalogueStore rebuilds the catalogue whenever one of
the files changes, so a sheet edit reaches the page without a restart.
"""
from __future__ import annotations

import csv
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("wahoobay.dashboard.review_catalogue")

_NO_STAGE = {"", "unknown", "none", "n/a"}


def normalize_raw(raw: str) -> str:
    """'Pomatomus_saltarix ' -> 'Pomatomus saltarix'."""
    return " ".join(raw.replace("_", " ").split())


def slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")


@dataclass(frozen=True)
class CataloguePaths:
    species_manifest: Path
    classes_json: Path
    taxonomy_csv: Path
    inat_manifest: Path
    celebrities_json: Path
    photo_root: Path                # iNat `local_path` values are relative to this

    def watched(self) -> tuple[Path, ...]:
        return (self.species_manifest, self.classes_json, self.taxonomy_csv,
                self.inat_manifest, self.celebrities_json)


@dataclass
class CatalogueEntry:
    name: str
    life_stage: str | None
    common_name: str | None = None
    aphia_id: int | None = None
    rank: str | None = None
    family: str | None = None
    resolved: bool = False
    raw_names: set[str] = field(default_factory=set)
    class_dir: str | None = None
    in_species_log: bool = False
    in_classifier: bool = False
    photo_path: Path | None = None
    photo_credit: str | None = None
    celebrity_ids: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.name}|{self.life_stage or ''}"


class Catalogue:
    def __init__(self, entries, accepted_by_raw: dict[str, str],
                 celebrities_by_id: dict[str, dict]) -> None:
        self.entries = sorted(entries, key=lambda e: ((e.common_name or e.name).lower(), e.key))
        self._accepted = dict(accepted_by_raw)
        self._by_key = {e.key: e for e in self.entries}
        self._by_name: dict[str, list[CatalogueEntry]] = {}
        for e in self.entries:
            self._by_name.setdefault(e.name, []).append(e)
        self.celebrities_by_id = dict(celebrities_by_id)
        self._photos = {slug(e.name): e for e in self.entries if e.photo_path}

    def accepted(self, raw: str | None) -> str | None:
        if raw is None:
            return None
        n = normalize_raw(raw)
        return self._accepted.get(n, n)

    def entry(self, name: str, life_stage: str | None) -> CatalogueEntry | None:
        return self._by_key.get(f"{name}|{life_stage or ''}")

    def has_name(self, name: str) -> bool:
        return name in self._by_name

    def names(self) -> list[str]:
        return sorted(self._by_name)

    def class_dir(self, name: str) -> str | None:
        return next((e.class_dir for e in self._by_name.get(name, []) if e.class_dir), None)

    def common_name(self, name: str | None) -> str | None:
        return next((e.common_name for e in self._by_name.get(name or "", [])
                     if e.common_name), None)

    def celebrity_ids(self, name: str | None) -> set[str]:
        return {c for e in self._by_name.get(name or "", []) for c in e.celebrity_ids}

    def photo(self, slug_: str) -> tuple[Path, str | None] | None:
        e = self._photos.get(slug_)
        return (e.photo_path, e.photo_credit) if e else None

    def to_json(self) -> list[dict]:
        out = []
        for e in self.entries:
            out.append({
                "key": e.key, "name": e.name, "life_stage": e.life_stage,
                "common": e.common_name, "aphia_id": e.aphia_id, "rank": e.rank,
                "family": e.family, "resolved": e.resolved,
                "in_species_log": e.in_species_log, "in_classifier": e.in_classifier,
                "photo_url": (f"/api/review/species/{slug(e.name)}/photo.jpg"
                              if e.photo_path else None),
                "photo_credit": e.photo_credit,
                "celebrities": [
                    {"id": c, "nickname": (self.celebrities_by_id.get(c) or {}).get("nickname") or c}
                    for c in e.celebrity_ids],
            })
        return out


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        log.warning("catalogue: %s not found", path)
        return default


def _read_taxonomy(path: Path) -> dict[str, dict]:
    if not path.exists():
        log.warning("catalogue: %s not found; names will not be reconciled", path)
        return {}
    with path.open(newline="") as f:
        return {normalize_raw(r["raw_name"]): r for r in csv.DictReader(f) if r.get("raw_name")}


def load_catalogue(paths: CataloguePaths) -> Catalogue:
    taxonomy = _read_taxonomy(paths.taxonomy_csv)
    accepted_by_raw = {raw: row["accepted_name"] for raw, row in taxonomy.items()
                       if row.get("accepted_name")}
    entries: dict[str, CatalogueEntry] = {}

    def entry_for(raw: str, stage: str | None) -> CatalogueEntry:
        raw_n = normalize_raw(raw)
        name = accepted_by_raw.get(raw_n, raw_n)
        key = f"{name}|{stage or ''}"
        e = entries.get(key)
        if e is None:
            e = entries[key] = CatalogueEntry(name=name, life_stage=stage)
            row = taxonomy.get(raw_n)
            if row and row.get("accepted_name"):
                e.resolved = True
                aphia = (row.get("aphia_id") or "").strip()
                e.aphia_id = int(aphia) if aphia.isdigit() else None
                e.rank = row.get("rank") or None
                e.family = row.get("family") or None
        e.raw_names.add(raw_n)
        return e

    for rec in _read_json(paths.species_manifest, []):
        raw = rec.get("binomial")
        if not raw or not normalize_raw(raw):
            continue
        stage = (rec.get("life_stage") or "").strip().lower()
        e = entry_for(raw, None if stage in _NO_STAGE else stage)
        e.in_species_log = True
        e.common_name = e.common_name or rec.get("common_name") or None
        e.family = e.family or rec.get("family") or None

    for cls in _read_json(paths.classes_json, []):
        name = accepted_by_raw.get(normalize_raw(cls), normalize_raw(cls))
        targets = [e for e in entries.values() if e.name == name] or [entry_for(cls, None)]
        for e in targets:
            e.in_classifier = True
            e.class_dir = cls
            e.raw_names.add(normalize_raw(cls))

    root = paths.photo_root.resolve()
    photos: dict[str, dict] = {}
    for p in sorted(_read_json(paths.inat_manifest, []),
                    key=lambda r: (str(r.get("binomial")), r.get("photo_id") or 0)):
        raw, local = p.get("binomial"), p.get("local_path")
        if not raw or not local:
            continue
        name = accepted_by_raw.get(normalize_raw(raw), normalize_raw(raw))
        if name in photos:
            continue
        path = (root / local).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            continue
        photos[name] = {"path": path, "credit": p.get("attribution"),
                        "common": p.get("common_name")}
    for e in entries.values():
        ph = photos.get(e.name)
        if ph:
            e.photo_path, e.photo_credit = ph["path"], ph["credit"]
            e.common_name = e.common_name or ph["common"]

    celebs = (_read_json(paths.celebrities_json, {}) or {}).get("celebrities") or []
    celebrities_by_id = {c["id"]: c for c in celebs if c.get("id")}
    for c in celebs:
        if not c.get("id") or not c.get("binomial"):
            continue
        name = accepted_by_raw.get(normalize_raw(c["binomial"]), normalize_raw(c["binomial"]))
        for e in entries.values():
            if e.name == name and c["id"] not in e.celebrity_ids:
                e.celebrity_ids.append(c["id"])

    return Catalogue(entries.values(), accepted_by_raw, celebrities_by_id)


class CatalogueStore:
    """The current catalogue, rebuilt when any of its source files changes."""

    def __init__(self, paths: CataloguePaths) -> None:
        self.paths = paths
        self._catalogue: Catalogue | None = None
        self._stamp: tuple | None = None

    def _current_stamp(self) -> tuple:
        out = []
        for p in self.paths.watched():
            try:
                out.append(p.stat().st_mtime_ns)
            except FileNotFoundError:
                out.append(None)
        return tuple(out)

    def get(self) -> Catalogue:
        stamp = self._current_stamp()
        if self._catalogue is None or stamp != self._stamp:
            try:
                self._catalogue = load_catalogue(self.paths)
            except Exception:
                log.exception("catalogue: rebuild failed; keeping the previous one")
                if self._catalogue is None:
                    self._catalogue = Catalogue([], {}, {})
            self._stamp = stamp
        return self._catalogue
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `timeout 300 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest tests/test_review_catalogue.py -q`
Expected: 9 passed.

- [ ] **Step 5: Lint and commit**

```bash
/raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/ruff check services/dashboard/app/review_catalogue.py tests/test_review_catalogue.py
git add services/dashboard/app/review_catalogue.py tests/test_review_catalogue.py
git commit -m "review: species catalogue from the Species Log, classifier and WoRMS

The picker offers every Species Log entry and every classifier class,
one entry per life stage, under the WoRMS accepted name, with a
reference photo and its credit. It remembers each species' training
folder, whose name keeps the original spelling, and rebuilds itself
when the daily sheet refresh rewrites the manifest.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Verdict resolution and agreement

**Files:**
- Create: `services/dashboard/app/review_labels.py`
- Test: `tests/test_review_labels.py`

**Interfaces:**
- Consumes: the schema from Task 1; fixtures from Task 1.
- Produces: `CONFIDENCE_RANK`, `VERIFIED_STATUSES = ("single", "agreed", "adjudicated")`; frozen dataclass `Verdict(id, event_id, user_id, created_at, corrected_name, not_a_fish, unidentifiable, life_stage=None, corrected_bbox=None, confidence="certain", label_source=None, is_adjudication=False, item_pass=None, session_id=None)`; frozen dataclass `Resolved(event_id, status, outcome, life_stage, confidence, bbox, deciding, user_ids, first=None, second=None)` with properties `is_verified: bool` and `species: str | None`; `outcome_of(v) -> tuple`; `resolve(verdicts) -> Resolved | None`; `agreement_pairs(resolved) -> list[tuple]`; `percent_agreement(pairs) -> float | None`; `krippendorff_alpha_nominal(pairs) -> float | None`; `agreement_summary(resolved, min_pairs=30) -> dict`; `async load_verdicts(conn, event_ids=None) -> dict[int, list[Verdict]]`; `async resolve_all(conn, event_ids=None) -> dict[int, Resolved]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_review_labels.py
"""The one definition of "verified" (spec §8, Resolution) and the
agreement statistic (spec §8, Agreement)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from review_fixtures import add_item, add_verdict, make_user, seed_frame

from app.review_labels import (
    Verdict,
    agreement_summary,
    krippendorff_alpha_nominal,
    outcome_of,
    percent_agreement,
    resolve,
    resolve_all,
)

T = datetime(2026, 6, 1, tzinfo=UTC)


def V(id, user=1, t=0, *, name="Seriola zonata", no_animal=False, unid=False, stage=None,
      bbox=None, conf="certain", source=None, adj=False, item_pass=None, event=7):
    return Verdict(id=id, event_id=event, user_id=user, created_at=T + timedelta(seconds=t),
                   corrected_name=None if (no_animal or unid) else name,
                   not_a_fish=no_animal, unidentifiable=unid, life_stage=stage,
                   corrected_bbox=bbox, confidence=conf, label_source=source,
                   is_adjudication=adj, item_pass=item_pass)


def test_outcomes():
    assert outcome_of(V(1)) == ("species", "Seriola zonata")
    assert outcome_of(V(1, no_animal=True)) == ("no_animal",)
    assert outcome_of(V(1, unid=True)) == ("unidentifiable",)


def test_a_single_verdict_is_verified():
    r = resolve([V(1)])
    assert r.status == "single" and r.species == "Seriola zonata" and r.is_verified


def test_the_latest_first_pass_verdict_wins_after_a_reopen():
    r = resolve([V(1, t=0, name="Aa bb"), V(2, t=60, name="Cc dd")])
    assert r.species == "Cc dd" and r.deciding == (2,)


def test_legacy_rows_without_a_user_are_ignored():
    assert resolve([V(1, user=None)]) is None
    r = resolve([V(1, user=None, no_animal=True), V(2, user=5)])
    assert r.outcome == ("species", "Seriola zonata")


def test_agreement_takes_the_less_certain_confidence():
    r = resolve([V(1, user=1), V(2, user=2, conf="uncertain", item_pass=2)])
    assert r.status == "agreed" and r.confidence == "uncertain" and r.user_ids == (1, 2)


def test_life_stage_and_box_differences_are_not_disagreements():
    r = resolve([V(1, stage="adult", bbox=(1, 2, 30, 40)),
                 V(2, user=2, stage="juvenile", item_pass=2)])
    assert r.status == "agreed" and r.bbox == (1, 2, 30, 40)


def test_disagreement_is_disputed_and_not_verified():
    r = resolve([V(1), V(2, user=2, item_pass=2, no_animal=True)])
    assert r.status == "disputed" and not r.is_verified
    assert r.outcome == ("species", "Seriola zonata")


def test_adjudication_overrides_everything():
    r = resolve([V(1), V(2, user=2, item_pass=2, no_animal=True),
                 V(3, user=9, t=100, adj=True, unid=True)])
    assert r.status == "adjudicated" and r.outcome == ("unidentifiable",)
    assert r.deciding == (3,) and r.is_verified


def test_a_spot_check_whose_first_verdict_was_legacy_counts_as_single():
    r = resolve([V(1, user=None), V(2, user=2, item_pass=2)])
    assert r.status == "single"


def test_the_box_falls_back_to_an_earlier_correction():
    # Reopened and re-answered without moving the box: the earlier fix stands.
    r = resolve([V(1, t=0, bbox=(5, 5, 50, 50)), V(2, t=10)])
    assert r.bbox == (5, 5, 50, 50)


def test_krippendorff_alpha_known_value():
    pairs = [("A", "A"), ("A", "A"), ("B", "B"), ("A", "B")]
    assert krippendorff_alpha_nominal(pairs) == pytest.approx(8 / 15)
    assert percent_agreement(pairs) == 0.75


def test_alpha_edge_cases():
    assert krippendorff_alpha_nominal([("A", "A"), ("B", "B")]) == 1.0
    assert krippendorff_alpha_nominal([("A", "B"), ("B", "A")]) == pytest.approx(-0.5)
    assert krippendorff_alpha_nominal([("A", "A")]) is None      # no variation at all
    assert krippendorff_alpha_nominal([]) is None
    assert percent_agreement([]) is None


def test_agreement_summary_splits_by_how_the_first_label_was_chosen():
    resolved = {
        1: resolve([V(1, event=1, source="model_guess"), V(2, event=1, user=2, item_pass=2)]),
        2: resolve([V(3, event=2, source="model_guess"),
                    V(4, event=2, user=2, item_pass=2, no_animal=True)]),
        3: resolve([V(5, event=3, source="search", name="Xx yy"),
                    V(6, event=3, user=2, item_pass=2, name="Xx yy")]),
        4: resolve([V(7, event=4)]),                              # never spot-checked
    }
    s = agreement_summary(resolved)
    assert s["overall"]["pairs"] == 3
    assert s["overall"]["percent"] == pytest.approx(2 / 3)
    assert s["by_label_source"]["model_guess"]["pairs"] == 2
    assert s["by_label_source"]["search"]["percent"] == 1.0
    assert s["min_pairs"] == 30


@pytest.mark.anyio
async def test_load_verdicts_reads_the_pass_from_the_review_item(rdb, tmp_path):
    frame = await seed_frame(rdb, tmp_path, boxes=[{"bbox": [10, 10, 100, 100],
                                                    "species": "Seriola zonata"}])
    one, two = await make_user(rdb, "one"), await make_user(rdb, "two")
    first_item = await add_item(rdb, frame, status="done")
    first = await add_verdict(rdb, frame["event_ids"][0], one, name="Seriola zonata",
                              item_id=first_item)
    second_item = await add_item(rdb, frame, pass_=2, status="done", exclude_user_id=one,
                                 first_correction_id=first)
    await add_verdict(rdb, frame["event_ids"][0], two, name="Seriola zonata",
                      item_id=second_item)
    async with rdb.connection() as conn:
        resolved = await resolve_all(conn)
    r = resolved[frame["event_ids"][0]]
    assert r.status == "agreed" and r.user_ids == (one, two)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn timeout 900 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest tests/test_review_labels.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.review_labels'`.

- [ ] **Step 3: Write `review_labels.py`**

```python
"""The one definition of "verified" (spec §8).

Every place that needs to know what a box's label is (the page, the
progress counts, the browse view, and phase 3's dataset builder) calls
resolve() here, so there is exactly one rule:

1. An administrator's adjudication, if any; the latest wins.
2. Otherwise the latest first-pass verdict, compared with the latest
   spot-check verdict if the box was spot-checked: the same outcome is
   "agreed", a different one is "disputed" and held out of the dataset.
3. Otherwise the latest first-pass verdict alone: "single".

Outcomes are compared as ("species", name), ("unidentifiable",) or
("no_animal",). Life stage and small box differences are not
disagreements. The 53 verdicts recorded before accounts existed have no
user_id and are ignored: anyone could write to that endpoint then.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime

from psycopg.rows import dict_row

CONFIDENCE_RANK = {"uncertain": 0, "probable": 1, "certain": 2}
VERIFIED_STATUSES = ("single", "agreed", "adjudicated")


@dataclass(frozen=True)
class Verdict:
    id: int
    event_id: int
    user_id: int | None
    created_at: datetime
    corrected_name: str | None
    not_a_fish: bool
    unidentifiable: bool
    life_stage: str | None = None
    corrected_bbox: tuple[int, int, int, int] | None = None
    confidence: str = "certain"
    label_source: str | None = None
    is_adjudication: bool = False
    item_pass: int | None = None
    session_id: int | None = None

    @property
    def order(self) -> tuple:
        return (self.created_at, self.id)


@dataclass(frozen=True)
class Resolved:
    event_id: int
    status: str                 # single | agreed | adjudicated | disputed
    outcome: tuple
    life_stage: str | None
    confidence: str
    bbox: tuple[int, int, int, int] | None   # None: use the model's box
    deciding: tuple[int, ...]
    user_ids: tuple[int, ...]
    first: Verdict | None = None
    second: Verdict | None = None

    @property
    def is_verified(self) -> bool:
        return self.status in VERIFIED_STATUSES

    @property
    def species(self) -> str | None:
        return self.outcome[1] if self.outcome[0] == "species" else None


def outcome_of(v: Verdict) -> tuple:
    if v.not_a_fish:
        return ("no_animal",)
    if v.unidentifiable:
        return ("unidentifiable",)
    return ("species", v.corrected_name)


def _latest(verdicts):
    return max(verdicts, key=lambda v: v.order) if verdicts else None


def _pick_bbox(deciding, everyone):
    for group in (deciding, everyone):
        for v in sorted(group, key=lambda v: v.order, reverse=True):
            if v.corrected_bbox:
                return tuple(v.corrected_bbox)
    return None


def resolve(verdicts) -> Resolved | None:
    eligible = [v for v in verdicts if v.user_id is not None]
    if not eligible:
        return None
    event_id = eligible[0].event_id
    adjudications = [v for v in eligible if v.is_adjudication]
    first = _latest([v for v in eligible if not v.is_adjudication and v.item_pass != 2])
    second = _latest([v for v in eligible if not v.is_adjudication and v.item_pass == 2])
    if adjudications:
        d = _latest(adjudications)
        return Resolved(event_id, "adjudicated", outcome_of(d), d.life_stage, d.confidence,
                        _pick_bbox([d], eligible), (d.id,), (d.user_id,), first, second)
    if first is None:
        first, second = second, None
    if second is None:
        return Resolved(event_id, "single", outcome_of(first), first.life_stage,
                        first.confidence, _pick_bbox([first], eligible), (first.id,),
                        (first.user_id,), first, None)
    status = "agreed" if outcome_of(first) == outcome_of(second) else "disputed"
    confidence = min(first.confidence, second.confidence,
                     key=lambda c: CONFIDENCE_RANK.get(c, 0))
    return Resolved(event_id, status, outcome_of(first), first.life_stage, confidence,
                    _pick_bbox([first, second], eligible), (first.id, second.id),
                    (first.user_id, second.user_id), first, second)


def agreement_pairs(resolved: dict) -> list[tuple]:
    """(first outcome, spot-check outcome, how the first label was chosen)
    for every box that has both."""
    return [(outcome_of(r.first), outcome_of(r.second), r.first.label_source)
            for r in resolved.values() if r.first is not None and r.second is not None]


def percent_agreement(pairs) -> float | None:
    if not pairs:
        return None
    return sum(1 for a, b in pairs if a == b) / len(pairs)


def krippendorff_alpha_nominal(pairs) -> float | None:
    """Krippendorff's alpha, nominal metric, for units that each carry two
    values from possibly different coders. Cohen's kappa assumes the same
    two raters for every unit; here each box's pair of volunteers differs.
    None when there is no variation to measure against."""
    if not pairs:
        return None
    coincidences: Counter = Counter()
    for a, b in pairs:
        coincidences[(a, b)] += 1
        coincidences[(b, a)] += 1
    marginals: Counter = Counter()
    for (c, _k), n in coincidences.items():
        marginals[c] += n
    total = sum(marginals.values())
    observed = sum(n for (c, k), n in coincidences.items() if c != k)
    expected = sum(marginals[c] * marginals[k] for c in marginals for k in marginals if c != k)
    if expected == 0:
        return None
    return 1.0 - (total - 1) * observed / expected


def agreement_summary(resolved: dict, min_pairs: int = 30) -> dict:
    pairs = agreement_pairs(resolved)

    def block(group):
        simple = [(a, b) for a, b, _source in group]
        return {"pairs": len(simple), "percent": percent_agreement(simple),
                "alpha": krippendorff_alpha_nominal(simple)}

    by_source: dict[str, list] = defaultdict(list)
    for p in pairs:
        by_source[p[2] or "none"].append(p)
    return {"overall": block(pairs), "min_pairs": min_pairs,
            "by_label_source": {k: block(v) for k, v in sorted(by_source.items())}}


_VERDICT_SQL = """
    SELECT c.id, c.event_id, c.user_id, c.created_at, c.corrected_name, c.not_a_fish,
           c.unidentifiable, c.life_stage, c.corrected_bbox, c.confidence, c.label_source,
           c.is_adjudication, c.session_id, ri.pass AS item_pass
      FROM detection_corrections c
      LEFT JOIN review_items ri ON ri.id = c.review_item_id
"""


def _row_to_verdict(r: dict) -> Verdict:
    bbox = r["corrected_bbox"]
    return Verdict(id=r["id"], event_id=r["event_id"], user_id=r["user_id"],
                   created_at=r["created_at"], corrected_name=r["corrected_name"],
                   not_a_fish=r["not_a_fish"], unidentifiable=r["unidentifiable"],
                   life_stage=r["life_stage"], corrected_bbox=tuple(bbox) if bbox else None,
                   confidence=r["confidence"], label_source=r["label_source"],
                   is_adjudication=r["is_adjudication"], item_pass=r["item_pass"],
                   session_id=r["session_id"])


async def load_verdicts(conn, event_ids: list[int] | None = None) -> dict[int, list[Verdict]]:
    sql, params = _VERDICT_SQL, None
    if event_ids is not None:
        sql, params = _VERDICT_SQL + " WHERE c.event_id = ANY(%s)", (list(event_ids),)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, params)
        rows = await cur.fetchall()
    out: dict[int, list[Verdict]] = defaultdict(list)
    for r in rows:
        out[r["event_id"]].append(_row_to_verdict(r))
    return dict(out)


async def resolve_all(conn, event_ids: list[int] | None = None) -> dict[int, Resolved]:
    out = {}
    for event_id, verdicts in (await load_verdicts(conn, event_ids)).items():
        r = resolve(verdicts)
        if r is not None:
            out[event_id] = r
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn timeout 900 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest tests/test_review_labels.py -q`
Expected: 14 passed.

- [ ] **Step 5: Lint and commit**

```bash
/raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/ruff check services/dashboard/app/review_labels.py tests/test_review_labels.py
git add services/dashboard/app/review_labels.py tests/test_review_labels.py
git commit -m "review: one rule for which verdict counts, and Krippendorff's alpha

Adjudication beats everything; otherwise the latest first-pass verdict,
agreed or disputed against a spot-check if there was one. Legacy rows
with no account are ignored. Agreement is Krippendorff's alpha rather
than Cohen's kappa because each box's pair of volunteers differs, and it
is split by how the first label was chosen, which is how anchoring to
the model's guesses would show up.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Images, the pool, and wiring it into the dashboard

**Files:**
- Create: `services/dashboard/app/review_images.py`
- Create: `services/dashboard/app/review_pool.py`
- Create: `scripts/dev/explain_review_probes.py`
- Modify: `services/dashboard/app/main.py` (`DashboardCfg`, the pool `max_size`, the lifespan)
- Test: `tests/test_review_pool.py`

**Interfaces:**
- Consumes: `CatalogueStore`, `catalogue_paths` (Task 4); `resolve_all` (Task 5).
- Produces:
  - `review_images`: `ZOOM_MARGIN = 0.5`, `ZOOM_MAX_SCALE = 3.0`, `ZOOM_MAX_EDGE = 960`; `frame_size(path: str) -> tuple[int, int]` (raises `FileNotFoundError`); `zoom_region(bbox, width, height, margin=ZOOM_MARGIN) -> tuple[int, int, int, int]`; `zoom_scale(region) -> float`; `render_zoom(path: str, region) -> bytes`; `render_frame(path: str, max_edge: int) -> bytes`.
  - `review_pool`: SQL constants `CHECK_PROBE_SQL`, `IDENTIFY_FRAMES_SQL`, `IDENTIFY_BOXES_SQL`; `PoolSettings` (+ `from_cfg(cfg)`); `big_enough(bbox, min_px) -> bool`; `async read_window(conn) -> tuple[datetime | None, datetime | None]`; `async retire_outside_window(conn, start, end) -> int`; `ReviewPool(db, settings, catalogue_store, baseline_path, rng=None)` with `async run_once() -> dict`, `async run_forever()`, `async refresh_progress()`, `baseline(name) -> int`, `verified(name) -> int`, `count(name) -> int`, `note_verified(name)`, `species_order() -> list[str]`, attribute `last_run: dict | None`.
  - `main.DashboardCfg`: `db_pool_max`, `review_pool_enabled`, `review_pool_interval_s`, `review_pool_max_probes`, `review_pool_max_run_s`, `review_check_open_per_species`, `review_identify_open`, `review_spacing_s`, `review_min_box_px`, `review_identify_min_det_conf`, `review_target`, `review_claim_s`, `review_spotcheck_rate`, `review_spotcheck_serve_rate`, `review_assets_dir`, `review_taxonomy_csv`, `review_classes_json`, `review_baseline_manifest`, and method `review_catalogue_paths() -> CataloguePaths`.
  - Lifespan `state` keys: `state["review_catalogue"]` (a `CatalogueStore`), `state["review_pool"]` (a `ReviewPool`, present whenever the database is).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_review_pool.py
"""The review pool (spec §6) and the image helpers it relies on."""
from __future__ import annotations

import io
import random
from datetime import timedelta

import pytest
from PIL import Image
from psycopg.types.json import Jsonb
from review_fixtures import T0, add_verdict, catalogue_paths, jpeg, make_user, seed_frame

from app.review_catalogue import CatalogueStore
from app.review_images import frame_size, render_zoom, zoom_region, zoom_scale
from app.review_pool import PoolSettings, ReviewPool


def test_zoom_region_adds_half_a_box_of_margin_and_clamps():
    assert zoom_region([100, 100, 200, 150], 1280, 720) == (50, 75, 250, 175)
    assert zoom_region([0, 0, 100, 100], 1280, 720) == (0, 0, 150, 150)
    assert zoom_region([1200, 650, 1280, 720], 1280, 720) == (1160, 615, 1280, 720)


def test_zoom_scale_caps_at_three_and_at_960_pixels():
    assert zoom_scale((0, 0, 100, 50)) == 3.0
    assert zoom_scale((0, 0, 1280, 720)) == pytest.approx(0.75)


def test_render_zoom_and_frame_size(tmp_path):
    path = jpeg(tmp_path / "f.jpg")
    assert frame_size(path) == (1280, 720)
    im = Image.open(io.BytesIO(render_zoom(path, (50, 75, 250, 175))))
    assert im.size == (600, 300)


pytestmark_db = pytest.mark.anyio


def _pool(rdb, review_assets, **overrides):
    kw = {"max_probes": 30, "max_run_s": 20, "check_open_per_species": 5, "identify_open": 0,
          "target": 5}
    kw.update(overrides)
    store = CatalogueStore(catalogue_paths(review_assets))
    return ReviewPool(rdb, PoolSettings(**kw), store, review_assets["baseline"],
                      rng=random.Random(0))


async def _items(rdb, lane=None):
    async with rdb.connection() as conn:
        cur = await conn.execute(
            """SELECT event_id, lane, saved_frame_id, predicted, route_species, status
                 FROM review_items WHERE (%s::text IS NULL OR lane = %s) ORDER BY event_id""",
            (lane, lane))
        return await cur.fetchall()


@pytestmark_db
async def test_check_lane_takes_guessed_boxes_that_have_a_saved_frame(rdb, review_assets, tmp_path):
    saved = await seed_frame(rdb, tmp_path, ts=T0, frame_id=10,
                             boxes=[{"bbox": [100, 100, 300, 250], "species": "Pomatomus saltarix"}])
    await seed_frame(rdb, tmp_path, ts=T0 + timedelta(minutes=10), frame_id=11, save=False,
                     boxes=[{"bbox": [100, 100, 300, 250], "species": "Pomatomus saltarix"}])
    stats = await _pool(rdb, review_assets).run_once()
    rows = await _items(rdb, "check")
    assert [r[0] for r in rows] == saved["event_ids"]
    # stored under the accepted name, so it counts toward the right species
    assert rows[0][3] == rows[0][4] == "Pomatomus saltatrix"
    assert stats["check_added"] == 1 and stats["errors"] == 0


@pytestmark_db
async def test_a_reused_frame_id_is_matched_by_timestamp(rdb, review_assets, tmp_path):
    # frame_id 500 was used twice across a worker restart. The detection
    # belongs to the earlier frame; the later one must not be picked.
    before = await seed_frame(rdb, tmp_path, ts=T0, frame_id=500,
                              boxes=[{"bbox": [100, 100, 300, 250], "species": "Seriola zonata"}])
    await seed_frame(rdb, tmp_path, ts=T0 + timedelta(hours=3), frame_id=500, boxes=[])
    await _pool(rdb, review_assets).run_once()
    rows = await _items(rdb, "check")
    assert [(r[0], r[2]) for r in rows] == [(before["event_ids"][0], before["saved_frame_id"])]


@pytestmark_db
async def test_small_boxes_close_neighbours_and_answered_boxes_are_skipped(rdb, review_assets,
                                                                           tmp_path):
    tiny = await seed_frame(rdb, tmp_path, ts=T0, frame_id=1,
                            boxes=[{"bbox": [10, 10, 30, 30], "species": "Seriola zonata"}])
    kept = await seed_frame(rdb, tmp_path, ts=T0 + timedelta(minutes=5), frame_id=2,
                            boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata"}])
    near = await seed_frame(rdb, tmp_path, ts=T0 + timedelta(minutes=5, seconds=30), frame_id=3,
                            boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata"}])
    answered = await seed_frame(rdb, tmp_path, ts=T0 + timedelta(minutes=30), frame_id=4,
                                boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata"}])
    uid = await make_user(rdb, "one")
    await add_verdict(rdb, answered["event_ids"][0], uid, name="Seriola zonata")
    await _pool(rdb, review_assets).run_once()
    got = {r[0] for r in await _items(rdb, "check")}
    # newest first: 'near' is taken, so 'kept' (30 s earlier) is too close to it
    assert near["event_ids"][0] in got
    assert not ({tiny["event_ids"][0], kept["event_ids"][0], answered["event_ids"][0]} & got)


@pytestmark_db
async def test_species_at_target_are_not_topped_up(rdb, review_assets, tmp_path):
    # the fixture training set already has 10 Blue Tang crops; target is 5
    await seed_frame(rdb, tmp_path, boxes=[{"bbox": [10, 10, 90, 90],
                                            "species": "Acanthurus coeruleus"}])
    await _pool(rdb, review_assets).run_once()
    assert await _items(rdb, "check") == []


@pytestmark_db
async def test_identify_lane_takes_the_largest_confident_unguessed_box(rdb, review_assets,
                                                                        tmp_path):
    frames = []
    for i in range(3):
        frames.append(await seed_frame(
            rdb, tmp_path, ts=T0 + timedelta(minutes=10 * i), frame_id=100 + i, boxes=[
                {"bbox": [0, 0, 60, 60]},                           # small but fine
                {"bbox": [100, 100, 400, 300]},                     # the largest
                {"bbox": [500, 100, 900, 500], "det_conf": 0.3},    # larger, not confident
                {"bbox": [0, 400, 300, 700], "species": "Seriola zonata"},  # guessed
            ]))
    pool = _pool(rdb, review_assets, identify_open=10)
    stats = await pool.run_once()
    rows = await _items(rdb, "identify")
    assert sorted(r[0] for r in rows) == sorted(f["event_ids"][1] for f in frames)
    assert all(r[3] is None and r[4] is None for r in rows)
    assert stats["identify_added"] == 3


@pytestmark_db
async def test_progress_counts_baseline_and_verified_boxes(rdb, review_assets, tmp_path):
    frame = await seed_frame(rdb, tmp_path, boxes=[
        {"bbox": [10, 10, 90, 90], "species": "Seriola zonata"},
        {"bbox": [100, 10, 190, 90], "species": "Seriola zonata"}])
    uid = await make_user(rdb, "one")
    await add_verdict(rdb, frame["event_ids"][0], uid, name="Seriola zonata")
    await add_verdict(rdb, frame["event_ids"][1], uid, name="Seriola zonata",
                      confidence="uncertain")
    pool = _pool(rdb, review_assets)
    await pool.refresh_progress()
    assert pool.baseline("Seriola zonata") == 3        # the 'review:' record is excluded
    assert pool.verified("Seriola zonata") == 1        # the 'not sure' one is not counted
    assert pool.baseline("Pomatomus saltatrix") == 1   # misspelled label, accepted name
    order = pool.species_order()
    assert "Acanthurus coeruleus" not in order         # 10 >= target 5
    assert order.index("Neopomacentrus cyanomos") < order.index("Seriola zonata")


@pytestmark_db
async def test_the_date_window_retires_items_outside_it(rdb, review_assets, tmp_path):
    early = await seed_frame(rdb, tmp_path, ts=T0, frame_id=1,
                             boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata"}])
    late = await seed_frame(rdb, tmp_path, ts=T0 + timedelta(days=2), frame_id=2,
                            boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata"}])
    pool = _pool(rdb, review_assets)
    await pool.run_once()
    async with rdb.connection() as conn:
        await conn.execute(
            "INSERT INTO review_settings (key, value) VALUES ('window', %s)",
            (Jsonb({"start": (T0 + timedelta(days=1)).isoformat(),
                    "end": (T0 + timedelta(days=3)).isoformat()}),))
    stats = await pool.run_once()
    status = {r[0]: r[5] for r in await _items(rdb, "check")}
    assert status[early["event_ids"][0]] == "retired" and stats["retired"] == 1
    assert status[late["event_ids"][0]] == "open"


@pytestmark_db
async def test_a_run_does_not_overlap_the_previous_one(rdb, review_assets):
    pool = _pool(rdb, review_assets)
    async with pool._lock:
        assert (await pool.run_once()) == {"skipped": "previous run still going"}


@pytestmark_db
async def test_the_statement_timeout_never_outlives_a_probe(rdb, review_assets, tmp_path):
    """Review Focus 1: a session-level timeout would stay on the pooled
    connection and kill the next borrower's long statement."""
    async def timeouts():
        async with rdb.connection() as a, rdb.connection() as b, rdb.connection() as c, \
                rdb.connection() as d:
            return [(await (await x.execute("SHOW statement_timeout")).fetchone())[0]
                    for x in (a, b, c, d)]

    before = await timeouts()
    await seed_frame(rdb, tmp_path, boxes=[{"bbox": [10, 10, 90, 90],
                                            "species": "Seriola zonata"}])
    await _pool(rdb, review_assets, identify_open=5).run_once()
    assert await timeouts() == before
```

- [ ] **Step 2: Run them to verify they fail**

Run: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn timeout 900 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest tests/test_review_pool.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.review_images'`.

- [ ] **Step 3: Write `review_images.py`**

```python
"""Frame and zoom images for the review page.

The zoom is the volunteer's work area (layout B): the model's box with
half a box of margin on each side, clamped to the frame, scaled up to 3x
and never past 960 px on its long edge. The page receives the region in
frame pixels so it can map drags back to frame coordinates; the zoom
endpoint recomputes the same region from the same inputs, so the two
always agree."""
from __future__ import annotations

import io
from functools import lru_cache

from PIL import Image

ZOOM_MARGIN = 0.5
ZOOM_MAX_SCALE = 3.0
ZOOM_MAX_EDGE = 960


@lru_cache(maxsize=4096)
def frame_size(path: str) -> tuple[int, int]:
    """(width, height), read from the file header. Raises FileNotFoundError."""
    with Image.open(path) as im:
        return im.size


def zoom_region(bbox, width: int, height: int, margin: float = ZOOM_MARGIN
                ) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = bbox
    mx = int(round(max(1, x2 - x1) * margin))
    my = int(round(max(1, y2 - y1) * margin))
    return (max(0, x1 - mx), max(0, y1 - my), min(width, x2 + mx), min(height, y2 + my))


def zoom_scale(region) -> float:
    longest = max(region[2] - region[0], region[3] - region[1], 1)
    return min(ZOOM_MAX_SCALE, ZOOM_MAX_EDGE / longest)


def _jpeg(im: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def render_zoom(path: str, region) -> bytes:
    scale = zoom_scale(region)
    with Image.open(path) as src:
        crop = src.convert("RGB").crop(tuple(region))
    w, h = crop.size
    crop = crop.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    return _jpeg(crop, 88)


def render_frame(path: str, max_edge: int) -> bytes:
    with Image.open(path) as src:
        im = src.convert("RGB")
    if max(im.size) > max_edge:
        im.thumbnail((max_edge, max_edge), Image.LANCZOS)
    return _jpeg(im, 85)
```

- [ ] **Step 4: Write `review_pool.py`**

```python
"""Keeps review_items topped up (spec §6).

A background task in the dashboard's lifespan. Each run:

1. refreshes the progress counts (training-set baseline + verified boxes);
2. retires open items outside the admin's date window;
3. tops up the `check` lane species by species, thinnest first;
4. tops up the `identify` lane with the largest unguessed box per frame.

Every probe is an index lookup that stops early. Each runs in its own
transaction with a 5 s timeout set by set_config(..., true): the limit
ends with the transaction and never reaches the next borrower of the
pooled connection (a session-level SET would, and the matview
refresher's long REFRESH would then die). For the same reason every
statement here runs inside `conn.transaction()`: a statement outside one
opens an implicit transaction, the next block becomes a savepoint, and
its local timeout would outlive it. A run is capped in probes and wall
time, and a run that finds the previous one still going returns at once.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from .review_labels import resolve_all

log = logging.getLogger("wahoobay.dashboard.review_pool")

CHECK_PROBE_SQL = """
    SELECT de.id, de.ts, de.source_name, de.bbox, de.best_accuracy, sf.id AS saved_frame_id
      FROM detection_events de
      JOIN saved_frames sf
        ON sf.frame_id = de.frame_id AND sf.source_name = de.source_name AND sf.ts = de.ts
     WHERE de.best_species_id = %s AND de.ts <= %s AND de.ts >= %s
     ORDER BY de.ts DESC
     LIMIT 50
"""
IDENTIFY_FRAMES_SQL = """
    SELECT id, ts, source_name, frame_id
      FROM saved_frames
     WHERE ts <= %s AND ts >= %s AND num_fish > 0
     ORDER BY ts DESC
     LIMIT 40
"""
IDENTIFY_BOXES_SQL = """
    SELECT f.saved_frame_id, de.id, de.ts, de.source_name, de.bbox
      FROM unnest(%s::bigint[], %s::text[], %s::timestamptz[], %s::bigint[])
           AS f(saved_frame_id, source_name, ts, frame_id)
      JOIN detection_events de
        ON de.source_name = f.source_name AND de.ts = f.ts AND de.frame_id = f.frame_id
     WHERE de.best_species_id IS NULL AND de.det_conf >= %s
"""
_FREE_EVENTS_SQL = """
    SELECT id FROM unnest(%s::bigint[]) AS c(id)
    EXCEPT SELECT event_id FROM detection_corrections WHERE event_id = ANY(%s)
    EXCEPT SELECT event_id FROM review_items WHERE event_id = ANY(%s)
"""
_TOO_CLOSE_SQL = """
    SELECT 1 FROM review_items
     WHERE source_name = %s AND lane = %s AND ts BETWEEN %s AND %s
       AND (%s::text IS NULL OR route_species = %s)
     LIMIT 1
"""
_INSERT_ITEM_SQL = """
    INSERT INTO review_items (event_id, pass, lane, saved_frame_id, source_name, ts, bbox,
                              predicted, predicted_p, route_species)
    VALUES (%s, 1, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (event_id, pass) DO NOTHING
    RETURNING id
"""


@dataclass
class PoolSettings:
    interval_s: float = 300.0
    max_probes: int = 200
    max_run_s: float = 30.0
    check_open_per_species: int = 20
    identify_open: int = 500
    spacing_s: int = 120
    min_box_px: int = 32
    identify_min_det_conf: float = 0.4
    target: int = 200
    statement_timeout_ms: int = 5000
    recent_exclusion_s: int = 600
    empty_species_skip_s: int = 86400

    @classmethod
    def from_cfg(cls, cfg) -> PoolSettings:
        return cls(interval_s=cfg.review_pool_interval_s,
                   max_probes=cfg.review_pool_max_probes,
                   max_run_s=cfg.review_pool_max_run_s,
                   check_open_per_species=cfg.review_check_open_per_species,
                   identify_open=cfg.review_identify_open,
                   spacing_s=cfg.review_spacing_s,
                   min_box_px=cfg.review_min_box_px,
                   identify_min_det_conf=cfg.review_identify_min_det_conf,
                   target=cfg.review_target)


def big_enough(bbox, min_px: int) -> bool:
    x1, y1, x2, y2 = bbox
    return min(x2 - x1, y2 - y1) >= min_px


def _area(bbox) -> int:
    return max(0, bbox[2] - bbox[0]) * max(0, bbox[3] - bbox[1])


async def read_window(conn) -> tuple[datetime | None, datetime | None]:
    """The admin's date window as stored, or (None, None) when unset."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT value FROM review_settings WHERE key = 'window'")
        row = await cur.fetchone()
    value = row[0] if row else None
    if not value:
        return None, None
    start = datetime.fromisoformat(value["start"]) if value.get("start") else None
    end = datetime.fromisoformat(value["end"]) if value.get("end") else None
    return start, end


async def retire_outside_window(conn, start: datetime, end: datetime) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            """UPDATE review_items SET status = 'retired', retired_reason = 'outside_window'
                WHERE status = 'open' AND pass = 1 AND (ts < %s OR ts > %s)""",
            (start, end))
        return cur.rowcount


class ReviewPool:
    def __init__(self, db, settings: PoolSettings, catalogue_store, baseline_path,
                 rng: random.Random | None = None) -> None:
        self.db = db
        self.s = settings
        self.catalogue_store = catalogue_store
        self.baseline_path = Path(baseline_path)
        self.rng = rng or random.Random()
        self._lock = asyncio.Lock()
        self._baseline: dict[str, int] = {}
        self._baseline_stamp: object = None
        self._verified: dict[str, int] = {}
        self._empty_until: dict[str, float] = {}
        self.last_run: dict | None = None

    # ---- progress ---------------------------------------------------------

    def _load_baseline(self) -> None:
        try:
            stamp = self.baseline_path.stat().st_mtime_ns
        except FileNotFoundError:
            if self._baseline_stamp != "missing":
                log.warning("review pool: %s not found; training-set counts taken as 0",
                            self.baseline_path)
            self._baseline, self._baseline_stamp = {}, "missing"
            return
        if stamp == self._baseline_stamp:
            return
        catalogue = self.catalogue_store.get()
        counts: dict[str, int] = {}
        for rec in json.loads(self.baseline_path.read_text()):
            # Crops merged back from a verified snapshot carry source_kind
            # 'review:...'. They are already counted as verified boxes.
            if str(rec.get("source_kind") or "").startswith("review:"):
                continue
            label = rec.get("label")
            if label:
                name = catalogue.accepted(label)
                counts[name] = counts.get(name, 0) + 1
        self._baseline, self._baseline_stamp = counts, stamp

    async def refresh_progress(self) -> None:
        self._load_baseline()
        async with self.db.connection() as conn, conn.transaction():
            resolved = await resolve_all(conn)
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT label, count(*) FROM review_added_boxes
                        WHERE removed_at IS NULL AND outcome = 'species'
                          AND confidence <> 'uncertain'
                        GROUP BY label""")
                added = await cur.fetchall()
        verified: dict[str, int] = {}
        for r in resolved.values():
            if r.is_verified and r.species and r.confidence != "uncertain":
                verified[r.species] = verified.get(r.species, 0) + 1
        for label, n in added:
            verified[label] = verified.get(label, 0) + n
        self._verified = verified

    def baseline(self, name: str) -> int:
        return self._baseline.get(name, 0)

    def verified(self, name: str) -> int:
        return self._verified.get(name, 0)

    def count(self, name: str | None) -> int:
        return self.baseline(name) + self.verified(name) if name else 0

    def note_verified(self, name: str | None) -> None:
        if name:
            self._verified[name] = self._verified.get(name, 0) + 1

    def species_order(self) -> list[str]:
        """Every catalogue species still below target, thinnest first."""
        below = [n for n in self.catalogue_store.get().names() if self.count(n) < self.s.target]
        return sorted(below, key=lambda n: (self.count(n), n))

    # ---- the run ------------------------------------------------------------

    async def run_forever(self) -> None:
        log.info("review pool: every %.0fs, target %d per species",
                 self.s.interval_s, self.s.target)
        while True:
            try:
                log.info("review pool: %s", await self.run_once())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("review pool: unexpected error; continuing")
            await asyncio.sleep(self.s.interval_s)

    async def run_once(self) -> dict:
        if self._lock.locked():
            return {"skipped": "previous run still going"}
        async with self._lock:
            t0 = time.monotonic()
            stats = {"check_added": 0, "identify_added": 0, "probes": 0, "retired": 0,
                     "timeouts": 0, "errors": 0}
            try:
                await self.refresh_progress()
                async with self.db.connection() as conn:
                    window = await self._window(conn)
                    if window is None:
                        stats["note"] = "no saved frames in the window yet"
                    else:
                        async with conn.transaction():
                            stats["retired"] = await retire_outside_window(conn, *window)
                        await self._fill_check(conn, window, stats, t0)
                        await self._fill_identify(conn, window, stats, t0)
            except asyncio.CancelledError:
                raise
            except Exception:
                stats["errors"] += 1
                log.exception("review pool: run failed")
            stats["duration_s"] = round(time.monotonic() - t0, 2)
            stats["finished_at"] = datetime.now(UTC).isoformat()
            self.last_run = stats
            return stats

    async def _window(self, conn) -> tuple[datetime, datetime] | None:
        async with conn.transaction():
            start, end = await read_window(conn)
            if start is None:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT min(ts) FROM saved_frames")
                    start = (await cur.fetchone())[0]
        latest = datetime.now(UTC) - timedelta(seconds=self.s.recent_exclusion_s)
        end = min(end, latest) if end else latest
        if start is None or start >= end:
            return None
        return start, end

    def _exhausted(self, stats: dict, t0: float) -> bool:
        return (stats["probes"] >= self.s.max_probes
                or time.monotonic() - t0 >= self.s.max_run_s)

    def _probe_time(self, window) -> datetime:
        start, end = window
        return start + (end - start) * self.rng.random()

    async def _probe(self, conn, sql: str, params, stats: dict) -> list[dict] | None:
        """One bounded read. None means it timed out."""
        stats["probes"] += 1
        try:
            async with conn.transaction():
                await conn.execute("SELECT set_config('statement_timeout', %s, true)",
                                   (str(self.s.statement_timeout_ms),))
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(sql, params)
                    return await cur.fetchall()
        except psycopg.errors.QueryCanceled:
            stats["timeouts"] += 1
            log.warning("review pool: a probe hit the %d ms statement timeout",
                        self.s.statement_timeout_ms)
            return None

    async def _rows(self, conn, sql: str, params) -> list:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                return await cur.fetchall()

    async def _free(self, conn, ids: list[int]) -> set[int]:
        if not ids:
            return set()
        return {r[0] for r in await self._rows(conn, _FREE_EVENTS_SQL, (ids, ids, ids))}

    async def _too_close(self, conn, *, source: str, lane: str, ts: datetime,
                         species: str | None) -> bool:
        gap = timedelta(seconds=self.s.spacing_s)
        return bool(await self._rows(conn, _TOO_CLOSE_SQL,
                                     (source, lane, ts - gap, ts + gap, species, species)))

    async def _insert(self, conn, *, event_id, lane, saved_frame_id, source, ts, bbox,
                      predicted, predicted_p, route) -> bool:
        return bool(await self._rows(conn, _INSERT_ITEM_SQL, (
            event_id, lane, saved_frame_id, source, ts, list(bbox), predicted, predicted_p,
            route)))

    async def _fill_check(self, conn, window, stats: dict, t0: float) -> None:
        catalogue = self.catalogue_store.get()
        open_counts = {name: n for name, n in await self._rows(conn, """
            SELECT route_species, count(*) FROM review_items
             WHERE lane = 'check' AND pass = 1 AND status IN ('open', 'claimed')
             GROUP BY route_species""", None)}
        # The model writes class names with spaces (classifier_v2.py): folder
        # 'Pomatomus_saltarix' appears in detection_events as 'Pomatomus
        # saltarix' and counts toward the accepted name.
        classes = sorted({(e.name, e.class_dir.replace("_", " "))
                          for e in catalogue.entries if e.class_dir},
                         key=lambda p: (self.count(p[0]), p[0]))
        now = time.time()
        for name, raw in classes:
            if self._exhausted(stats, t0):
                return
            if self.count(name) >= self.s.target or self._empty_until.get(raw, 0) > now:
                continue
            need = self.s.check_open_per_species - open_counts.get(name, 0)
            if need > 0:
                open_counts[name] = open_counts.get(name, 0) + await self._probe_check(
                    conn, window, name, raw, need, stats)

    async def _probe_check(self, conn, window, name: str, raw: str, need: int,
                           stats: dict) -> int:
        rows = await self._probe(conn, CHECK_PROBE_SQL,
                                 (raw, self._probe_time(window), window[0]), stats)
        if rows == []:
            # Nothing before the random point: look once more from the end
            # of the window before deciding the species has no candidates.
            rows = await self._probe(conn, CHECK_PROBE_SQL, (raw, window[1], window[0]), stats)
            if rows == []:
                self._empty_until[raw] = time.time() + self.s.empty_species_skip_s
        if not rows:
            return 0
        rows = [r for r in rows if big_enough(r["bbox"], self.s.min_box_px)]
        free = await self._free(conn, [r["id"] for r in rows])
        added = 0
        for r in rows:
            if added >= need:
                break
            if r["id"] not in free or await self._too_close(
                    conn, source=r["source_name"], lane="check", ts=r["ts"], species=name):
                continue
            if await self._insert(conn, event_id=r["id"], lane="check",
                                  saved_frame_id=r["saved_frame_id"], source=r["source_name"],
                                  ts=r["ts"], bbox=r["bbox"], predicted=name,
                                  predicted_p=r["best_accuracy"], route=name):
                added += 1
        stats["check_added"] += added
        return added

    async def _fill_identify(self, conn, window, stats: dict, t0: float) -> None:
        open_n = (await self._rows(conn, """
            SELECT count(*) FROM review_items
             WHERE lane = 'identify' AND pass = 1 AND status IN ('open', 'claimed')""",
                                   None))[0][0]
        while open_n < self.s.identify_open and not self._exhausted(stats, t0):
            frames = await self._probe(conn, IDENTIFY_FRAMES_SQL,
                                       (self._probe_time(window), window[0]), stats)
            if not frames:
                continue
            boxes = await self._probe(conn, IDENTIFY_BOXES_SQL, (
                [f["id"] for f in frames], [f["source_name"] for f in frames],
                [f["ts"] for f in frames], [f["frame_id"] for f in frames],
                self.s.identify_min_det_conf), stats)
            if not boxes:
                continue
            boxes = [b for b in boxes if big_enough(b["bbox"], self.s.min_box_px)]
            free = await self._free(conn, [b["id"] for b in boxes])
            best: dict[int, dict] = {}
            for b in boxes:
                current = best.get(b["saved_frame_id"])
                if b["id"] in free and (current is None
                                        or _area(b["bbox"]) > _area(current["bbox"])):
                    best[b["saved_frame_id"]] = b
            for saved_frame_id, b in best.items():
                if open_n >= self.s.identify_open:
                    break
                if await self._too_close(conn, source=b["source_name"], lane="identify",
                                         ts=b["ts"], species=None):
                    continue
                if await self._insert(conn, event_id=b["id"], lane="identify",
                                      saved_frame_id=saved_frame_id, source=b["source_name"],
                                      ts=b["ts"], bbox=b["bbox"], predicted=None,
                                      predicted_p=None, route=None):
                    open_n += 1
                    stats["identify_added"] += 1
```

- [ ] **Step 5: Wire it into `main.py`**

(a) `Path` is already imported in `main.py`. `self.public_base_url = ...` is the last line of `DashboardCfg.__init__`, and nothing else follows `__init__` in the class. Paste this block directly after that line, keeping its indentation: the lines indented 8 spaces continue `__init__`, and `def review_catalogue_paths` (indented 4) becomes a new method of the class.

```python
        # --- database pool ---
        # The review pool task holds a connection for up to 30 s per run, and
        # the matview refresher holds one through each REFRESH; 6 leaves the
        # public endpoints room.
        self.db_pool_max = int(_env("DASHBOARD_DB_POOL_MAX", "6"))

        # --- review tool (docs/superpowers/specs/2026-09-24-...) ---
        repo_root = Path(__file__).resolve().parents[3]
        self.review_pool_enabled = _env("REVIEW_POOL_ENABLED", "true").lower() != "false"
        self.review_pool_interval_s = float(_env("REVIEW_POOL_TOPUP_INTERVAL_S", "300"))
        self.review_pool_max_probes = int(_env("REVIEW_POOL_MAX_PROBES", "200"))
        self.review_pool_max_run_s = float(_env("REVIEW_POOL_MAX_RUN_S", "30"))
        self.review_check_open_per_species = int(_env("REVIEW_POOL_CHECK_OPEN_PER_SPECIES", "20"))
        self.review_identify_open = int(_env("REVIEW_POOL_IDENTIFY_OPEN", "500"))
        self.review_spacing_s = int(_env("REVIEW_POOL_SPACING_S", "120"))
        self.review_min_box_px = int(_env("REVIEW_MIN_BOX_PX", "32"))
        self.review_identify_min_det_conf = float(_env("REVIEW_IDENTIFY_MIN_DET_CONF", "0.4"))
        self.review_target = int(_env("REVIEW_TARGET_PER_SPECIES", "200"))
        self.review_claim_s = int(_env("REVIEW_CLAIM_S", "600"))
        self.review_spotcheck_rate = float(_env("REVIEW_SPOTCHECK_RATE", "0.10"))
        self.review_spotcheck_serve_rate = float(_env("REVIEW_SPOTCHECK_SERVE_RATE", "0.10"))
        self.review_assets_dir = Path(_env("REVIEW_ASSETS_DIR", str(repo_root / "assets")))
        self.review_taxonomy_csv = Path(_env(
            "REVIEW_TAXONOMY_CSV", str(self.review_assets_dir / "taxonomy.csv")))
        self.review_classes_json = Path(_env(
            "CLASSIFIER_V2_CLASSES_JSON",
            str(repo_root / "data" / "models" / "classifier_v2" / "classes.json")))
        self.review_baseline_manifest = Path(_env(
            "REVIEW_BASELINE_MANIFEST",
            str(repo_root / "data" / "fine_tune" / "_dataset_manifest.json")))

    def review_catalogue_paths(self):
        from .review_catalogue import CataloguePaths

        a = self.review_assets_dir
        return CataloguePaths(
            species_manifest=a / "species_manifest.json",
            classes_json=self.review_classes_json,
            taxonomy_csv=self.review_taxonomy_csv,
            inat_manifest=a / "inat_manifest.json",
            celebrities_json=a / "celebrities.json",
            photo_root=a.parent)
```

(b) In the lifespan, change `AsyncConnectionPool(cfg.database_url, min_size=1, max_size=4, open=False)` to `AsyncConnectionPool(cfg.database_url, min_size=1, max_size=cfg.db_pool_max, open=False)`.

(c) In the lifespan, directly after `state["prune_task"] = asyncio.create_task(_prune_sessions())` and before `try:`, add:

```python
        # Review tool: the species catalogue is read from disk on demand
        # (and rebuilt when the sheet poller rewrites it); the pool task
        # keeps review_items topped up.
        from .review_catalogue import CatalogueStore
        from .review_pool import PoolSettings, ReviewPool

        state["review_catalogue"] = CatalogueStore(cfg.review_catalogue_paths())
        review_task: asyncio.Task | None = None
        if state.get("pool") is not None:
            state["review_pool"] = ReviewPool(
                state["pool"], PoolSettings.from_cfg(cfg), state["review_catalogue"],
                cfg.review_baseline_manifest)
            if cfg.review_pool_enabled:
                review_task = asyncio.create_task(
                    state["review_pool"].run_forever(), name="review-pool")
            else:
                log.info("review pool: disabled (REVIEW_POOL_ENABLED=false)")
```

(d) In the `finally:` block, change the tuple to include the new task:
`for t in (slo_task, matview_task, sheet_task, state.get("prune_task"), review_task):`

- [ ] **Step 6: Write `scripts/dev/explain_review_probes.py`**

```python
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
        conn.execute("SET default_transaction_read_only = on")
        for title, sql, params in probes:
            plan = "\n".join(r[0] for r in conn.execute("EXPLAIN " + sql, params))
            print(f"== {title}\n{plan}\n")
            if "Seq Scan on detection_events" in plan or "Seq Scan on saved_frames" in plan:
                bad = True
    print("RESULT:", "SEQUENTIAL SCAN FOUND: do not enable the pool" if bad
          else "index scans only")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn timeout 900 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest tests/test_review_pool.py -q`
Expected: 12 passed.

Then the whole suite, to confirm the lifespan change broke nothing:

Run: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn timeout 900 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest tests -q`
Expected: all pass.

- [ ] **Step 8: Check the probe plans against the test database**

The test database is small, so its plans prove little about production, but this confirms the script runs. Write a throwaway `.env`-style file pointing at the test database:

```bash
/raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -c "
import os; dsn = open('.superpowers/test_dsn').read().strip()
fd = os.open('.superpowers/test.env', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
os.write(fd, ('DATABASE_URL=' + dsn + '\n').encode()); os.close(fd)"
timeout 120 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python scripts/dev/explain_review_probes.py --env .superpowers/test.env
```

Expected: three plans printed and a `RESULT:` line (on an empty test database the planner may choose sequential scans; that is fine here, and production is checked in Task 14).

- [ ] **Step 9: Lint and commit**

```bash
/raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/ruff check services/dashboard/app/review_images.py services/dashboard/app/review_pool.py services/dashboard/app/main.py scripts/dev/explain_review_probes.py tests/test_review_pool.py
git add services/dashboard/app/review_images.py services/dashboard/app/review_pool.py services/dashboard/app/main.py scripts/dev/explain_review_probes.py tests/test_review_pool.py
git commit -m "review: the pool task that keeps boxes ready, thinnest species first

A background task in the dashboard tops up a small review_items table
from index-only probes, binding each box to its frame by exact
timestamp and spacing boxes two minutes apart per camera, because
frame and track ids restart with the workers. Timeouts are scoped to
each probe's own transaction so they can never reach the matview
refresher's long REFRESH on a pooled connection; the test checks every
pooled connection after a run.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Serving boxes and recording verdicts

**Files:**
- Create: `services/dashboard/app/review_queue.py`
- Create: `services/dashboard/app/routes_review.py`
- Modify: `services/dashboard/app/main.py` (mount the router; delete the placeholder `/review` route)
- Test: `tests/test_review_queue.py`, `tests/test_review_routes.py`

**Interfaces:**
- Consumes: `effective_role`, `make_require_role`, `require_csrf`, `CurrentUser` (spec 1); `frame_size`, `zoom_region`, `render_zoom`, `render_frame` (Task 6); `slug` (Task 4); `state["review_catalogue"]`, `state["review_pool"]` (Task 6).
- Produces (`review_queue`): exceptions `ReviewError` (`.status`, `.message`), `Invalid` (400), `Forbidden` (403), `NotFound` (404), `FrameMissing(NotFound)`, `Conflict` (409); `LANES`; `VerdictIn`; `async fetch_one(conn, sql, params=None) -> dict | None`; `async fetch_all(conn, sql, params=None) -> list[dict]`; `async event_with_frame(conn, event_id) -> dict | None` (keys `event_id, ts, source_name, frame_id, bbox, best_species_id, best_accuracy, saved_frame_id, image_path`); `async next_item(conn, *, user_id, lane, species_order, spot_serve_rate, claim_s, rng) -> dict | None`; `async retire_item(conn, item_id, reason)`; `async build_payload(conn, item, *, catalogue, counts, target, starting_bbox=None) -> dict`; `async submit_verdict(conn, *, user, body, catalogue, spot_rate, rng, adjudication=False) -> dict` (keys `id, created_at, outcome, label, first_pass, counts, spot_check_scheduled`); `async skip_item(conn, *, user_id, item_id)`; `async release_item(conn, *, user_id, item_id)`; `async recent_verdicts(conn, *, user_id, session_id, catalogue, limit=20) -> dict`; `async reopen_payload(conn, *, user, event_id, catalogue, counts, target) -> dict`.
- Produces (`routes_review`): `build_review_router(cfg, state, templates) -> APIRouter`, ending in `return router`. Tasks 8 and 9 insert their routes immediately above that line and extend the import block at the top.
- Payload shape (read by the page in Tasks 10–12):
  `{"item": {"id", "pass", "lane", "event_id"}, "box": {"bbox", "model_bbox", "source_name", "ts", "saved_frame_id"}, "frame": {"url", "width", "height"}, "zoom": {"url", "region": [x0, y0, x1, y1]}, "other_boxes": [{"event_id", "bbox"}], "model": {"name", "p", "common"} | null, "hints": [{"name", "p", "common"}] | null, "counts_toward": {"name", "common", "count", "target"} | null}` plus `"mine": {"outcome", "label", "life_stage", "not_sure", "celebrity_id"}` on a reopened box.

- [ ] **Step 1: Write the failing module tests**

```python
# tests/test_review_queue.py
"""Serving boxes and recording verdicts (spec §8), without HTTP."""
from __future__ import annotations

import asyncio
import json
import random
from datetime import UTC, datetime, timedelta

import pytest
from review_fixtures import T0, add_item, add_verdict, as_user, catalogue_paths, make_user, seed_frame

from app.review_catalogue import CatalogueStore
from app.review_queue import (
    Conflict,
    Forbidden,
    Invalid,
    VerdictIn,
    build_payload,
    fetch_one,
    next_item,
    release_item,
    reopen_payload,
    skip_item,
    submit_verdict,
)

pytestmark = pytest.mark.anyio

HINTS = [{"name": "Seriola zonata", "p": 0.56}, {"name": "Pomatomus saltatrix", "p": 0.2},
         {"name": "Pomacanthus paru", "p": 0.1}]


@pytest.fixture
def cat(review_assets):
    return CatalogueStore(catalogue_paths(review_assets)).get()


async def _boxes(rdb, tmp_path, n=2, **item_kw):
    out = []
    for i in range(n):
        f = await seed_frame(rdb, tmp_path, ts=T0 + timedelta(minutes=10 * i), frame_id=10 + i,
                             boxes=[{"bbox": [100, 100, 300, 250], "species": "Seriola zonata"}])
        kw = {"predicted": "Seriola zonata", "predicted_p": 0.56, "route": "Seriola zonata",
              "hints": HINTS, **item_kw}
        out.append((f, await add_item(rdb, f, **kw)))
    return out


async def _next(rdb, uid, lane="check", order=(), rate=0.0, seed=1):
    async with rdb.connection() as conn:
        return await next_item(conn, user_id=uid, lane=lane, species_order=list(order),
                               spot_serve_rate=rate, claim_s=600, rng=random.Random(seed))


async def _verdict(rdb, cat, user, spot=0.0, **body):
    async with rdb.connection() as conn:
        return await submit_verdict(conn, user=user, body=VerdictIn(**body), catalogue=cat,
                                    spot_rate=spot, rng=random.Random(0))


async def _row(rdb, sql, *params):
    async with rdb.connection() as conn:
        return await (await conn.execute(sql, params)).fetchone()


async def _status(rdb, item_id):
    return (await _row(rdb, "SELECT status FROM review_items WHERE id = %s", item_id))[0]


# ---- serving --------------------------------------------------------------

async def test_two_volunteers_asking_at_once_never_get_the_same_box(rdb, tmp_path):
    await _boxes(rdb, tmp_path, n=2)
    one, two = await make_user(rdb, "one"), await make_user(rdb, "two")
    a, b = await asyncio.gather(_next(rdb, one), _next(rdb, two))
    assert a["id"] != b["id"] and a["claimed_by"] == one and b["claimed_by"] == two


async def test_asking_again_returns_the_box_you_already_hold(rdb, tmp_path):
    await _boxes(rdb, tmp_path, n=2)
    one = await make_user(rdb, "one")
    first = await _next(rdb, one)
    assert (await _next(rdb, one, seed=99))["id"] == first["id"]


async def test_a_lapsed_claim_goes_back_to_the_pool(rdb, tmp_path):
    [(_f, item)] = await _boxes(rdb, tmp_path, n=1)
    one, two = await make_user(rdb, "one"), await make_user(rdb, "two")
    async with rdb.connection() as conn:
        await conn.execute(
            """UPDATE review_items SET status = 'claimed', claimed_by = %s,
                      claimed_until = now() - interval '1 minute' WHERE id = %s""", (one, item))
    got = await _next(rdb, two)
    assert got["id"] == item and got["claimed_by"] == two


async def test_the_thinnest_species_comes_first(rdb, tmp_path):
    [(_fa, _a), (_fb, b)] = await _boxes(rdb, tmp_path, n=2)
    async with rdb.connection() as conn:
        await conn.execute(
            "UPDATE review_items SET route_species = 'Pomatomus saltatrix' WHERE id = %s", (b,))
    one = await make_user(rdb, "one")
    got = await _next(rdb, one, order=["Pomatomus saltatrix", "Seriola zonata"])
    assert got["id"] == b


async def test_a_skipped_box_is_not_offered_to_you_again(rdb, tmp_path):
    [(_f, item)] = await _boxes(rdb, tmp_path, n=1)
    one, two = await make_user(rdb, "one"), await make_user(rdb, "two")
    await _next(rdb, one)
    async with rdb.connection() as conn:
        await skip_item(conn, user_id=one, item_id=item)
    assert await _next(rdb, one) is None
    assert (await _next(rdb, two))["id"] == item


async def test_releasing_puts_the_box_back(rdb, tmp_path):
    [(_f, item)] = await _boxes(rdb, tmp_path, n=1)
    one = await make_user(rdb, "one")
    await _next(rdb, one)
    async with rdb.connection() as conn:
        await release_item(conn, user_id=one, item_id=item)
    assert await _status(rdb, item) == "open"


async def test_spot_checks_skip_the_first_reviewer_and_anyone_who_answered(rdb, tmp_path):
    [(f, _i)] = await _boxes(rdb, tmp_path, n=1, status="done")
    one = await make_user(rdb, "one")
    two = await make_user(rdb, "two")
    three = await make_user(rdb, "three")
    first = await add_verdict(rdb, f["event_ids"][0], one, name="Seriola zonata")
    await add_verdict(rdb, f["event_ids"][0], three, name="Seriola zonata")
    spot = await add_item(rdb, f, pass_=2, exclude_user_id=one, first_correction_id=first)
    assert await _next(rdb, one, rate=1.0) is None
    assert await _next(rdb, three, rate=1.0) is None
    assert (await _next(rdb, two, rate=1.0))["id"] == spot


async def test_a_spot_check_waiting_two_days_is_served_even_at_rate_zero(rdb, tmp_path):
    [(f, f_item), (_g, _g_item)] = await _boxes(rdb, tmp_path, n=2)
    one, two = await make_user(rdb, "one"), await make_user(rdb, "two")
    async with rdb.connection() as conn:
        await conn.execute("UPDATE review_items SET status = 'done' WHERE id = %s", (f_item,))
    first = await add_verdict(rdb, f["event_ids"][0], one, name="Seriola zonata")
    spot = await add_item(rdb, f, pass_=2, exclude_user_id=one, first_correction_id=first,
                          added_at=datetime.now(UTC) - timedelta(hours=49))
    assert (await _next(rdb, two, rate=0.0))["id"] == spot


async def test_a_spot_check_payload_says_nothing_about_the_model(rdb, tmp_path, cat):
    """Review Focus 2: not the guess, not the hints, not what it counts toward."""
    [(f, _i)] = await _boxes(rdb, tmp_path, n=1, status="done")
    one = await make_user(rdb, "one")
    first = await add_verdict(rdb, f["event_ids"][0], one, name="Seriola zonata")
    spot = await add_item(rdb, f, pass_=2, exclude_user_id=one, first_correction_id=first,
                          predicted="Seriola zonata", route="Seriola zonata", hints=HINTS)
    async with rdb.connection() as conn:
        item = await fetch_one(conn, "SELECT * FROM review_items WHERE id = %s", (spot,))
        payload = await build_payload(conn, item, catalogue=cat, counts=lambda n: 3, target=200)
    assert payload["model"] is None and payload["hints"] is None
    assert payload["counts_toward"] is None
    text = json.dumps(payload)
    for word in ("Seriola", "Pomatomus", "Pomacanthus", "Rudderfish"):
        assert word not in text


async def test_a_first_pass_payload_shows_the_guess_the_other_hints_and_progress(rdb, tmp_path,
                                                                                  cat):
    [(_f, item_id)] = await _boxes(rdb, tmp_path, n=1)
    async with rdb.connection() as conn:
        item = await fetch_one(conn, "SELECT * FROM review_items WHERE id = %s", (item_id,))
        payload = await build_payload(conn, item, catalogue=cat, counts=lambda n: 3, target=200)
    assert payload["model"]["name"] == "Seriola zonata"
    assert payload["model"]["common"] == "Banded Rudderfish"
    # the guess itself is not repeated among the hint buttons
    assert [h["name"] for h in payload["hints"]] == ["Pomatomus saltatrix", "Pomacanthus paru"]
    assert payload["counts_toward"] == {"name": "Seriola zonata", "common": "Banded Rudderfish",
                                        "count": 3, "target": 200}
    assert payload["zoom"]["region"] == [0, 25, 400, 325]
    assert payload["frame"]["width"] == 1280 and payload["frame"]["height"] == 720


# ---- verdicts -----------------------------------------------------------------

async def test_accepting_the_guess_records_an_endorsement_and_closes_the_item(rdb, tmp_path,
                                                                              cat):
    [(f, item)] = await _boxes(rdb, tmp_path, n=1)
    one = await make_user(rdb, "one")
    await _next(rdb, one)
    out = await _verdict(rdb, cat, as_user(one), event_id=f["event_ids"][0],
                         review_item_id=item, outcome="accept")
    assert out["label"] == "Seriola zonata" and out["first_pass"] and out["counts"]
    row = await _row(rdb, """SELECT corrected_name, is_endorsement, label_source, review_item_id,
                                    confidence, user_id
                               FROM detection_corrections WHERE id = %s""", out["id"])
    assert row == ("Seriola zonata", True, "model_guess", item, "certain", one)
    assert await _status(rdb, item) == "done"


async def test_a_species_from_the_search_with_its_life_stage(rdb, tmp_path, cat):
    [(f, item)] = await _boxes(rdb, tmp_path, n=1)
    one = await make_user(rdb, "one")
    out = await _verdict(rdb, cat, as_user(one), event_id=f["event_ids"][0], review_item_id=item,
                         outcome="species", label="Pomacanthus paru", life_stage="juvenile",
                         label_source="search", not_sure=True)
    row = await _row(rdb, """SELECT corrected_name, life_stage, is_endorsement, confidence
                               FROM detection_corrections WHERE id = %s""", out["id"])
    assert row == ("Pomacanthus paru", "juvenile", False, "uncertain")
    assert out["counts"] is False            # 'not sure' labels do not count toward progress


async def test_answer_validation(rdb, tmp_path, cat):
    [(f, item)] = await _boxes(rdb, tmp_path, n=1)
    user = as_user(await make_user(rdb, "one"))
    eid = f["event_ids"][0]
    bad = [
        dict(outcome="species", label="Not a species", label_source="search"),  # Review Focus 5
        dict(outcome="species", label="Pomacanthus paru", label_source="search"),  # stage needed
        dict(outcome="species", label="Neopomacentrus cyanomos", label_source="hint"),
        dict(outcome="species", label="Seriola zonata", label_source="model_guess"),
        dict(outcome="no_animal", label="Seriola zonata"),
        dict(outcome="unidentifiable", not_sure=True),
        dict(outcome="no_animal", bbox=[10, 10, 50, 50]),
        dict(outcome="accept", bbox=[-1, 0, 50, 50]),
        dict(outcome="accept", bbox=[10, 10, 13, 13]),
        dict(outcome="accept", bbox=[10, 10, 1281, 100]),
        dict(outcome="species", label="Seriola zonata", label_source="search",
             celebrity_id="bobby"),
    ]
    for extra in bad:
        with pytest.raises(Invalid):
            await _verdict(rdb, cat, user, event_id=eid, review_item_id=item, **extra)
    ok = await _verdict(rdb, cat, user, event_id=eid, review_item_id=item, outcome="accept",
                        bbox=[0, 0, 1280, 720])
    assert ok["label"] == "Seriola zonata"


async def test_a_second_opinion_cannot_use_the_guess_or_the_hints(rdb, tmp_path, cat):
    [(f, _i)] = await _boxes(rdb, tmp_path, n=1, status="done")
    one, two = await make_user(rdb, "one"), await make_user(rdb, "two")
    first = await add_verdict(rdb, f["event_ids"][0], one, name="Seriola zonata")
    spot = await add_item(rdb, f, pass_=2, exclude_user_id=one, first_correction_id=first,
                          predicted="Seriola zonata", hints=HINTS)
    for extra in (dict(outcome="accept"),
                  dict(outcome="species", label="Pomatomus saltatrix", label_source="hint")):
        with pytest.raises(Invalid):
            await _verdict(rdb, cat, as_user(two), event_id=f["event_ids"][0],
                           review_item_id=spot, **extra)
    out = await _verdict(rdb, cat, as_user(two), spot=1.0, event_id=f["event_ids"][0],
                         review_item_id=spot, outcome="species", label="Seriola zonata",
                         label_source="search")
    assert out["first_pass"] is False and out["spot_check_scheduled"] is False
    assert await _status(rdb, spot) == "done"


async def test_a_finished_or_held_box_is_a_conflict(rdb, tmp_path, cat):
    [(f, item)] = await _boxes(rdb, tmp_path, n=1)
    one, two = await make_user(rdb, "one"), await make_user(rdb, "two")
    await _next(rdb, one)
    with pytest.raises(Conflict):
        await _verdict(rdb, cat, as_user(two), event_id=f["event_ids"][0], review_item_id=item,
                       outcome="accept")
    await _verdict(rdb, cat, as_user(one), event_id=f["event_ids"][0], review_item_id=item,
                   outcome="accept")
    with pytest.raises(Conflict):
        await _verdict(rdb, cat, as_user(two), event_id=f["event_ids"][0], review_item_id=item,
                       outcome="accept")


async def test_without_an_item_you_can_only_answer_boxes_nobody_else_has(rdb, tmp_path, cat):
    f = await seed_frame(rdb, tmp_path, boxes=[{"bbox": [10, 10, 90, 90],
                                                "species": "Seriola zonata"}])
    one, two = await make_user(rdb, "one"), await make_user(rdb, "two")
    boss = await make_user(rdb, "boss", role="admin")
    eid = f["event_ids"][0]
    await _verdict(rdb, cat, as_user(one), event_id=eid, outcome="accept")
    await _verdict(rdb, cat, as_user(one), event_id=eid, outcome="no_animal")   # your own: fine
    with pytest.raises(Forbidden):
        await _verdict(rdb, cat, as_user(two), event_id=eid, outcome="accept")
    await _verdict(rdb, cat, as_user(boss, role="admin"), event_id=eid, outcome="accept")


async def test_a_first_pass_verdict_can_schedule_a_spot_check(rdb, tmp_path, cat):
    [(f, item)] = await _boxes(rdb, tmp_path, n=1)
    one = await make_user(rdb, "one")
    out = await _verdict(rdb, cat, as_user(one), spot=1.0, event_id=f["event_ids"][0],
                         review_item_id=item, outcome="accept")
    assert out["spot_check_scheduled"]
    row = await _row(rdb, """SELECT pass, status, exclude_user_id, first_correction_id
                               FROM review_items WHERE event_id = %s AND pass = 2""",
                     f["event_ids"][0])
    assert row == (2, "open", one, out["id"])


async def test_reopening_shows_your_last_answer_and_box(rdb, tmp_path, cat):
    [(f, item)] = await _boxes(rdb, tmp_path, n=1)
    one, two = await make_user(rdb, "one"), await make_user(rdb, "two")
    eid = f["event_ids"][0]
    await _verdict(rdb, cat, as_user(one), event_id=eid, review_item_id=item, outcome="accept",
                   bbox=[90, 90, 310, 260])
    async with rdb.connection() as conn:
        p = await reopen_payload(conn, user=as_user(one), event_id=eid, catalogue=cat,
                                 counts=lambda n: 0, target=200)
        assert p["item"]["id"] is None and p["box"]["bbox"] == [90, 90, 310, 260]
        assert p["mine"]["outcome"] == "species" and p["mine"]["label"] == "Seriola zonata"
        with pytest.raises(Forbidden):
            await reopen_payload(conn, user=as_user(two), event_id=eid, catalogue=cat,
                                 counts=lambda n: 0, target=200)
```

- [ ] **Step 2: Write the failing HTTP tests**

```python
# tests/test_review_routes.py
"""The review tool over HTTP: roles, CSRF, the page, images, and the
missing-frame fallback."""
from __future__ import annotations

import io
import os
from datetime import timedelta

import pytest
from PIL import Image
from review_fixtures import CSRF, T0, add_item, login, make_user, seed_frame

pytestmark = pytest.mark.anyio


async def _status(rdb, item_id):
    async with rdb.connection() as conn:
        cur = await conn.execute("SELECT status FROM review_items WHERE id = %s", (item_id,))
        return (await cur.fetchone())[0]


async def test_the_review_api_needs_a_reviewer(rdb, rclient):
    assert rclient.get("/api/review/next").status_code == 401
    await make_user(rdb, "watcher", role="viewer")
    login(rclient, "watcher")
    assert rclient.get("/api/review/next").status_code == 403
    await make_user(rdb, "rev")
    login(rclient, "rev")
    r = rclient.get("/api/review/next")
    assert r.status_code == 200 and r.json()["item"] is None


async def test_the_page_is_for_reviewers_and_redirects_everyone_else(rdb, rclient):
    r = rclient.get("/review", headers={"Accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("/login")
    await make_user(rdb, "rev")
    login(rclient, "rev")
    assert rclient.get("/review", headers={"Accept": "text/html"}).status_code == 200


async def test_a_verdict_needs_the_csrf_header_and_counts_in_the_session(rdb, rclient, tmp_path):
    await make_user(rdb, "rev")
    login(rclient, "rev")
    f = await seed_frame(rdb, tmp_path, boxes=[{"bbox": [100, 100, 300, 250],
                                                "species": "Seriola zonata"}])
    await add_item(rdb, f, predicted="Seriola zonata", predicted_p=0.56, route="Seriola zonata")
    nxt = rclient.get("/api/review/next?lane=check").json()
    body = {"event_id": nxt["item"]["event_id"], "review_item_id": nxt["item"]["id"],
            "outcome": "accept"}
    assert rclient.post("/api/review/verdicts", json=body).status_code == 403
    r = rclient.post("/api/review/verdicts", json=body, headers=CSRF)
    assert r.status_code == 200 and r.json()["label"] == "Seriola zonata"
    mine = rclient.get("/api/review/mine/recent").json()
    assert mine["session_count"] == 1 and mine["recent"][0]["label"] == "Seriola zonata"


async def test_an_unknown_species_is_a_clear_400(rdb, rclient, tmp_path):
    await make_user(rdb, "rev")
    login(rclient, "rev")
    f = await seed_frame(rdb, tmp_path, boxes=[{"bbox": [100, 100, 300, 250]}])
    r = rclient.post("/api/review/verdicts", headers=CSRF, json={
        "event_id": f["event_ids"][0], "outcome": "species", "label": "Gone fishus",
        "label_source": "search"})
    assert r.status_code == 400 and r.json()["error"] == "pick a species from the list"


async def test_a_box_whose_frame_file_is_gone_is_retired_and_the_next_served(rdb, rclient,
                                                                              tmp_path):
    """Review Focus 3."""
    await make_user(rdb, "rev")
    login(rclient, "rev")
    gone = await seed_frame(rdb, tmp_path, frame_id=1, boxes=[{"bbox": [10, 10, 90, 90],
                                                               "species": "Seriola zonata"}])
    ok = await seed_frame(rdb, tmp_path, ts=T0 + timedelta(minutes=10), frame_id=2,
                          boxes=[{"bbox": [10, 10, 90, 90]}])
    # a route species puts `gone` first in the queue; `ok` has none, so it sorts last
    gone_item = await add_item(rdb, gone, predicted="Seriola zonata", route="Seriola zonata")
    await add_item(rdb, ok, route=None)
    os.remove(gone["image_path"])
    r = rclient.get("/api/review/next?lane=check")
    assert r.status_code == 200 and r.json()["item"]["event_id"] == ok["event_ids"][0]
    assert await _status(rdb, gone_item) == "retired"


async def test_species_list_photos_and_images(rdb, rclient, tmp_path):
    await make_user(rdb, "rev")
    login(rclient, "rev")
    species = rclient.get("/api/review/species").json()["species"]
    assert any(s["name"] == "Pomatomus saltatrix" for s in species)
    photo = rclient.get("/api/review/species/Seriola_zonata/photo.jpg")
    assert photo.status_code == 200 and photo.headers["content-type"] == "image/jpeg"
    assert rclient.get("/api/review/species/Nope/photo.jpg").status_code == 404
    f = await seed_frame(rdb, tmp_path, boxes=[{"bbox": [100, 100, 200, 150]}])
    frame = rclient.get(f"/api/review/frames/{f['saved_frame_id']}/image.jpg?size=640")
    assert Image.open(io.BytesIO(frame.content)).size == (640, 360)
    zoom = rclient.get(f"/api/review/boxes/{f['event_ids'][0]}/zoom.jpg")
    assert Image.open(io.BytesIO(zoom.content)).size == (600, 300)
```

- [ ] **Step 3: Run them to verify they fail**

Run: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn timeout 900 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest tests/test_review_queue.py tests/test_review_routes.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.review_queue'` (collection error for the first file; the route tests fail with 404s once the module exists).

- [ ] **Step 4: Write `review_queue.py`**

```python
"""Serving boxes to volunteers and recording their verdicts (spec §8).

The rules live here, as functions taking a connection, so they can be
tested without HTTP; routes_review.py only turns the exceptions below
into status codes. Invariants:

* A box is claimed with one UPDATE ... FOR UPDATE SKIP LOCKED, so two
  volunteers asking at the same moment never get the same box.
* A spot-check (pass 2) is never served to the person who gave the
  first verdict or to anyone else who answered the box, and its payload
  carries no model guess, no hints, no species-it-counts-toward and no
  earlier verdict. The server leaves them out; the page hides nothing.
* Verdicts are appended, never edited. Reopening a box adds a new one.
"""
from __future__ import annotations

import random
from datetime import UTC, datetime
from typing import Literal

from psycopg.rows import dict_row
from pydantic import BaseModel, Field

from .auth import effective_role
from .review_images import frame_size, zoom_region

LANES = ("check", "identify")


class ReviewError(Exception):
    status = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class Invalid(ReviewError):
    status = 400


class Forbidden(ReviewError):
    status = 403


class NotFound(ReviewError):
    status = 404


class FrameMissing(NotFound):
    """The box's saved frame is missing from the database or from disk."""


class Conflict(ReviewError):
    status = 409


class VerdictIn(BaseModel):
    event_id: int
    review_item_id: int | None = None
    outcome: Literal["accept", "species", "unidentifiable", "no_animal"]
    label: str | None = Field(None, max_length=200)
    life_stage: str | None = Field(None, max_length=40)
    bbox: list[int] | None = None
    label_source: Literal["model_guess", "hint", "search"] | None = None
    not_sure: bool = False
    celebrity_id: str | None = Field(None, max_length=80)
    notes: str | None = Field(None, max_length=2000)


async def fetch_one(conn, sql: str, params=None) -> dict | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, params)
        return await cur.fetchone()


async def fetch_all(conn, sql: str, params=None) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, params)
        return await cur.fetchall()


_EVENT_FRAME_SQL = """
    SELECT de.id AS event_id, de.ts, de.source_name, de.frame_id, de.bbox,
           de.best_species_id, de.best_accuracy, sf.id AS saved_frame_id, sf.image_path
      FROM detection_events de
      LEFT JOIN LATERAL (
          SELECT id, image_path FROM saved_frames
           WHERE frame_id = de.frame_id AND source_name = de.source_name AND ts = de.ts
           ORDER BY id LIMIT 1
      ) sf ON true
     WHERE de.id = %s
"""


async def event_with_frame(conn, event_id: int) -> dict | None:
    """The detection and the frame saved at its exact timestamp, if any."""
    return await fetch_one(conn, _EVENT_FRAME_SQL, (event_id,))


# ---- serving ------------------------------------------------------------

_CLAIM = ("status = 'claimed', claimed_by = %(uid)s, "
          "claimed_until = now() + make_interval(secs => %(claim_s)s)")
_PASS1_SQL = f"""
    UPDATE review_items SET {_CLAIM}
     WHERE id = (
        SELECT ri.id FROM review_items ri
         WHERE ri.status = 'open' AND ri.pass = 1 AND ri.lane = %(lane)s
           AND NOT EXISTS (SELECT 1 FROM review_skips s
                            WHERE s.item_id = ri.id AND s.user_id = %(uid)s)
         ORDER BY COALESCE(array_position(%(order)s::text[], ri.route_species), 1000000),
                  random()
         LIMIT 1
         FOR UPDATE SKIP LOCKED)
    RETURNING *
"""
_SPOT_ELIGIBLE = """
    ri.status = 'open' AND ri.pass = 2
    AND ri.exclude_user_id IS DISTINCT FROM %(uid)s
    AND NOT EXISTS (SELECT 1 FROM detection_corrections c
                     WHERE c.event_id = ri.event_id AND c.user_id = %(uid)s)
    AND NOT EXISTS (SELECT 1 FROM review_skips s
                     WHERE s.item_id = ri.id AND s.user_id = %(uid)s)
"""
_SPOT_SQL = f"""
    UPDATE review_items SET {_CLAIM}
     WHERE id = (
        SELECT ri.id FROM review_items ri
         WHERE {_SPOT_ELIGIBLE}
         ORDER BY ri.added_at
         LIMIT 1
         FOR UPDATE SKIP LOCKED)
    RETURNING *
"""
_STALE_SPOT_SQL = f"""
    SELECT 1 FROM review_items ri
     WHERE {_SPOT_ELIGIBLE} AND ri.added_at < now() - interval '48 hours'
     LIMIT 1
"""


async def next_item(conn, *, user_id: int, lane: str, species_order: list[str],
                    spot_serve_rate: float, claim_s: int, rng: random.Random) -> dict | None:
    if lane not in LANES:
        raise Invalid("unknown lane")
    params = {"uid": user_id, "lane": lane, "order": list(species_order), "claim_s": claim_s}
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                """UPDATE review_items SET status = 'open', claimed_by = NULL,
                          claimed_until = NULL
                    WHERE status = 'claimed' AND claimed_until < now()""")
        held = await fetch_one(conn, """
            SELECT * FROM review_items
             WHERE status = 'claimed' AND claimed_by = %(uid)s
               AND (lane = %(lane)s OR pass = 2)
             ORDER BY id LIMIT 1""", params)
        if held:
            return held
        want_spot = (rng.random() < spot_serve_rate
                     or await fetch_one(conn, _STALE_SPOT_SQL, params) is not None)
        if want_spot:
            item = await fetch_one(conn, _SPOT_SQL, params)
            if item:
                return item
        item = await fetch_one(conn, _PASS1_SQL, params)
        if item is None and not want_spot:
            # The lane is empty: a waiting spot-check is still useful work.
            item = await fetch_one(conn, _SPOT_SQL, params)
        return item


async def retire_item(conn, item_id: int, reason: str) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """UPDATE review_items SET status = 'retired', retired_reason = %s,
                      claimed_by = NULL, claimed_until = NULL
                WHERE id = %s""", (reason, item_id))


async def build_payload(conn, item: dict, *, catalogue, counts, target: int,
                        starting_bbox=None) -> dict:
    ev = await event_with_frame(conn, item["event_id"])
    if ev is None or ev["saved_frame_id"] is None:
        raise FrameMissing("this box's frame was not saved")
    try:
        width, height = frame_size(ev["image_path"])
    except FileNotFoundError as e:
        raise FrameMissing("this box's frame is missing on disk") from e
    others = await fetch_all(conn, """
        SELECT id AS event_id, bbox FROM detection_events
         WHERE source_name = %s AND ts = %s AND frame_id = %s AND id <> %s
         ORDER BY id""", (ev["source_name"], ev["ts"], ev["frame_id"], ev["event_id"]))
    payload = {
        "item": {"id": item.get("id"), "pass": item.get("pass", 1), "lane": item["lane"],
                 "event_id": ev["event_id"]},
        "box": {"bbox": list(starting_bbox or ev["bbox"]), "model_bbox": list(ev["bbox"]),
                "source_name": ev["source_name"], "ts": ev["ts"].isoformat(),
                "saved_frame_id": ev["saved_frame_id"]},
        "frame": {"url": f"/api/review/frames/{ev['saved_frame_id']}/image.jpg",
                  "width": width, "height": height},
        "zoom": {"url": f"/api/review/boxes/{ev['event_id']}/zoom.jpg",
                 "region": list(zoom_region(ev["bbox"], width, height))},
        "other_boxes": [{"event_id": o["event_id"], "bbox": list(o["bbox"])} for o in others],
        "model": None, "hints": None, "counts_toward": None,
    }
    if item.get("pass") == 2:
        return payload        # a second opinion comes from the picture alone
    guess = item.get("predicted")
    if guess:
        payload["model"] = {"name": guess, "p": item.get("predicted_p"),
                            "common": catalogue.common_name(guess)}
    hints = [h for h in (item.get("hints") or []) if h.get("name") and h["name"] != guess]
    if hints:
        payload["hints"] = [{"name": h["name"], "p": h.get("p"),
                             "common": catalogue.common_name(h["name"])} for h in hints[:3]]
    route = item.get("route_species")
    if route:
        payload["counts_toward"] = {"name": route, "common": catalogue.common_name(route),
                                    "count": counts(route), "target": target}
    return payload


# ---- verdicts -----------------------------------------------------------

def _validate(body: VerdictIn, *, catalogue, guess, item, blind: bool, width: int,
              height: int, adjudication: bool) -> dict:
    bbox = None
    if body.bbox is not None:
        if body.outcome == "no_animal":
            raise Invalid("a box can't be adjusted on a 'no animal' answer")
        if len(body.bbox) != 4:
            raise Invalid("a box is [x1, y1, x2, y2]")
        x1, y1, x2, y2 = (int(v) for v in body.bbox)
        if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
            raise Invalid("the box must lie inside the frame")
        if x2 - x1 < 4 or y2 - y1 < 4:
            raise Invalid("the box is too small")
        bbox = [x1, y1, x2, y2]
    name = life_stage = label_source = None
    if body.outcome == "accept":
        if blind:
            raise Invalid("a second opinion has no model guess to accept")
        if not guess:
            raise Invalid("this box has no model guess to accept")
        name, label_source = guess, "model_guess"
        if body.life_stage and catalogue.entry(guess, body.life_stage):
            life_stage = body.life_stage
    elif body.outcome == "species":
        if not body.label or not catalogue.entry(body.label, body.life_stage):
            raise Invalid("pick a species from the list")
        name, life_stage = body.label, body.life_stage
        if not adjudication:
            label_source = body.label_source
            if label_source not in ("hint", "search"):
                raise Invalid("say whether the species came from a hint or the search")
            hint_names = {h.get("name") for h in ((item or {}).get("hints") or [])}
            if label_source == "hint" and (blind or name not in hint_names):
                raise Invalid("that species was not one of the hints")
    elif body.label or body.label_source:
        raise Invalid("no species goes with this answer")
    if body.not_sure and name is None:
        raise Invalid("'not sure' only applies to a species")
    celebrity_id = (body.celebrity_id or "").strip() or None
    if celebrity_id and celebrity_id not in catalogue.celebrity_ids(name):
        raise Invalid("that named individual is not recorded for this species")
    return {"name": name, "life_stage": life_stage, "label_source": label_source, "bbox": bbox,
            "confidence": "uncertain" if body.not_sure else "certain",
            "celebrity_id": celebrity_id, "notes": (body.notes or "").strip() or None}


async def submit_verdict(conn, *, user, body: VerdictIn, catalogue, spot_rate: float,
                         rng: random.Random, adjudication: bool = False) -> dict:
    role = effective_role(user.status, user.role)
    async with conn.transaction():
        ev = await event_with_frame(conn, body.event_id)
        if ev is None:
            raise NotFound("no such detection")
        if ev["saved_frame_id"] is None:
            raise Invalid("this detection's frame was not saved")
        try:
            width, height = frame_size(ev["image_path"])
        except FileNotFoundError as e:
            raise FrameMissing("this box's frame is missing on disk") from e

        item = None
        if body.review_item_id is not None and not adjudication:
            item = await fetch_one(conn, "SELECT * FROM review_items WHERE id = %s FOR UPDATE",
                                   (body.review_item_id,))
            if item is None or item["event_id"] != body.event_id:
                raise NotFound("no such review item")
            if item["status"] in ("done", "retired"):
                raise Conflict("this box was already answered")
            if (item["status"] == "claimed" and item["claimed_by"] != user.id
                    and item["claimed_until"] > datetime.now(UTC)):
                raise Conflict("another volunteer is answering this box")
        elif not adjudication:
            # No queue item: whole-frame mode, or changing your own answer.
            if role != "admin" and await fetch_one(conn, """
                    SELECT 1 FROM detection_corrections
                     WHERE event_id = %s AND user_id IS NOT NULL AND user_id <> %s
                     LIMIT 1""", (body.event_id, user.id)):
                raise Forbidden("another volunteer already answered this box")
            if await fetch_one(conn, """
                    SELECT 1 FROM review_items
                     WHERE event_id = %s AND status = 'claimed' AND claimed_by <> %s
                       AND claimed_until > now()
                     LIMIT 1""", (body.event_id, user.id)):
                raise Conflict("another volunteer is answering this box")

        blind = item is not None and item["pass"] == 2
        guess = (item or {}).get("predicted") or catalogue.accepted(ev["best_species_id"])
        v = _validate(body, catalogue=catalogue, guess=guess, item=item, blind=blind,
                      width=width, height=height, adjudication=adjudication)
        row = await fetch_one(conn, """
            INSERT INTO detection_corrections
                (event_id, corrected_name, corrected_species_id, not_a_fish, unidentifiable,
                 is_endorsement, corrected_bbox, confidence, reviewer, notes, celebrity_id,
                 user_id, session_id, review_item_id, label_source, life_stage,
                 is_adjudication)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, created_at""",
            (body.event_id, v["name"], v["name"], body.outcome == "no_animal",
             body.outcome == "unidentifiable", v["name"] is not None and v["name"] == guess,
             v["bbox"], v["confidence"], user.display_name, v["notes"], v["celebrity_id"],
             user.id, user.session_id, item["id"] if item else None, v["label_source"],
             v["life_stage"], adjudication))

        if item is not None and item["pass"] == 2:
            where, params = "id = %s", (item["id"],)
        else:
            where, params = "event_id = %s AND pass = 1", (body.event_id,)
        async with conn.cursor() as cur:
            await cur.execute(
                f"""UPDATE review_items SET status = 'done', done_at = now(),
                           claimed_by = NULL, claimed_until = NULL
                     WHERE {where} AND status IN ('open', 'claimed')""", params)

        first_pass = not adjudication and (item is None or item["pass"] == 1)
        spot = None
        if first_pass and rng.random() < spot_rate:
            lane = (item or {}).get("lane") or ("check" if ev["best_species_id"] else "identify")
            spot = await fetch_one(conn, """
                INSERT INTO review_items (event_id, pass, lane, saved_frame_id, source_name, ts,
                                          bbox, predicted, predicted_p, exclude_user_id,
                                          first_correction_id)
                VALUES (%s, 2, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (event_id, pass) DO NOTHING
                RETURNING id""",
                (body.event_id, lane, ev["saved_frame_id"], ev["source_name"], ev["ts"],
                 list(ev["bbox"]), guess, ev["best_accuracy"], user.id, row["id"]))
    return {"id": row["id"], "created_at": row["created_at"].isoformat(),
            "outcome": body.outcome, "label": v["name"], "first_pass": first_pass,
            "counts": v["name"] is not None and v["confidence"] != "uncertain",
            "spot_check_scheduled": spot is not None}


async def skip_item(conn, *, user_id: int, item_id: int) -> None:
    async with conn.transaction():
        item = await fetch_one(conn, """SELECT id, status, claimed_by FROM review_items
                                         WHERE id = %s FOR UPDATE""", (item_id,))
        if item is None:
            raise NotFound("no such review item")
        async with conn.cursor() as cur:
            await cur.execute("""INSERT INTO review_skips (item_id, user_id) VALUES (%s, %s)
                                 ON CONFLICT DO NOTHING""", (item_id, user_id))
            if item["status"] == "claimed" and item["claimed_by"] == user_id:
                await cur.execute(
                    """UPDATE review_items SET status = 'open', claimed_by = NULL,
                              claimed_until = NULL WHERE id = %s""", (item_id,))


async def release_item(conn, *, user_id: int, item_id: int) -> None:
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            """UPDATE review_items SET status = 'open', claimed_by = NULL, claimed_until = NULL
                WHERE id = %s AND status = 'claimed' AND claimed_by = %s""", (item_id, user_id))


def _outcome_word(row: dict) -> str:
    if row["not_a_fish"]:
        return "no_animal"
    return "unidentifiable" if row["unidentifiable"] else "species"


async def recent_verdicts(conn, *, user_id: int, session_id: int | None, catalogue,
                          limit: int = 20) -> dict:
    count = 0
    if session_id is not None:
        count = (await fetch_one(conn, """SELECT count(*) AS n FROM detection_corrections
                                           WHERE session_id = %s""", (session_id,)))["n"]
    rows = await fetch_all(conn, """
        SELECT id, event_id, corrected_name, not_a_fish, unidentifiable, life_stage, created_at
          FROM detection_corrections
         WHERE user_id = %s AND session_id IS NOT DISTINCT FROM %s AND NOT is_adjudication
         ORDER BY created_at DESC, id DESC
         LIMIT %s""", (user_id, session_id, limit))
    return {"session_count": count, "recent": [
        {"id": r["id"], "event_id": r["event_id"], "outcome": _outcome_word(r),
         "label": r["corrected_name"], "common": catalogue.common_name(r["corrected_name"]),
         "life_stage": r["life_stage"], "at": r["created_at"].isoformat(),
         "zoom_url": f"/api/review/boxes/{r['event_id']}/zoom.jpg"} for r in rows]}


async def reopen_payload(conn, *, user, event_id: int, catalogue, counts, target: int) -> dict:
    mine = await fetch_one(conn, """
        SELECT corrected_name, not_a_fish, unidentifiable, life_stage, corrected_bbox,
               confidence, celebrity_id
          FROM detection_corrections
         WHERE event_id = %s AND user_id = %s
         ORDER BY created_at DESC, id DESC LIMIT 1""", (event_id, user.id))
    if mine is None and effective_role(user.status, user.role) != "admin":
        raise Forbidden("you can only reopen boxes you answered")
    ev = await event_with_frame(conn, event_id)
    if ev is None:
        raise NotFound("no such detection")
    item = await fetch_one(conn, "SELECT * FROM review_items WHERE event_id = %s AND pass = 1",
                           (event_id,))
    guess = catalogue.accepted(ev["best_species_id"])
    base = item or {"lane": "check" if guess else "identify", "predicted": guess,
                    "predicted_p": ev["best_accuracy"], "hints": None, "route_species": guess}
    pseudo = {**base, "id": None, "pass": 1, "event_id": event_id}
    payload = await build_payload(conn, pseudo, catalogue=catalogue, counts=counts,
                                  target=target,
                                  starting_bbox=(mine or {}).get("corrected_bbox"))
    if mine:
        payload["mine"] = {"outcome": _outcome_word(mine), "label": mine["corrected_name"],
                           "life_stage": mine["life_stage"],
                           "not_sure": mine["confidence"] == "uncertain",
                           "celebrity_id": mine["celebrity_id"]}
    return payload
```

- [ ] **Step 5: Write `routes_review.py`**

```python
"""HTTP surface of the review tool (spec §10).

Thin handlers. The rules live in review_queue (and, from Tasks 8 and 9,
review_frames and review_browse), which raise ReviewError subclasses;
this module turns them into status codes. Every change carries
require_role and require_csrf, as everywhere since spec 1."""
from __future__ import annotations

import asyncio
import random

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .auth import effective_role, make_require_role, require_csrf
from .review_images import frame_size, render_frame, render_zoom, zoom_region
from .review_queue import (
    FrameMissing,
    ReviewError,
    VerdictIn,
    build_payload,
    event_with_frame,
    fetch_one,
    next_item,
    recent_verdicts,
    release_item,
    reopen_payload,
    retire_item,
    skip_item,
    submit_verdict,
)

IMAGE_HEADERS = {"Cache-Control": "private, max-age=86400, immutable"}


def build_review_router(cfg, state, templates) -> APIRouter:
    router = APIRouter()
    require_role = make_require_role(cfg, state)
    reviewer = require_role("reviewer", "admin")
    admin_only = require_role("admin")
    rng = random.Random()

    def _db():
        pool = state.get("pool")
        if pool is None:
            raise HTTPException(status_code=503, detail="database unavailable")
        return pool

    def _catalogue():
        return state["review_catalogue"].get()

    def _pool():
        return state.get("review_pool")

    def _counts(name):
        pool_obj = _pool()
        return pool_obj.count(name) if pool_obj else 0

    def _fail(e: ReviewError) -> JSONResponse:
        return JSONResponse({"error": e.message}, status_code=e.status)

    async def _jpeg(fn, *args) -> Response:
        try:
            data = await asyncio.to_thread(fn, *args)
        except FileNotFoundError:
            return JSONResponse({"error": "image file missing on disk"}, status_code=404)
        return Response(content=data, media_type="image/jpeg", headers=IMAGE_HEADERS)

    @router.get("/review", response_class=HTMLResponse)
    async def review_page(request: Request, user=Depends(reviewer)):
        version = state.get("asset_version")
        return templates.TemplateResponse(request, "review.html", {
            "display_name": user.display_name,
            "role": effective_role(user.status, user.role),
            "asset_version": version() if version else "",
        })

    @router.get("/api/review/next")
    async def next_box(lane: str = Query("check"), user=Depends(reviewer)):
        pool_obj = _pool()
        order = pool_obj.species_order() if pool_obj else []
        empty = {"item": None, "pool": pool_obj.last_run if pool_obj else None}
        async with _db().connection() as conn:
            try:
                for _attempt in range(5):
                    item = await next_item(
                        conn, user_id=user.id, lane=lane, species_order=order,
                        spot_serve_rate=cfg.review_spotcheck_serve_rate,
                        claim_s=cfg.review_claim_s, rng=rng)
                    if item is None:
                        return JSONResponse(empty)
                    try:
                        return JSONResponse(await build_payload(
                            conn, item, catalogue=_catalogue(), counts=_counts,
                            target=cfg.review_target))
                    except FrameMissing:
                        # Review Focus 3: never hand out a box nobody can see.
                        await retire_item(conn, item["id"], "frame_missing")
                return JSONResponse(empty)
            except ReviewError as e:
                return _fail(e)

    @router.post("/api/review/verdicts", dependencies=[Depends(require_csrf)])
    async def post_verdict(body: VerdictIn, user=Depends(reviewer)):
        async with _db().connection() as conn:
            try:
                out = await submit_verdict(conn, user=user, body=body, catalogue=_catalogue(),
                                           spot_rate=cfg.review_spotcheck_rate, rng=rng)
            except ReviewError as e:
                return _fail(e)
        pool_obj = _pool()
        if pool_obj and out["first_pass"] and out["counts"]:
            pool_obj.note_verified(out["label"])
        return JSONResponse(out)

    @router.post("/api/review/items/{item_id}/skip", dependencies=[Depends(require_csrf)])
    async def skip(item_id: int, user=Depends(reviewer)):
        async with _db().connection() as conn:
            try:
                await skip_item(conn, user_id=user.id, item_id=item_id)
            except ReviewError as e:
                return _fail(e)
        return JSONResponse({"ok": True})

    @router.post("/api/review/items/{item_id}/release", dependencies=[Depends(require_csrf)])
    async def release(item_id: int, user=Depends(reviewer)):
        async with _db().connection() as conn:
            await release_item(conn, user_id=user.id, item_id=item_id)
        return JSONResponse({"ok": True})

    @router.get("/api/review/boxes/{event_id}")
    async def reopen(event_id: int, user=Depends(reviewer)):
        async with _db().connection() as conn:
            try:
                return JSONResponse(await reopen_payload(
                    conn, user=user, event_id=event_id, catalogue=_catalogue(),
                    counts=_counts, target=cfg.review_target))
            except ReviewError as e:
                return _fail(e)

    @router.get("/api/review/mine/recent")
    async def mine(user=Depends(reviewer)):
        async with _db().connection() as conn:
            return JSONResponse(await recent_verdicts(
                conn, user_id=user.id, session_id=user.session_id, catalogue=_catalogue()))

    @router.get("/api/review/species")
    async def species(user=Depends(reviewer)):
        return JSONResponse({"species": _catalogue().to_json()})

    @router.get("/api/review/species/{name_slug}/photo.jpg")
    async def species_photo(name_slug: str, user=Depends(reviewer)):
        found = _catalogue().photo(name_slug)
        if found is None:
            return JSONResponse({"error": "no photo for that species"}, status_code=404)
        return await _jpeg(render_frame, str(found[0]), 320)

    @router.get("/api/review/frames/{saved_frame_id}/image.jpg")
    async def frame_image(saved_frame_id: int, size: int = Query(1280, ge=160, le=1920),
                          user=Depends(reviewer)):
        async with _db().connection() as conn:
            row = await fetch_one(conn, "SELECT image_path FROM saved_frames WHERE id = %s",
                                  (saved_frame_id,))
        if row is None:
            return JSONResponse({"error": "no such frame"}, status_code=404)
        return await _jpeg(render_frame, row["image_path"], size)

    @router.get("/api/review/boxes/{event_id}/zoom.jpg")
    async def box_zoom(event_id: int, user=Depends(reviewer)):
        async with _db().connection() as conn:
            ev = await event_with_frame(conn, event_id)
        if ev is None or ev["image_path"] is None:
            return JSONResponse({"error": "no frame for this box"}, status_code=404)
        try:
            width, height = await asyncio.to_thread(frame_size, ev["image_path"])
        except FileNotFoundError:
            return JSONResponse({"error": "image file missing on disk"}, status_code=404)
        return await _jpeg(render_zoom, ev["image_path"],
                           zoom_region(ev["bbox"], width, height))

    _ = admin_only    # used by the admin routes added in Task 9

    return router
```

- [ ] **Step 6: Mount it in `main.py`**

After `app.include_router(build_admin_users_router(cfg, state, TEMPLATES))`, add:

```python
    from .routes_review import build_review_router
    app.include_router(build_review_router(cfg, state, TEMPLATES))
```

Delete the placeholder route (the `@app.get("/review", response_class=HTMLResponse)` block and its three-line comment). The placeholder `templates/review.html` stays until Task 10 replaces it; it only reads `display_name`.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn timeout 900 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest tests/test_review_queue.py tests/test_review_routes.py tests/test_auth_db.py -q`
Expected: all pass. `test_auth_db.py` still contains a `/review` test from spec 1; it passes because the new route keeps the same path, role and redirect behaviour.

- [ ] **Step 8: Lint and commit**

```bash
/raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/ruff check services/dashboard/app/review_queue.py services/dashboard/app/routes_review.py services/dashboard/app/main.py tests/test_review_queue.py tests/test_review_routes.py
git add services/dashboard/app/review_queue.py services/dashboard/app/routes_review.py services/dashboard/app/main.py tests/test_review_queue.py tests/test_review_routes.py
git commit -m "review: serve boxes, record verdicts, schedule blind spot-checks

Boxes are claimed with SKIP LOCKED so two volunteers never share one,
thinnest species first. Verdicts are validated against the catalogue
and the frame, appended rather than edited, and one in ten first-pass
verdicts schedules a second opinion that is never shown to anyone who
answered the box and carries nothing from the model. A box whose frame
has gone missing is retired and the next one served.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Whole-frame mode

**Files:**
- Create: `services/dashboard/app/review_frames.py`
- Modify: `services/dashboard/app/routes_review.py` (imports; routes inserted above `_ = admin_only`)
- Test: `tests/test_review_frames.py`

**Interfaces:**
- Consumes: `fetch_one`, `fetch_all`, the exceptions, `submit_verdict` (Task 7); `resolve_all` (Task 5); `frame_size`, `zoom_region`, `render_zoom` (Task 6).
- Produces: `AddedBoxIn`; `async frame_record(conn, saved_frame_id) -> dict`; `async open_frame(conn, *, saved_frame_id, user_id, claim_s)`; `async frame_state(conn, *, saved_frame_id, user, catalogue) -> dict`; `async add_box(conn, *, saved_frame_id, user, body, catalogue) -> dict`; `async update_box(conn, *, saved_frame_id, box_id, user, body, catalogue) -> dict`; `async remove_box(conn, *, saved_frame_id, box_id, user)`; `async complete_frame(conn, *, saved_frame_id, user, catalogue) -> dict`; `async uncomplete_frame(conn, *, saved_frame_id)`.
- Frame state shape (read by Task 11): `{"saved_frame_id", "source_name", "ts", "frame": {"url", "width", "height"}, "boxes": [{"n", "event_id", "bbox", "state": "needs_answer"|"answered"|"answered_hidden"|"locked", "mine", "review_item_id", "zoom": {"url", "region"}, "outcome", "label", "common", "status", "model", "hints"}], "added": [{"id", "bbox", "outcome", "label", "common", "life_stage", "not_sure", "celebrity_id", "mine", "by", "zoom_url"}], "complete": {"at", "by"} | null, "blocking": [n, ...]}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_review_frames.py
"""Whole-frame mode (spec §8)."""
from __future__ import annotations

import io
import json
import random
from datetime import UTC, datetime, timedelta

import pytest
from PIL import Image
from review_fixtures import (
    CSRF,
    add_item,
    add_verdict,
    as_user,
    catalogue_paths,
    login,
    make_user,
    seed_frame,
)

from app.review_catalogue import CatalogueStore
from app.review_frames import (
    AddedBoxIn,
    add_box,
    complete_frame,
    frame_state,
    open_frame,
    remove_box,
    update_box,
)
from app.review_queue import Conflict, Forbidden, Invalid, VerdictIn, submit_verdict

pytestmark = pytest.mark.anyio

BOXES = [{"bbox": [10, 10, 110, 110], "species": "Seriola zonata"},
         {"bbox": [200, 10, 300, 110], "species": "Pomatomus saltarix"},
         {"bbox": [400, 10, 500, 110]},
         {"bbox": [600, 10, 700, 110], "species": "Seriola zonata"}]


@pytest.fixture
def cat(review_assets):
    return CatalogueStore(catalogue_paths(review_assets)).get()


async def _state(rdb, frame, user, cat):
    async with rdb.connection() as conn:
        return await frame_state(conn, saved_frame_id=frame["saved_frame_id"], user=user,
                                 catalogue=cat)


async def test_each_box_shows_what_this_volunteer_may_see(rdb, tmp_path, cat):
    f = await seed_frame(rdb, tmp_path, boxes=BOXES)
    me, other = await make_user(rdb, "me"), await make_user(rdb, "other")
    e = f["event_ids"]
    await add_verdict(rdb, e[0], other, name="Seriola zonata")          # answered, visible
    first = await add_verdict(rdb, e[1], other, name="Pomatomus saltatrix")
    await add_item(rdb, f, 1, pass_=2, exclude_user_id=other, first_correction_id=first)
    await add_item(rdb, f, 2, lane="identify", status="claimed", claimed_by=other,
                   claimed_until=datetime.now(UTC) + timedelta(minutes=5))
    state = await _state(rdb, f, as_user(me), cat)
    by_n = {b["n"]: b for b in state["boxes"]}
    assert by_n[1]["state"] == "answered" and by_n[1]["label"] == "Seriola zonata"
    # Review Focus 2: a box waiting for a second opinion hides its answer
    assert by_n[2]["state"] == "answered_hidden" and by_n[2]["label"] is None
    assert "Pomatomus saltatrix" not in json.dumps(by_n[2])
    assert by_n[3]["state"] == "locked"
    assert by_n[4]["state"] == "needs_answer" and by_n[4]["model"]["name"] == "Seriola zonata"
    assert state["blocking"] == [3, 4]
    # the first reviewer does see their own answer
    theirs = await _state(rdb, f, as_user(other), cat)
    assert theirs["boxes"][1]["state"] == "answered"


async def test_opening_claims_the_frames_open_boxes(rdb, tmp_path, cat):
    f = await seed_frame(rdb, tmp_path, boxes=BOXES)
    me = await make_user(rdb, "me")
    item = await add_item(rdb, f, 0, predicted="Seriola zonata", route="Seriola zonata")
    async with rdb.connection() as conn:
        await open_frame(conn, saved_frame_id=f["saved_frame_id"], user_id=me, claim_s=600)
    state = await _state(rdb, f, as_user(me), cat)
    assert state["boxes"][0]["review_item_id"] == item


async def test_completing_needs_every_model_box_answered(rdb, tmp_path, cat):
    f = await seed_frame(rdb, tmp_path, boxes=BOXES[:2])
    me = await make_user(rdb, "me")
    user = as_user(me)
    async with rdb.connection() as conn:
        with pytest.raises(Conflict, match="answer box 1, 2 first"):
            await complete_frame(conn, saved_frame_id=f["saved_frame_id"], user=user,
                                 catalogue=cat)
    for eid in f["event_ids"]:
        async with rdb.connection() as conn:
            await submit_verdict(conn, user=user, body=VerdictIn(event_id=eid, outcome="accept"),
                                 catalogue=cat, spot_rate=0.0, rng=random.Random(0))
    async with rdb.connection() as conn:
        done = await complete_frame(conn, saved_frame_id=f["saved_frame_id"], user=user,
                                    catalogue=cat)
        again = await complete_frame(conn, saved_frame_id=f["saved_frame_id"], user=user,
                                     catalogue=cat)
    assert done == {"complete": True, "already": False} and again["already"] is True


async def test_drawn_boxes_belong_to_whoever_drew_them(rdb, tmp_path, cat):
    f = await seed_frame(rdb, tmp_path, boxes=[])
    me, other = await make_user(rdb, "me"), await make_user(rdb, "other")
    boss = await make_user(rdb, "boss", role="admin")
    sf = f["saved_frame_id"]
    body = AddedBoxIn(bbox=[20, 20, 120, 90], outcome="species", label="Seriola zonata")
    async with rdb.connection() as conn:
        for bad in (AddedBoxIn(bbox=[20, 20, 22, 90], outcome="unidentifiable"),
                    AddedBoxIn(bbox=[20, 20, 120, 90], outcome="species", label="Nope nope"),
                    AddedBoxIn(bbox=[20, 20, 120, 90], outcome="unidentifiable",
                               label="Seriola zonata")):
            with pytest.raises(Invalid):
                await add_box(conn, saved_frame_id=sf, user=as_user(me), body=bad, catalogue=cat)
        box = await add_box(conn, saved_frame_id=sf, user=as_user(me), body=body, catalogue=cat)
        with pytest.raises(Forbidden):
            await update_box(conn, saved_frame_id=sf, box_id=box["id"], user=as_user(other),
                             body=body, catalogue=cat)
        await update_box(conn, saved_frame_id=sf, box_id=box["id"], user=as_user(me),
                         body=AddedBoxIn(bbox=[20, 20, 130, 95], outcome="unidentifiable"),
                         catalogue=cat)
        await complete_frame(conn, saved_frame_id=sf, user=as_user(me), catalogue=cat)
        with pytest.raises(Conflict):
            await remove_box(conn, saved_frame_id=sf, box_id=box["id"], user=as_user(me))
        await remove_box(conn, saved_frame_id=sf, box_id=box["id"],
                         user=as_user(boss, role="admin"))
    state = await _state(rdb, f, as_user(me), cat)
    assert state["added"] == [] and state["complete"] is not None


async def test_whole_frame_over_http(rdb, rclient, tmp_path):
    await make_user(rdb, "rev")
    login(rclient, "rev")
    f = await seed_frame(rdb, tmp_path, boxes=BOXES[:1])
    sf = f["saved_frame_id"]
    assert rclient.post(f"/api/review/frames/{sf}/open").status_code == 403       # no CSRF
    state = rclient.post(f"/api/review/frames/{sf}/open", headers=CSRF).json()
    assert state["boxes"][0]["state"] == "needs_answer"
    added = rclient.post(f"/api/review/frames/{sf}/boxes", headers=CSRF, json={
        "bbox": [300, 300, 400, 380], "outcome": "unidentifiable"}).json()
    zoom = rclient.get(f"/api/review/added/{added['id']}/zoom.jpg")
    assert zoom.status_code == 200 and Image.open(io.BytesIO(zoom.content)).size[0] > 0
    assert rclient.patch(f"/api/review/frames/{sf}/boxes/{added['id']}", headers=CSRF, json={
        "bbox": [300, 300, 410, 390], "outcome": "unidentifiable"}).status_code == 200
    assert rclient.post(f"/api/review/frames/{sf}/complete", headers=CSRF).status_code == 409
    rclient.post("/api/review/verdicts", headers=CSRF, json={
        "event_id": f["event_ids"][0], "outcome": "accept"})
    assert rclient.post(f"/api/review/frames/{sf}/complete", headers=CSRF).status_code == 200
    assert rclient.delete(f"/api/review/frames/{sf}/complete", headers=CSRF).status_code == 403
    assert rclient.get(f"/api/review/frames/{sf}").json()["complete"] is not None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn timeout 900 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest tests/test_review_frames.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.review_frames'`.

- [ ] **Step 3: Write `review_frames.py`**

```python
"""Whole-frame mode (spec §8).

A volunteer answers every box the model drew in one frame, draws boxes
around animals it missed, and marks the frame complete, which makes it
usable as detector training data.

* Opening the mode claims the frame's open pool items for the
  volunteer; boxes someone else holds are shown locked.
* A box with a pending spot-check shows as answered with the answer
  hidden (unless you gave it), so viewing the frame cannot contaminate
  the second opinion.
* Complete means every model box has a first-pass verdict from anyone.
  A drawn box always has an answer, because it cannot be saved without.
* Drawn boxes can be changed or removed by whoever drew them until the
  frame is complete; administrators can change them at any time.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from .auth import effective_role
from .review_images import frame_size, zoom_region
from .review_labels import resolve_all
from .review_queue import (
    Conflict,
    Forbidden,
    FrameMissing,
    Invalid,
    NotFound,
    fetch_all,
    fetch_one,
)


class AddedBoxIn(BaseModel):
    bbox: list[int]
    outcome: Literal["species", "unidentifiable"]
    label: str | None = Field(None, max_length=200)
    life_stage: str | None = Field(None, max_length=40)
    not_sure: bool = False
    celebrity_id: str | None = Field(None, max_length=80)


def _is_admin(user) -> bool:
    return effective_role(user.status, user.role) == "admin"


async def frame_record(conn, saved_frame_id: int) -> dict:
    row = await fetch_one(conn, """SELECT id, ts, source_name, frame_id, image_path
                                     FROM saved_frames WHERE id = %s""", (saved_frame_id,))
    if row is None:
        raise NotFound("no such frame")
    try:
        row["width"], row["height"] = frame_size(row["image_path"])
    except FileNotFoundError as e:
        raise FrameMissing("this frame is missing on disk") from e
    return row


async def open_frame(conn, *, saved_frame_id: int, user_id: int, claim_s: int) -> None:
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute("""
            UPDATE review_items
               SET status = 'claimed', claimed_by = %s,
                   claimed_until = now() + make_interval(secs => %s)
             WHERE saved_frame_id = %s AND pass = 1
               AND (status = 'open' OR (status = 'claimed' AND claimed_until < now()))""",
                          (user_id, claim_s, saved_frame_id))


async def frame_state(conn, *, saved_frame_id: int, user, catalogue) -> dict:
    fr = await frame_record(conn, saved_frame_id)
    events = await fetch_all(conn, """
        SELECT id, bbox, best_species_id, best_accuracy FROM detection_events
         WHERE source_name = %s AND ts = %s AND frame_id = %s ORDER BY id""",
                             (fr["source_name"], fr["ts"], fr["frame_id"]))
    ids = [e["id"] for e in events]
    resolved = await resolve_all(conn, ids) if ids else {}
    items = await fetch_all(conn, """
        SELECT id, event_id, pass, status, claimed_by, claimed_until, hints
          FROM review_items WHERE event_id = ANY(%s)""", (ids,)) if ids else []
    mine = {r["event_id"] for r in await fetch_all(conn, """
        SELECT DISTINCT event_id FROM detection_corrections
         WHERE event_id = ANY(%s) AND user_id = %s""", (ids, user.id))} if ids else set()
    now = datetime.now(UTC)
    boxes = []
    for n, e in enumerate(events, 1):
        its = [i for i in items if i["event_id"] == e["id"]]
        pending_check = any(i["pass"] == 2 and i["status"] in ("open", "claimed") for i in its)
        held_by_other = any(i["pass"] == 1 and i["status"] == "claimed"
                            and i["claimed_by"] != user.id and i["claimed_until"] > now
                            for i in its)
        my_item = next((i for i in its if i["pass"] == 1 and i["status"] == "claimed"
                        and i["claimed_by"] == user.id), None)
        r = resolved.get(e["id"])
        if r is None:
            state = "locked" if held_by_other else "needs_answer"
        elif pending_check and e["id"] not in mine and not _is_admin(user):
            state = "answered_hidden"
        else:
            state = "answered"
        box = {"n": n, "event_id": e["id"], "bbox": list(e["bbox"]), "state": state,
               "mine": e["id"] in mine, "review_item_id": my_item["id"] if my_item else None,
               "zoom": {"url": f"/api/review/boxes/{e['id']}/zoom.jpg",
                        "region": list(zoom_region(e["bbox"], fr["width"], fr["height"]))},
               "outcome": None, "label": None, "common": None, "status": None,
               "model": None, "hints": None}
        if state == "answered":
            box.update(outcome=r.outcome[0], label=r.species,
                       common=catalogue.common_name(r.species), status=r.status)
        elif state == "needs_answer":
            guess = catalogue.accepted(e["best_species_id"])
            if guess:
                box["model"] = {"name": guess, "p": e["best_accuracy"],
                                "common": catalogue.common_name(guess)}
            if my_item and my_item["hints"]:
                box["hints"] = [{"name": h["name"], "p": h.get("p"),
                                 "common": catalogue.common_name(h["name"])}
                                for h in my_item["hints"] if h.get("name") != guess][:3]
        boxes.append(box)
    added = await fetch_all(conn, """
        SELECT a.id, a.bbox, a.outcome, a.label, a.life_stage, a.confidence, a.celebrity_id,
               a.user_id, u.display_name
          FROM review_added_boxes a JOIN users u ON u.id = a.user_id
         WHERE a.saved_frame_id = %s AND a.removed_at IS NULL
         ORDER BY a.id""", (saved_frame_id,))
    done = await fetch_one(conn, """
        SELECT f.completed_at, u.display_name FROM review_frames f
          JOIN users u ON u.id = f.completed_by
         WHERE f.saved_frame_id = %s""", (saved_frame_id,))
    return {
        "saved_frame_id": saved_frame_id, "source_name": fr["source_name"],
        "ts": fr["ts"].isoformat(),
        "frame": {"url": f"/api/review/frames/{saved_frame_id}/image.jpg",
                  "width": fr["width"], "height": fr["height"]},
        "boxes": boxes,
        "added": [{"id": a["id"], "bbox": list(a["bbox"]), "outcome": a["outcome"],
                   "label": a["label"], "common": catalogue.common_name(a["label"]),
                   "life_stage": a["life_stage"], "not_sure": a["confidence"] == "uncertain",
                   "celebrity_id": a["celebrity_id"], "mine": a["user_id"] == user.id,
                   "by": a["display_name"], "zoom_url": f"/api/review/added/{a['id']}/zoom.jpg"}
                  for a in added],
        "complete": ({"at": done["completed_at"].isoformat(), "by": done["display_name"]}
                     if done else None),
        "blocking": [b["n"] for b in boxes if b["state"] not in ("answered", "answered_hidden")],
    }


def _validate_added(body: AddedBoxIn, *, catalogue, width: int, height: int) -> dict:
    if len(body.bbox) != 4:
        raise Invalid("a box is [x1, y1, x2, y2]")
    x1, y1, x2, y2 = (int(v) for v in body.bbox)
    if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
        raise Invalid("the box must lie inside the frame")
    if x2 - x1 < 4 or y2 - y1 < 4:
        raise Invalid("the box is too small")
    label = life_stage = None
    if body.outcome == "species":
        if not body.label or not catalogue.entry(body.label, body.life_stage):
            raise Invalid("pick a species from the list")
        label, life_stage = body.label, body.life_stage
    elif body.label:
        raise Invalid("no species goes with this answer")
    elif body.not_sure:
        raise Invalid("'not sure' only applies to a species")
    celebrity_id = (body.celebrity_id or "").strip() or None
    if celebrity_id and celebrity_id not in catalogue.celebrity_ids(label):
        raise Invalid("that named individual is not recorded for this species")
    return {"bbox": [x1, y1, x2, y2], "label": label, "life_stage": life_stage,
            "confidence": "uncertain" if body.not_sure else "certain",
            "celebrity_id": celebrity_id}


async def _is_complete(conn, saved_frame_id: int) -> bool:
    return await fetch_one(conn, "SELECT 1 FROM review_frames WHERE saved_frame_id = %s",
                           (saved_frame_id,)) is not None


async def add_box(conn, *, saved_frame_id: int, user, body: AddedBoxIn, catalogue) -> dict:
    fr = await frame_record(conn, saved_frame_id)
    v = _validate_added(body, catalogue=catalogue, width=fr["width"], height=fr["height"])
    async with conn.transaction():
        if not _is_admin(user) and await _is_complete(conn, saved_frame_id):
            raise Conflict("this frame is already marked complete")
        row = await fetch_one(conn, """
            INSERT INTO review_added_boxes (saved_frame_id, bbox, outcome, label, life_stage,
                                            confidence, celebrity_id, user_id, session_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                              (saved_frame_id, v["bbox"], body.outcome, v["label"],
                               v["life_stage"], v["confidence"], v["celebrity_id"], user.id,
                               user.session_id))
    return {"id": row["id"]}


async def _own_box(conn, *, saved_frame_id: int, box_id: int, user) -> None:
    row = await fetch_one(conn, """SELECT saved_frame_id, user_id FROM review_added_boxes
                                    WHERE id = %s AND removed_at IS NULL FOR UPDATE""",
                          (box_id,))
    if row is None or row["saved_frame_id"] != saved_frame_id:
        raise NotFound("no such drawn box")
    if _is_admin(user):
        return
    if row["user_id"] != user.id:
        raise Forbidden("you can only change boxes you drew")
    if await _is_complete(conn, saved_frame_id):
        raise Conflict("this frame is already marked complete")


async def update_box(conn, *, saved_frame_id: int, box_id: int, user, body: AddedBoxIn,
                     catalogue) -> dict:
    fr = await frame_record(conn, saved_frame_id)
    v = _validate_added(body, catalogue=catalogue, width=fr["width"], height=fr["height"])
    async with conn.transaction():
        await _own_box(conn, saved_frame_id=saved_frame_id, box_id=box_id, user=user)
        async with conn.cursor() as cur:
            await cur.execute("""
                UPDATE review_added_boxes
                   SET bbox = %s, outcome = %s, label = %s, life_stage = %s, confidence = %s,
                       celebrity_id = %s, updated_at = now()
                 WHERE id = %s""", (v["bbox"], body.outcome, v["label"], v["life_stage"],
                                    v["confidence"], v["celebrity_id"], box_id))
    return {"id": box_id}


async def remove_box(conn, *, saved_frame_id: int, box_id: int, user) -> None:
    async with conn.transaction():
        await _own_box(conn, saved_frame_id=saved_frame_id, box_id=box_id, user=user)
        async with conn.cursor() as cur:
            await cur.execute("UPDATE review_added_boxes SET removed_at = now() WHERE id = %s",
                              (box_id,))


async def complete_frame(conn, *, saved_frame_id: int, user, catalogue) -> dict:
    state = await frame_state(conn, saved_frame_id=saved_frame_id, user=user,
                              catalogue=catalogue)
    if state["complete"]:
        return {"complete": True, "already": True}
    if state["blocking"]:
        raise Conflict("answer box " + ", ".join(str(n) for n in state["blocking"]) + " first")
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute("""INSERT INTO review_frames (saved_frame_id, completed_by, session_id)
                             VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
                          (saved_frame_id, user.id, user.session_id))
    return {"complete": True, "already": False}


async def uncomplete_frame(conn, *, saved_frame_id: int) -> None:
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute("DELETE FROM review_frames WHERE saved_frame_id = %s",
                          (saved_frame_id,))
```

- [ ] **Step 4: Add the routes**

In `routes_review.py`, add to the imports:

```python
from .review_frames import (
    AddedBoxIn,
    add_box,
    complete_frame,
    frame_state,
    open_frame,
    remove_box,
    uncomplete_frame,
    update_box,
)
```

and insert immediately above the line `_ = admin_only    # used by the admin routes added in Task 9`:

```python
    # ---- whole-frame mode (Task 8) ----

    async def _frame_call(fn, **kw):
        async with _db().connection() as conn:
            try:
                return JSONResponse(await fn(conn, **kw))
            except ReviewError as e:
                return _fail(e)

    @router.post("/api/review/frames/{saved_frame_id}/open", dependencies=[Depends(require_csrf)])
    async def frame_open(saved_frame_id: int, user=Depends(reviewer)):
        async with _db().connection() as conn:
            try:
                await open_frame(conn, saved_frame_id=saved_frame_id, user_id=user.id,
                                 claim_s=cfg.review_claim_s)
                return JSONResponse(await frame_state(conn, saved_frame_id=saved_frame_id,
                                                      user=user, catalogue=_catalogue()))
            except ReviewError as e:
                return _fail(e)

    @router.get("/api/review/frames/{saved_frame_id}")
    async def frame_get(saved_frame_id: int, user=Depends(reviewer)):
        return await _frame_call(frame_state, saved_frame_id=saved_frame_id, user=user,
                                 catalogue=_catalogue())

    @router.post("/api/review/frames/{saved_frame_id}/boxes",
                 dependencies=[Depends(require_csrf)])
    async def frame_add_box(saved_frame_id: int, body: AddedBoxIn, user=Depends(reviewer)):
        return await _frame_call(add_box, saved_frame_id=saved_frame_id, user=user, body=body,
                                 catalogue=_catalogue())

    @router.patch("/api/review/frames/{saved_frame_id}/boxes/{box_id}",
                  dependencies=[Depends(require_csrf)])
    async def frame_update_box(saved_frame_id: int, box_id: int, body: AddedBoxIn,
                               user=Depends(reviewer)):
        return await _frame_call(update_box, saved_frame_id=saved_frame_id, box_id=box_id,
                                 user=user, body=body, catalogue=_catalogue())

    @router.delete("/api/review/frames/{saved_frame_id}/boxes/{box_id}",
                   dependencies=[Depends(require_csrf)])
    async def frame_remove_box(saved_frame_id: int, box_id: int, user=Depends(reviewer)):
        async with _db().connection() as conn:
            try:
                await remove_box(conn, saved_frame_id=saved_frame_id, box_id=box_id, user=user)
            except ReviewError as e:
                return _fail(e)
        return JSONResponse({"ok": True})

    @router.post("/api/review/frames/{saved_frame_id}/complete",
                 dependencies=[Depends(require_csrf)])
    async def frame_complete(saved_frame_id: int, user=Depends(reviewer)):
        return await _frame_call(complete_frame, saved_frame_id=saved_frame_id, user=user,
                                 catalogue=_catalogue())

    @router.delete("/api/review/frames/{saved_frame_id}/complete",
                   dependencies=[Depends(require_csrf)])
    async def frame_uncomplete(saved_frame_id: int, user=Depends(admin_only)):
        async with _db().connection() as conn:
            await uncomplete_frame(conn, saved_frame_id=saved_frame_id)
        return JSONResponse({"ok": True})

    @router.get("/api/review/added/{box_id}/zoom.jpg")
    async def added_zoom(box_id: int, user=Depends(reviewer)):
        async with _db().connection() as conn:
            row = await fetch_one(conn, """
                SELECT a.bbox, sf.image_path FROM review_added_boxes a
                  JOIN saved_frames sf ON sf.id = a.saved_frame_id
                 WHERE a.id = %s""", (box_id,))
        if row is None:
            return JSONResponse({"error": "no such drawn box"}, status_code=404)
        try:
            width, height = await asyncio.to_thread(frame_size, row["image_path"])
        except FileNotFoundError:
            return JSONResponse({"error": "image file missing on disk"}, status_code=404)
        return await _jpeg(render_zoom, row["image_path"],
                           zoom_region(row["bbox"], width, height))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn timeout 900 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest tests/test_review_frames.py tests/test_review_routes.py -q`
Expected: all pass.

- [ ] **Step 6: Lint and commit**

```bash
/raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/ruff check services/dashboard/app/review_frames.py services/dashboard/app/routes_review.py tests/test_review_frames.py
git add services/dashboard/app/review_frames.py services/dashboard/app/routes_review.py tests/test_review_frames.py
git commit -m "review: whole-frame mode for detector-grade frames

A volunteer can answer every box in a frame, draw the animals the model
missed, and mark the frame complete once every model box has an answer.
Boxes held by someone else are locked, and a box waiting for a second
opinion shows as answered without its answer.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: Browse, progress and the admin tools

**Files:**
- Create: `services/dashboard/app/review_browse.py`
- Modify: `services/dashboard/app/routes_review.py` (imports; routes replace the `_ = admin_only` line)
- Test: `tests/test_review_browse.py`

**Interfaces:**
- Consumes: `load_verdicts`, `resolve`, `outcome_of`, `agreement_summary` (Task 5); `read_window`, `retire_outside_window`, `ReviewPool` (Task 6); `fetch_one`, `fetch_all`, `Invalid`, `NotFound`, `VerdictIn`, `submit_verdict` (Task 7).
- Produces: `LOCAL_TZ`, `STATUSES`, `PAGE_SIZE = 48`; `BrowseFilters(status="any", species=None, camera=None, who="anyone", lane="any", date_from=None, date_to=None)`; `WindowIn(start: date | None, end: date | None)`; `AdjudicateIn(outcome, label, life_stage, bbox)`; `async progress_summary(conn, *, pool_obj, catalogue, session_id, target) -> dict`; `async browse(conn, *, catalogue, user, filters, page, page_size=PAGE_SIZE) -> dict`; `async list_disputes(conn, *, catalogue) -> list[dict]`; `async agreement(conn) -> dict`; `async get_window(conn) -> dict`; `async set_window(conn, *, admin_id, start, end) -> dict`; `async pool_status(conn, *, pool_obj) -> dict`.
- Browse cell shape (read by Task 12): `{"kind": "box"|"added"|"queue", "event_id", "added_id", "saved_frame_id", "zoom_url", "status", "outcome", "label", "common", "camera", "ts", "lane", "mine", "reviewers", "last_at"}`; status `awaiting_second_opinion` hides the answer.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_review_browse.py
"""Progress, browse, disputes, agreement, the date window, pool status."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from review_fixtures import (
    CSRF,
    T0,
    add_item,
    add_verdict,
    as_user,
    catalogue_paths,
    login,
    make_user,
    seed_frame,
)

from app.review_browse import (
    BrowseFilters,
    agreement,
    browse,
    list_disputes,
    pool_status,
    progress_summary,
    set_window,
)
from app.review_catalogue import CatalogueStore
from app.review_pool import PoolSettings, ReviewPool

pytestmark = pytest.mark.anyio


@pytest.fixture
def store(review_assets):
    return CatalogueStore(catalogue_paths(review_assets))


async def _world(rdb, tmp_path):
    """Five answered boxes and one open queue item, by two volunteers."""
    one, two = await make_user(rdb, "one"), await make_user(rdb, "two")
    f = await seed_frame(rdb, tmp_path, ts=T0, boxes=[
        {"bbox": [10, 10, 90, 90], "species": "Seriola zonata"},
        {"bbox": [100, 10, 190, 90], "species": "Seriola zonata"},
        {"bbox": [200, 10, 290, 90]},
        {"bbox": [300, 10, 390, 90], "species": "Seriola zonata"},
        {"bbox": [400, 10, 490, 90], "species": "Seriola zonata"}])
    g = await seed_frame(rdb, tmp_path, ts=T0 + timedelta(days=1), frame_id=2,
                         source="pier_cam", boxes=[{"bbox": [10, 10, 90, 90]}])
    e = f["event_ids"]
    await add_verdict(rdb, e[0], one, name="Seriola zonata")                     # single
    a1 = await add_verdict(rdb, e[1], one, name="Seriola zonata", label_source="model_guess")
    s1 = await add_item(rdb, f, 1, pass_=2, status="done", exclude_user_id=one,
                        first_correction_id=a1)
    await add_verdict(rdb, e[1], two, name="Seriola zonata", item_id=s1)          # agreed
    await add_verdict(rdb, e[2], one, not_a_fish=True)                            # no animal
    d1 = await add_verdict(rdb, e[3], one, name="Seriola zonata", label_source="model_guess")
    s2 = await add_item(rdb, f, 3, pass_=2, status="done", exclude_user_id=one,
                        first_correction_id=d1)
    await add_verdict(rdb, e[3], two, unidentifiable=True, item_id=s2)            # disputed
    p1 = await add_verdict(rdb, e[4], one, name="Seriola zonata")
    await add_item(rdb, f, 4, pass_=2, exclude_user_id=one, first_correction_id=p1)  # pending
    await add_item(rdb, g, 0, lane="identify")                                    # in queue
    return {"one": one, "two": two, "f": f, "g": g}


async def test_progress_counts(rdb, tmp_path, store, review_assets):
    w = await _world(rdb, tmp_path)
    pool = ReviewPool(rdb, PoolSettings(target=200), store, review_assets["baseline"])
    await pool.refresh_progress()
    async with rdb.connection() as conn:
        p = await progress_summary(conn, pool_obj=pool, catalogue=store.get(), session_id=None,
                                   target=200)
    k = p["kpis"]
    assert k["verified_boxes"] == 4           # single, agreed, no-animal, pending-but-single
    assert k["species_with_verified"] == 1
    assert k["open"] == {"check": 0, "identify": 1, "spot_checks": 1}
    seriola = next(s for s in p["species"] if s["name"] == "Seriola zonata")
    assert seriola["baseline"] == 3 and seriola["verified"] == 3
    assert p["species"][0]["baseline"] + p["species"][0]["verified"] == 0   # thinnest first
    assert w


async def test_browse_filters(rdb, tmp_path, store):
    w = await _world(rdb, tmp_path)
    one = as_user(w["one"])
    two = as_user(w["two"])

    async def cells(user, **kw):
        async with rdb.connection() as conn:
            return (await browse(conn, catalogue=store.get(), user=user,
                                 filters=BrowseFilters(**kw), page=0))["cells"]

    assert {c["status"] for c in await cells(one, status="disputed")} == {"disputed"}
    assert len(await cells(one, status="verified")) == 4
    assert [c["kind"] for c in await cells(one, status="in_queue")] == ["queue"]
    assert len(await cells(two, who="me")) == 2
    assert all(c["camera"] == "pier_cam" for c in await cells(one, status="in_queue",
                                                              camera="pier_cam"))
    # the pending spot-check hides its answer from everyone but its first reviewer
    hidden = [c for c in await cells(two) if c["status"] == "awaiting_second_opinion"]
    assert len(hidden) == 1 and hidden[0]["label"] is None
    assert not [c for c in await cells(one) if c["status"] == "awaiting_second_opinion"]


async def test_disputes_and_adjudication(rdb, rclient, tmp_path):
    w = await _world(rdb, tmp_path)
    await make_user(rdb, "boss", role="admin")
    login(rclient, "boss")
    disputes = rclient.get("/api/review/disputes").json()["disputes"]
    assert len(disputes) == 1
    d = disputes[0]
    assert d["first"]["label"] == "Seriola zonata" and d["second"]["outcome"] == "unidentifiable"
    r = rclient.post(f"/api/review/disputes/{d['event_id']}/adjudicate", headers=CSRF,
                     json={"outcome": "species", "label": "Seriola zonata"})
    assert r.status_code == 200
    assert rclient.get("/api/review/disputes").json()["disputes"] == []
    assert w


async def test_agreement_numbers(rdb, tmp_path):
    await _world(rdb, tmp_path)
    async with rdb.connection() as conn:
        a = await agreement(conn)
    assert a["overall"]["pairs"] == 2 and a["overall"]["percent"] == 0.5
    assert a["by_label_source"]["model_guess"]["pairs"] == 2


async def test_the_window_end_day_is_inclusive_in_local_time(rdb, tmp_path):
    """Review Focus 4: 2026-06-25 18:00 EDT is inside a window ending 2026-06-25."""
    boss = await make_user(rdb, "boss", role="admin")
    inside = await seed_frame(rdb, tmp_path, ts=datetime(2026, 6, 25, 22, 0, tzinfo=UTC),
                              frame_id=1, boxes=[{"bbox": [10, 10, 90, 90]}])
    outside = await seed_frame(rdb, tmp_path, ts=datetime(2026, 6, 26, 9, 0, tzinfo=UTC),
                               frame_id=2, boxes=[{"bbox": [10, 10, 90, 90]}])
    i_in, i_out = await add_item(rdb, inside, lane="identify"), await add_item(
        rdb, outside, lane="identify")
    async with rdb.connection() as conn:
        out = await set_window(conn, admin_id=boss, start=date(2026, 5, 17),
                               end=date(2026, 6, 25))
        assert out["retired"] == 1
        statuses = dict(await (await conn.execute(
            "SELECT id, status FROM review_items")).fetchall())
    assert statuses[i_in] == "open" and statuses[i_out] == "retired"


async def test_admin_endpoints_are_admin_only(rdb, rclient):
    await make_user(rdb, "rev")
    login(rclient, "rev")
    for path in ("/api/review/disputes", "/api/review/agreement", "/api/review/status",
                 "/api/review/settings/window"):
        assert rclient.get(path).status_code == 403
    assert rclient.get("/api/review/progress").status_code == 200
    await make_user(rdb, "boss", role="admin")
    login(rclient, "boss")
    assert rclient.put("/api/review/settings/window",
                       json={"start": "2026-05-17", "end": "2026-06-25"}).status_code == 403
    r = rclient.put("/api/review/settings/window", headers=CSRF,
                    json={"start": "2026-05-17", "end": "2026-06-25"})
    assert r.status_code == 200 and r.json()["end"].startswith("2026-06-26T00:00:00")
    status = rclient.get("/api/review/status").json()
    assert status["window"]["end"].startswith("2026-06-26") and "last_run" in status


async def test_pool_status_counts_items(rdb, tmp_path):
    await _world(rdb, tmp_path)
    async with rdb.connection() as conn:
        s = await pool_status(conn, pool_obj=None)
    assert {"lane": "identify", "pass": 1, "status": "open", "count": 1} in s["items"]
    assert s["unscored"] == 2 and s["last_run"] is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn timeout 900 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest tests/test_review_browse.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.review_browse'`.

- [ ] **Step 3: Write `review_browse.py`**

```python
"""Progress, browse, disputes, agreement, the date window and pool status
(spec §9, Browse; §8, Agreement).

Browse follows the same rule as whole-frame mode: a box waiting for a
second opinion shows as "awaiting a second opinion" with no answer to
everyone except the volunteer who gave the first answer and
administrators, so browsing cannot contaminate a spot-check.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from typing import Literal
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from .auth import effective_role
from .review_labels import agreement_summary, load_verdicts, outcome_of, resolve
from .review_pool import read_window, retire_outside_window
from .review_queue import Invalid, fetch_all, fetch_one

LOCAL_TZ = ZoneInfo("America/New_York")
STATUSES = ("any", "verified", "agreed", "adjudicated", "disputed", "unidentifiable",
            "no_animal", "in_queue")
PAGE_SIZE = 48


@dataclass
class BrowseFilters:
    status: str = "any"
    species: str | None = None
    camera: str | None = None
    who: str = "anyone"            # anyone | me | session
    lane: str = "any"              # any | check | identify
    date_from: date | None = None
    date_to: date | None = None


class WindowIn(BaseModel):
    start: date | None = None
    end: date | None = None


class AdjudicateIn(BaseModel):
    outcome: Literal["species", "unidentifiable", "no_animal"]
    label: str | None = Field(None, max_length=200)
    life_stage: str | None = Field(None, max_length=40)
    bbox: list[int] | None = None


def local_day_start(d: date) -> datetime:
    return datetime.combine(d, dtime.min, tzinfo=LOCAL_TZ)


async def _resolved(conn) -> dict:
    out = {}
    for event_id, verdicts in (await load_verdicts(conn)).items():
        r = resolve(verdicts)
        if r is not None:
            out[event_id] = (r, verdicts)
    return out


async def _pending_spot_checks(conn) -> set[int]:
    return {r["event_id"] for r in await fetch_all(conn, """
        SELECT event_id FROM review_items
         WHERE pass = 2 AND status IN ('open', 'claimed')""")}


async def progress_summary(conn, *, pool_obj, catalogue, session_id, target: int) -> dict:
    resolved = await _resolved(conn)
    verified = [r for r, _v in resolved.values() if r.is_verified]
    added = await fetch_all(conn, """SELECT label, outcome FROM review_added_boxes
                                      WHERE removed_at IS NULL""")
    species_seen = ({r.species for r in verified if r.species}
                    | {a["label"] for a in added if a["outcome"] == "species"})
    frames = (await fetch_one(conn, "SELECT count(*) AS n FROM review_frames"))["n"]
    session = 0
    if session_id is not None:
        session = (await fetch_one(conn, """SELECT count(*) AS n FROM detection_corrections
                                             WHERE session_id = %s""", (session_id,)))["n"]
    open_counts = {"check": 0, "identify": 0, "spot_checks": 0}
    for r in await fetch_all(conn, """SELECT lane, pass, count(*) AS n FROM review_items
                                       WHERE status IN ('open', 'claimed')
                                       GROUP BY lane, pass"""):
        open_counts["spot_checks" if r["pass"] == 2 else r["lane"]] += r["n"]
    species = [{"name": name, "common": catalogue.common_name(name),
                "baseline": pool_obj.baseline(name) if pool_obj else 0,
                "verified": pool_obj.verified(name) if pool_obj else 0,
                "target": target} for name in catalogue.names()]
    species.sort(key=lambda s: (s["baseline"] + s["verified"], s["name"]))
    return {"kpis": {"verified_boxes": len(verified) + len(added),
                     "species_with_verified": len(species_seen),
                     "complete_frames": frames, "session_verified": session,
                     "open": open_counts},
            "species": species, "target": target}


def _status_matches(r, status: str) -> bool:
    if status == "any":
        return True
    if status == "verified":
        return r.is_verified
    if status in ("agreed", "adjudicated", "disputed"):
        return r.status == status
    return r.is_verified and r.outcome[0] == status       # unidentifiable | no_animal


async def browse(conn, *, catalogue, user, filters: BrowseFilters, page: int,
                 page_size: int = PAGE_SIZE) -> dict:
    if filters.status not in STATUSES:
        raise Invalid("unknown status filter")
    start = local_day_start(filters.date_from) if filters.date_from else None
    end = local_day_start(filters.date_to + timedelta(days=1)) if filters.date_to else None
    if filters.status == "in_queue":
        rows = await fetch_all(conn, """
            SELECT event_id, lane, source_name, ts, route_species, saved_frame_id, added_at
              FROM review_items
             WHERE pass = 1 AND status IN ('open', 'claimed')
               AND (%(lane)s = 'any' OR lane = %(lane)s)
               AND (%(camera)s::text IS NULL OR source_name = %(camera)s)
               AND (%(species)s::text IS NULL OR route_species = %(species)s)
               AND (%(start)s::timestamptz IS NULL OR ts >= %(start)s)
               AND (%(end)s::timestamptz IS NULL OR ts < %(end)s)
             ORDER BY added_at DESC, id DESC
             LIMIT %(limit)s OFFSET %(offset)s""", {
            "lane": filters.lane, "camera": filters.camera, "species": filters.species,
            "start": start, "end": end, "limit": page_size + 1, "offset": page * page_size})
        cells = [{"kind": "queue", "event_id": r["event_id"], "added_id": None,
                  "saved_frame_id": r["saved_frame_id"],
                  "zoom_url": f"/api/review/boxes/{r['event_id']}/zoom.jpg",
                  "status": "in_queue", "outcome": None, "label": r["route_species"],
                  "common": catalogue.common_name(r["route_species"]),
                  "camera": r["source_name"], "ts": r["ts"].isoformat(), "lane": r["lane"],
                  "mine": False, "reviewers": [], "last_at": r["added_at"].isoformat()}
                 for r in rows[:page_size]]
        return {"cells": cells, "page": page, "has_more": len(rows) > page_size}

    admin = effective_role(user.status, user.role) == "admin"
    resolved = await _resolved(conn)
    pending = await _pending_spot_checks(conn)
    ids = list(resolved)
    meta = {r["id"]: r for r in await fetch_all(conn, """
        SELECT id, source_name, ts, best_species_id FROM detection_events
         WHERE id = ANY(%s)""", (ids,))} if ids else {}
    frames = {r["event_id"]: r["saved_frame_id"] for r in await fetch_all(conn, """
        SELECT de.id AS event_id, sf.id AS saved_frame_id
          FROM detection_events de
          JOIN saved_frames sf
            ON sf.frame_id = de.frame_id AND sf.source_name = de.source_name AND sf.ts = de.ts
         WHERE de.id = ANY(%s)""", (ids,))} if ids else {}
    names = {r["id"]: r["display_name"]
             for r in await fetch_all(conn, "SELECT id, display_name FROM users")}
    cells = []
    for event_id, (r, verdicts) in resolved.items():
        m = meta.get(event_id)
        if m is None:
            continue
        mine = any(v.user_id == user.id for v in verdicts)
        hidden = event_id in pending and not admin and not mine
        if hidden and (filters.status != "any" or filters.species):
            continue
        if not hidden and not _status_matches(r, filters.status):
            continue
        if filters.species and r.species != filters.species:
            continue
        lane = "check" if m["best_species_id"] else "identify"
        if ((filters.camera and m["source_name"] != filters.camera)
                or (filters.lane != "any" and lane != filters.lane)
                or (start and m["ts"] < start) or (end and m["ts"] >= end)):
            continue
        if filters.who == "me" and not mine:
            continue
        if filters.who == "session" and not (
                user.session_id is not None
                and any(v.session_id == user.session_id for v in verdicts)):
            continue
        cell = {"kind": "box", "event_id": event_id, "added_id": None,
                "saved_frame_id": frames.get(event_id),
                "zoom_url": f"/api/review/boxes/{event_id}/zoom.jpg",
                "status": r.status, "outcome": r.outcome[0], "label": r.species,
                "common": catalogue.common_name(r.species), "camera": m["source_name"],
                "ts": m["ts"].isoformat(), "lane": lane, "mine": mine,
                "reviewers": sorted({names.get(v.user_id, "?") for v in verdicts if v.user_id}),
                "last_at": max(v.created_at for v in verdicts)}
        if hidden:
            cell.update(status="awaiting_second_opinion", outcome=None, label=None, common=None,
                        reviewers=[])
        cells.append(cell)
    if filters.status in ("any", "verified") and filters.lane == "any":
        for a in await fetch_all(conn, """
                SELECT a.id, a.saved_frame_id, a.label, a.outcome, a.user_id, a.session_id,
                       a.created_at, sf.source_name, sf.ts
                  FROM review_added_boxes a JOIN saved_frames sf ON sf.id = a.saved_frame_id
                 WHERE a.removed_at IS NULL"""):
            if ((filters.species and a["label"] != filters.species)
                    or (filters.camera and a["source_name"] != filters.camera)
                    or (start and a["ts"] < start) or (end and a["ts"] >= end)
                    or (filters.who == "me" and a["user_id"] != user.id)
                    or (filters.who == "session"
                        and (user.session_id is None or a["session_id"] != user.session_id))):
                continue
            cells.append({"kind": "added", "event_id": None, "added_id": a["id"],
                          "saved_frame_id": a["saved_frame_id"],
                          "zoom_url": f"/api/review/added/{a['id']}/zoom.jpg",
                          "status": "single", "outcome": a["outcome"], "label": a["label"],
                          "common": catalogue.common_name(a["label"]),
                          "camera": a["source_name"], "ts": a["ts"].isoformat(),
                          "lane": "drawn", "mine": a["user_id"] == user.id,
                          "reviewers": [names.get(a["user_id"], "?")],
                          "last_at": a["created_at"]})
    cells.sort(key=lambda c: c["last_at"], reverse=True)
    page_cells = cells[page * page_size:(page + 1) * page_size]
    for c in page_cells:
        c["last_at"] = c["last_at"].isoformat()
    return {"cells": page_cells, "page": page, "total": len(cells),
            "has_more": len(cells) > (page + 1) * page_size}


async def list_disputes(conn, *, catalogue) -> list[dict]:
    names = {r["id"]: r["display_name"]
             for r in await fetch_all(conn, "SELECT id, display_name FROM users")}

    def side(v):
        kind, *rest = outcome_of(v)
        label = rest[0] if rest else None
        return {"outcome": kind, "label": label, "common": catalogue.common_name(label),
                "life_stage": v.life_stage, "by": names.get(v.user_id, "?"),
                "at": v.created_at.isoformat()}

    out = [{"event_id": event_id, "zoom_url": f"/api/review/boxes/{event_id}/zoom.jpg",
            "first": side(r.first), "second": side(r.second)}
           for event_id, (r, _v) in (await _resolved(conn)).items() if r.status == "disputed"]
    return sorted(out, key=lambda d: d["event_id"])


async def agreement(conn) -> dict:
    return agreement_summary({event_id: r for event_id, (r, _v) in (await _resolved(conn)).items()})


async def get_window(conn) -> dict:
    start, end = await read_window(conn)
    return {"start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None}


async def set_window(conn, *, admin_id: int, start: date | None, end: date | None) -> dict:
    """Both days are local (America/New_York) and inclusive: the stored end
    is local midnight at the start of the day after `end` (Review Focus 4)."""
    if start and end and start > end:
        raise Invalid("the window starts after it ends")
    async with conn.transaction():
        async with conn.cursor() as cur:
            if start is None and end is None:
                await cur.execute("DELETE FROM review_settings WHERE key = 'window'")
                return {"start": None, "end": None, "retired": 0}
            value = {"start": local_day_start(start).isoformat() if start else None,
                     "end": local_day_start(end + timedelta(days=1)).isoformat() if end else None}
            await cur.execute("""
                INSERT INTO review_settings (key, value, updated_by, updated_at)
                VALUES ('window', %s, %s, now())
                ON CONFLICT (key) DO UPDATE
                   SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by,
                       updated_at = now()""", (Jsonb(value), admin_id))
        retired = await retire_outside_window(
            conn,
            datetime.fromisoformat(value["start"]) if value["start"]
            else datetime.min.replace(tzinfo=UTC),
            datetime.fromisoformat(value["end"]) if value["end"]
            else datetime.max.replace(tzinfo=UTC))
    return {**value, "retired": retired}


async def pool_status(conn, *, pool_obj) -> dict:
    items = await fetch_all(conn, """SELECT lane, pass, status, count(*) AS n FROM review_items
                                      GROUP BY lane, pass, status ORDER BY lane, pass, status""")
    unscored = (await fetch_one(conn, """SELECT count(*) AS n FROM review_items
                                          WHERE hints IS NULL
                                            AND status IN ('open', 'claimed')""", None))["n"]
    last = (await fetch_one(conn, "SELECT max(hinted_at) AS t FROM review_items"))["t"]
    return {"items": [{"lane": r["lane"], "pass": r["pass"], "status": r["status"],
                       "count": r["n"]} for r in items],
            "unscored": unscored, "last_hinted_at": last.isoformat() if last else None,
            "last_run": pool_obj.last_run if pool_obj else None,
            "window": await get_window(conn)}
```

- [ ] **Step 4: Add the routes**

In `routes_review.py`, add `from datetime import date` to the imports, and:

```python
from .review_browse import (
    AdjudicateIn,
    BrowseFilters,
    WindowIn,
    agreement,
    browse,
    get_window,
    list_disputes,
    pool_status,
    progress_summary,
    set_window,
)
```

Replace the line `_ = admin_only    # used by the admin routes added in Task 9` with:

```python
    # ---- browse, progress and the admin tools (Task 9) ----

    @router.get("/api/review/progress")
    async def progress(user=Depends(reviewer)):
        pool_obj = _pool()
        if pool_obj:
            await pool_obj.refresh_progress()
        async with _db().connection() as conn:
            return JSONResponse(await progress_summary(
                conn, pool_obj=pool_obj, catalogue=_catalogue(), session_id=user.session_id,
                target=cfg.review_target))

    @router.get("/api/review/browse")
    async def browse_boxes(status: str = "any", species: str | None = None,
                           camera: str | None = None, who: str = "anyone", lane: str = "any",
                           date_from: date | None = None, date_to: date | None = None,
                           page: int = Query(0, ge=0, le=100000), user=Depends(reviewer)):
        filters = BrowseFilters(status=status, species=species or None, camera=camera or None,
                                who=who, lane=lane, date_from=date_from, date_to=date_to)
        async with _db().connection() as conn:
            try:
                return JSONResponse(await browse(conn, catalogue=_catalogue(), user=user,
                                                 filters=filters, page=page))
            except ReviewError as e:
                return _fail(e)

    @router.get("/api/review/disputes")
    async def disputes(user=Depends(admin_only)):
        async with _db().connection() as conn:
            return JSONResponse({"disputes": await list_disputes(conn, catalogue=_catalogue())})

    @router.post("/api/review/disputes/{event_id}/adjudicate",
                 dependencies=[Depends(require_csrf)])
    async def adjudicate(event_id: int, body: AdjudicateIn, user=Depends(admin_only)):
        verdict = VerdictIn(event_id=event_id, outcome=body.outcome, label=body.label,
                            life_stage=body.life_stage, bbox=body.bbox)
        async with _db().connection() as conn:
            try:
                return JSONResponse(await submit_verdict(
                    conn, user=user, body=verdict, catalogue=_catalogue(), spot_rate=0.0,
                    rng=rng, adjudication=True))
            except ReviewError as e:
                return _fail(e)

    @router.get("/api/review/agreement")
    async def agreement_stats(user=Depends(admin_only)):
        async with _db().connection() as conn:
            return JSONResponse(await agreement(conn))

    @router.get("/api/review/status")
    async def status(user=Depends(admin_only)):
        async with _db().connection() as conn:
            return JSONResponse(await pool_status(conn, pool_obj=_pool()))

    @router.get("/api/review/settings/window")
    async def window_get(user=Depends(admin_only)):
        async with _db().connection() as conn:
            return JSONResponse(await get_window(conn))

    @router.put("/api/review/settings/window", dependencies=[Depends(require_csrf)])
    async def window_put(body: WindowIn, user=Depends(admin_only)):
        async with _db().connection() as conn:
            try:
                return JSONResponse(await set_window(conn, admin_id=user.id, start=body.start,
                                                     end=body.end))
            except ReviewError as e:
                return _fail(e)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn timeout 900 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest tests/test_review_browse.py tests/test_review_routes.py tests/test_review_frames.py -q`
Expected: all pass.

- [ ] **Step 6: Lint and commit**

```bash
/raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/ruff check services/dashboard/app/review_browse.py services/dashboard/app/routes_review.py tests/test_review_browse.py
git add services/dashboard/app/review_browse.py services/dashboard/app/routes_review.py tests/test_review_browse.py
git commit -m "review: browse, progress, disputes, agreement and the date window

Volunteers can see what is verified overall and in their session, and
what each species still needs. Administrators settle disputes, read
agreement split by how labels were chosen, and narrow the pool to a
date range whose end day is inclusive in local time. A box waiting for
a second opinion never shows its answer in browse either.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: The page: shell, shared plumbing, single-box view, and a demo to try it on

**Files:**
- Modify (replace entirely): `services/dashboard/app/templates/review.html`
- Create: `services/dashboard/app/static/review.css`, `services/dashboard/app/static/review-core.js`, `services/dashboard/app/static/review-single.js`
- Modify: `services/dashboard/app/main.py` (`_asset_version` file list)
- Create: `scripts/dev/seed_review_demo.py`
- Test: `tests/test_review_page.py`

**Interfaces:**
- Consumes: every `/api/review/*` endpoint from Tasks 7–9 and the payload shape in Task 7; `renderIdentityChip` from `static/auth.js` (fills `#identity-chip`).
- Produces (global `RV`, used by Tasks 11 and 12): `RV.role`, `RV.lane`, `RV.view`, `RV.catalogue`, `RV.views` (each view is `{enter(arg)}`), `RV.heldItemId`, `RV.mine`; helpers `RV.el(tag, attrs, ...children)`, `RV.svg(tag, attrs)`, `RV.api/get/post/patch/del/put`, `RV.banner(msg)`, `RV.toast(msg)`, `RV.pct(p)`, `RV.loadCatalogue()`, `RV.entry(name, lifeStage)`, `RV.search(query, limit)`, class `RV.SpeciesPicker(container, onPick)` (`.focus()`, `.input`, `.update()`), `RV.clamp`, `RV.handlesOf(box)`, `RV.dragBox(start, mode, dx, dy, frameW, frameH)`, class `RV.BoxEditor(container, {imageUrl, region, box, frameW, frameH, others, editable})` (`.box`, `.moved`, `.reset()`), `RV.onKey(view, handler)`, `RV.show(view, arg)`, `RV.refreshMine()`. `RV.views.single.renderRecent()`.
- DOM ids: `#rv-nav` (buttons with `data-lane` or `data-view`), `#rv-session-count`, `#identity-chip`, `#rv-banner`, `#rv-toast`, `#rv-single`, `#rv-frame`, `#rv-browse`, `#rv-recent`.

- [ ] **Step 1: Write the failing page tests**

```python
# tests/test_review_page.py
"""The review page's shell and scripts. There is no browser-test harness
in this repo, so the JavaScript gets a syntax check here and a manual
checklist in Task 12."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from review_fixtures import login, make_user

STATIC = Path(__file__).resolve().parents[1] / "services" / "dashboard" / "app" / "static"
NODE = shutil.which("node") or "/raid/scratch/dzimmerman2021/miniconda3/envs/node/bin/node"
SCRIPTS = ["review-core.js", "review-single.js"]        # Tasks 11 and 12 add theirs


@pytest.mark.skipif(not Path(NODE).exists(), reason="node is not available")
@pytest.mark.parametrize("name", SCRIPTS)
def test_script_parses(name):
    r = subprocess.run([NODE, "--check", str(STATIC / name)], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0, r.stderr


def test_scripts_never_write_markup_from_data():
    for name in SCRIPTS:
        assert "innerHTML" not in (STATIC / name).read_text(), name


@pytest.mark.anyio
async def test_the_page_carries_the_role_and_loads_the_scripts(rdb, rclient):
    await make_user(rdb, "boss", role="admin")
    login(rclient, "boss")
    html = rclient.get("/review", headers={"Accept": "text/html"}).text
    assert 'data-role="admin"' in html and 'id="rv-single"' in html
    assert "/static/review.css?v=" in html
    for name in SCRIPTS:
        assert f"/static/{name}?v=" in html
```

- [ ] **Step 2: Run them to verify they fail**

Run: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn timeout 900 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest tests/test_review_page.py -q`
Expected: FAIL. `node --check` cannot find the scripts, and the placeholder page has no `data-role`.

- [ ] **Step 3: Replace `templates/review.html`**

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Review — Wahoo Bay</title>
  <link rel="stylesheet" href="/static/style.css?v={{ asset_version }}">
  <link rel="stylesheet" href="/static/review.css?v={{ asset_version }}">
</head>
<body class="rv-page" data-role="{{ role }}" data-display-name="{{ display_name }}">
  <header class="rv-top">
    <span class="rv-brand">Wahoo Bay · Review</span>
    <nav id="rv-nav" class="rv-tabs" aria-label="Review views">
      <button type="button" data-lane="check">Check the model's guesses</button>
      <button type="button" data-lane="identify">Identify new fish</button>
      <button type="button" data-view="browse">Browse</button>
    </nav>
    <span class="rv-spacer"></span>
    <span class="rv-meta">This session: <b id="rv-session-count">0</b> answered</span>
    <span id="identity-chip" class="rv-meta"></span>
  </header>
  <div id="rv-banner" class="rv-banner" role="alert" hidden></div>
  <div id="rv-toast" class="rv-toast" role="status" aria-live="polite" hidden></div>
  <main class="rv-main">
    <section id="rv-single" aria-label="Review one box" hidden></section>
    <section id="rv-frame" aria-label="Whole frame" hidden></section>
    <section id="rv-browse" aria-label="Browse" hidden></section>
  </main>
  <footer id="rv-recent" class="rv-recent" aria-label="Your recent boxes"></footer>
  <script src="/static/auth.js?v={{ asset_version }}"></script>
  <script src="/static/review-core.js?v={{ asset_version }}"></script>
  <script src="/static/review-single.js?v={{ asset_version }}"></script>
</body>
</html>
```

- [ ] **Step 4: Write `static/review.css`**

```css
/* services/dashboard/app/static/review.css
   The review page (spec 2026-09-24 §9, layout B). Colours come from the
   dashboard's variables in style.css. */
.rv-page { min-height: 100vh; }
.rv-top { display: flex; align-items: center; gap: 14px; padding: 8px 14px; background: var(--panel);
  border-bottom: 1px solid #1f3b4f; flex-wrap: wrap; position: sticky; top: 0; z-index: 5; }
.rv-brand { font-weight: 600; }
.rv-tabs { display: flex; gap: 4px; flex-wrap: wrap; }
.rv-tabs button { padding: 5px 10px; border-radius: 6px; border: 1px solid #1f3b4f; background: var(--bg);
  color: var(--muted); cursor: pointer; }
.rv-tabs button.on { background: var(--accent); border-color: var(--accent); color: #062033; font-weight: 600; }
.rv-spacer { flex: 1; }
.rv-meta { color: var(--muted); font-size: 13px; }
.rv-main { padding: 12px 14px 96px; }
.rv-banner { background: #5a2a14; color: #ffe2cf; padding: 8px 14px; }
.rv-toast { position: fixed; bottom: 84px; left: 50%; transform: translateX(-50%); background: var(--panel);
  border: 1px solid var(--accent); padding: 8px 14px; border-radius: 8px; z-index: 10; }
.rv-empty { padding: 40px 0; text-align: center; }
.rv-muted { color: var(--muted); }
.rv-label { font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); }
.rv-layout { display: grid; grid-template-columns: minmax(0, 1.45fr) minmax(300px, 1fr); gap: 16px;
  align-items: start; }
@media (max-width: 900px) { .rv-layout { grid-template-columns: 1fr; } }
.rv-editor { position: relative; line-height: 0; background: #000; border-radius: 6px; overflow: hidden;
  touch-action: none; }
.rv-editor img { width: 100%; display: block; user-select: none; -webkit-user-drag: none; }
.rv-editor-svg { position: absolute; inset: 0; width: 100%; height: 100%; }
.rv-box { fill: rgba(255, 196, 0, 0.06); stroke: #ffc400; stroke-width: 2px;
  vector-effect: non-scaling-stroke; cursor: move; }
.rv-other { fill: none; stroke: rgba(220, 220, 220, 0.7); stroke-width: 1px;
  vector-effect: non-scaling-stroke; pointer-events: none; }
.rv-handle { fill: #ffc400; stroke: #000; stroke-width: 1px; vector-effect: non-scaling-stroke;
  cursor: grab; }
.rv-cap { color: var(--muted); font-size: 12px; margin: 6px 0 0; }
.rv-side { display: flex; flex-direction: column; gap: 10px; }
.rv-thumb { position: relative; line-height: 0; border-radius: 6px; overflow: hidden; }
.rv-thumb img { width: 100%; display: block; }
.rv-thumb-svg { position: absolute; inset: 0; width: 100%; height: 100%; }
.rv-progress { display: flex; align-items: center; gap: 8px; font-size: 13px; }
.rv-bar { flex: 1; height: 7px; background: #1f3b4f; border-radius: 4px; overflow: hidden; display: flex; }
.rv-bar i { display: block; height: 100%; background: var(--warn); }
.rv-guess { display: flex; gap: 10px; align-items: center; padding: 8px; border: 1px solid #1f3b4f;
  border-radius: 8px; flex-wrap: wrap; }
.rv-guess img { width: 72px; height: 54px; object-fit: cover; border-radius: 4px; }
.rv-guess-text { flex: 1; min-width: 140px; }
.rv-common { font-weight: 600; }
.rv-latin { font-style: italic; color: var(--muted); font-size: 12px; }
.rv-credit { font-size: 10px; color: var(--muted); }
.rv-side button, .rv-empty button, .rv-frame-top button { padding: 7px 10px; border-radius: 7px;
  border: 1px solid #1f3b4f; background: var(--bg); color: var(--ink); cursor: pointer; }
.rv-side button:hover { border-color: var(--accent); }
.rv-side button.rv-primary { background: var(--good); color: #062a14; border: none; font-weight: 700; }
.rv-side button:disabled { opacity: .5; cursor: default; }
.rv-search { width: 100%; padding: 8px 10px; border-radius: 7px; border: 1px solid #1f3b4f;
  background: var(--bg); color: var(--ink); }
.rv-results { list-style: none; margin: 4px 0 0; padding: 0; max-height: 320px; overflow-y: auto; }
.rv-result { display: flex; gap: 8px; align-items: center; padding: 5px 6px; border-radius: 6px; cursor: pointer; }
.rv-result.active, .rv-result:hover { background: #1f3b4f; }
.rv-result img, .rv-nophoto { width: 40px; height: 30px; object-fit: cover; border-radius: 3px;
  background: #1f3b4f; flex: none; }
.rv-result-text { display: flex; flex-direction: column; }
.rv-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; }
.rv-side button.rv-chip { border-radius: 999px; border-style: dashed; font-size: 12px; }
.rv-row { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
.rv-row > button { flex: 1; }
.rv-small { font-size: 12px; }
.rv-small > button { flex: 0 0 auto; }
.rv-side button.rv-link, .rv-frame-top button.rv-link { background: none; border: none; color: var(--accent);
  padding: 2px 4px; }
.rv-check { display: inline-flex; gap: 4px; align-items: center; }
.rv-notes { width: 100%; background: var(--bg); color: var(--ink); border: 1px solid #1f3b4f; border-radius: 6px; }
.rv-note { background: #173049; border-left: 3px solid var(--accent); padding: 6px 10px; border-radius: 4px;
  margin: 0; font-size: 13px; }
.rv-celeb { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
.rv-page kbd { font-family: ui-monospace, monospace; font-size: 10px; padding: 0 4px;
  border: 1px solid currentColor; border-radius: 3px; opacity: .75; }
.rv-recent { position: fixed; bottom: 0; left: 0; right: 0; display: flex; gap: 8px; align-items: center;
  padding: 8px 14px; background: var(--panel); border-top: 1px solid #1f3b4f; overflow-x: auto; }
.rv-recent-item { display: flex; flex-direction: column; align-items: center; gap: 2px; background: none;
  border: 1px solid #1f3b4f; border-radius: 6px; padding: 3px; color: var(--ink); font-size: 10px;
  cursor: pointer; flex: none; }
.rv-recent-item img { width: 64px; height: 40px; object-fit: cover; border-radius: 3px; }
```

- [ ] **Step 5: Write `static/review-core.js`**

```js
// services/dashboard/app/static/review-core.js
// Shared plumbing for the review page (spec 2026-09-24 §9): API calls, the
// species catalogue and its search box, the drag-to-fix box editor,
// keyboard shortcuts and view switching. The views live in
// review-single.js, review-frame.js and review-browse.js and register
// themselves on RV.views. Nothing here writes data into innerHTML: every
// label comes from the database or the Species Log.
"use strict";

const RV = {
  role: document.body.dataset.role || "reviewer",
  view: null,
  lane: "check",
  catalogue: [],
  byKey: new Map(),
  views: {},
  keyHandlers: {},
  heldItemId: null,
  mine: { session_count: 0, recent: [] },
  HEADERS: { "Content-Type": "application/json", "X-Requested-With": "wahoobay" },
};

RV.el = function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2), value);
    } else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
};

const SVG_NS = "http://www.w3.org/2000/svg";
RV.svg = function svg(tag, attrs) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, String(value));
  return node;
};

// ---- server calls -----------------------------------------------------------

RV.api = async function api(method, url, body) {
  const options = { method, credentials: "same-origin", headers: RV.HEADERS };
  if (body !== undefined) options.body = JSON.stringify(body);
  let response;
  try {
    response = await fetch(url, options);
  } catch (err) {
    RV.banner("Can't reach the server. Check your connection; nothing was saved.");
    return { ok: false, status: 0, data: { error: "network error" } };
  }
  if (response.status === 401) {
    window.location.href = "/login?next=" + encodeURIComponent("/review");
    return { ok: false, status: 401, data: { error: "signed out" } };
  }
  let data = {};
  try { data = await response.json(); } catch (err) { data = {}; }
  if (response.status === 503) {
    RV.banner("The database is unavailable right now. Nothing was saved; try again in a minute.");
  } else if (response.ok) {
    RV.banner(null);
  }
  return { ok: response.ok, status: response.status, data: data || {} };
};
RV.get = (url) => RV.api("GET", url);
RV.post = (url, body) => RV.api("POST", url, body || {});
RV.patch = (url, body) => RV.api("PATCH", url, body || {});
RV.put = (url, body) => RV.api("PUT", url, body || {});
RV.del = (url) => RV.api("DELETE", url);

RV.banner = function banner(message) {
  const b = document.getElementById("rv-banner");
  b.textContent = message || "";
  b.hidden = !message;
};

RV.toast = function toast(message) {
  const t = document.getElementById("rv-toast");
  t.textContent = message;
  t.hidden = false;
  clearTimeout(RV.toastTimer);
  RV.toastTimer = setTimeout(() => { t.hidden = true; }, 4000);
};

RV.pct = (p) => (p === null || p === undefined ? "" : `${Math.round(p * 100)}%`);

// ---- the species catalogue ------------------------------------------------

RV.loadCatalogue = async function loadCatalogue() {
  const r = await RV.get("/api/review/species");
  if (!r.ok) return false;
  RV.catalogue = r.data.species || [];
  RV.byKey = new Map(RV.catalogue.map((e) => [e.key, e]));
  return true;
};

RV.entry = function entry(name, lifeStage) {
  return RV.byKey.get(`${name}|${lifeStage || ""}`)
    || RV.catalogue.find((e) => e.name === name) || null;
};

RV.search = function search(query, limit) {
  const q = query.trim().toLowerCase();
  if (q.length < 2) return [];
  const scored = [];
  for (const e of RV.catalogue) {
    const common = (e.common || "").toLowerCase();
    const name = e.name.toLowerCase();
    let score = -1;
    if (common.startsWith(q) || name.startsWith(q)) score = 0;
    else if (`${common} ${name}`.split(/\s+/).some((w) => w.startsWith(q))) score = 1;
    else if (common.includes(q) || name.includes(q)) score = 2;
    if (score >= 0) scored.push([score, e]);
  }
  scored.sort((a, b) => a[0] - b[0]
    || (a[1].common || a[1].name).localeCompare(b[1].common || b[1].name));
  return scored.slice(0, limit || 8).map((s) => s[1]);
};

RV.SpeciesPicker = class SpeciesPicker {
  constructor(container, onPick) {
    this.onPick = onPick;
    this.results = [];
    this.active = -1;
    this.input = RV.el("input", {
      type: "search", class: "rv-search", autocomplete: "off",
      placeholder: "Search common or Latin name…  ( / )", "aria-label": "Search species",
    });
    this.list = RV.el("ul", { class: "rv-results", role: "listbox" });
    this.input.addEventListener("input", () => this.update());
    this.input.addEventListener("keydown", (ev) => this.onKey(ev));
    container.append(this.input, this.list);
  }

  focus() { this.input.focus(); this.input.select(); }

  clear() { this.input.value = ""; this.update(); }

  update() {
    this.results = RV.search(this.input.value, 8);
    this.active = this.results.length ? 0 : -1;
    this.render();
  }

  render() {
    this.list.replaceChildren(...this.results.map((e, i) => RV.el("li", {
      class: "rv-result" + (i === this.active ? " active" : ""), role: "option",
      onmousedown: (ev) => { ev.preventDefault(); this.pick(e); },
    },
    e.photo_url ? RV.el("img", { src: e.photo_url, alt: "", loading: "lazy" })
      : RV.el("span", { class: "rv-nophoto", "aria-hidden": "true" }),
    RV.el("span", { class: "rv-result-text" },
      RV.el("span", { class: "rv-common", text: e.common || e.name }),
      RV.el("span", { class: "rv-latin", text: e.name + (e.life_stage ? ` · ${e.life_stage}` : "") })))));
  }

  onKey(ev) {
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      if (!this.results.length) return;
      const step = ev.key === "ArrowDown" ? 1 : -1;
      this.active = (this.active + step + this.results.length) % this.results.length;
      this.render();
    } else if (ev.key === "Enter") {
      ev.preventDefault();
      if (this.active >= 0) this.pick(this.results[this.active]);
    } else if (ev.key === "Escape") {
      this.clear();
      this.input.blur();
    }
  }

  pick(entry) { this.clear(); this.input.blur(); this.onPick(entry); }
};

// ---- the box editor ---------------------------------------------------------

RV.clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);

RV.handlesOf = function handlesOf(box) {
  const [x1, y1, x2, y2] = box;
  const mx = (x1 + x2) / 2;
  const my = (y1 + y2) / 2;
  return [[x1, y1, "nw"], [mx, y1, "n"], [x2, y1, "ne"], [x2, my, "e"],
    [x2, y2, "se"], [mx, y2, "s"], [x1, y2, "sw"], [x1, my, "w"]];
};

// Apply a drag of (dx, dy) in `mode` ("move" or a handle name such as
// "nw") to the box `start`, keeping it inside the frame and at least 4 px
// wide and tall. All values are frame pixels.
RV.dragBox = function dragBox(start, mode, dx, dy, frameW, frameH) {
  let [x1, y1, x2, y2] = start;
  if (mode === "move") {
    const w = x2 - x1;
    const h = y2 - y1;
    x1 = RV.clamp(x1 + dx, 0, frameW - w);
    y1 = RV.clamp(y1 + dy, 0, frameH - h);
    x2 = x1 + w;
    y2 = y1 + h;
  } else {
    if (mode.includes("w")) x1 = RV.clamp(x1 + dx, 0, x2 - 4);
    if (mode.includes("e")) x2 = RV.clamp(x2 + dx, x1 + 4, frameW);
    if (mode.includes("n")) y1 = RV.clamp(y1 + dy, 0, y2 - 4);
    if (mode.includes("s")) y2 = RV.clamp(y2 + dy, y1 + 4, frameH);
  }
  return [Math.round(x1), Math.round(y1), Math.round(x2), Math.round(y2)];
};

// A draggable box over an image showing `region` ([x0, y0, x1, y1] in
// frame pixels). The SVG's viewBox is the region itself, so the box is
// stored and drawn in frame pixels; only the pointer is converted.
RV.BoxEditor = class BoxEditor {
  constructor(container, { imageUrl, region, box, frameW, frameH, others, editable }) {
    this.region = region;
    this.original = box.slice();
    this.box = box.slice();
    this.frameW = frameW;
    this.frameH = frameH;
    this.others = others || [];
    this.editable = editable !== false;
    this.drag = null;
    const [x0, y0, x1, y1] = region;
    this.svgEl = RV.svg("svg", {
      class: "rv-editor-svg", viewBox: `${x0} ${y0} ${x1 - x0} ${y1 - y0}`,
      preserveAspectRatio: "none",
    });
    const wrap = RV.el("div", { class: "rv-editor" },
      RV.el("img", { src: imageUrl, alt: "The detected animal, zoomed", draggable: "false" }));
    wrap.append(this.svgEl);
    container.replaceChildren(wrap);
    this.svgEl.addEventListener("pointerdown", (ev) => this.onDown(ev));
    this.svgEl.addEventListener("pointermove", (ev) => this.onMove(ev));
    this.svgEl.addEventListener("pointerup", (ev) => this.onUp(ev));
    this.svgEl.addEventListener("pointercancel", (ev) => this.onUp(ev));
    this.draw();
  }

  get moved() { return this.box.some((v, i) => v !== this.original[i]); }

  reset() { this.box = this.original.slice(); this.draw(); }

  handleSize() { return (this.region[2] - this.region[0]) / 40; }

  toFrame(ev) {
    const r = this.svgEl.getBoundingClientRect();
    const [x0, y0, x1, y1] = this.region;
    return [x0 + ((ev.clientX - r.left) / r.width) * (x1 - x0),
      y0 + ((ev.clientY - r.top) / r.height) * (y1 - y0)];
  }

  draw() {
    const [x1, y1, x2, y2] = this.box;
    const nodes = this.others.map((o) => RV.svg("rect", {
      x: o[0], y: o[1], width: o[2] - o[0], height: o[3] - o[1], class: "rv-other" }));
    nodes.push(RV.svg("rect", { x: x1, y: y1, width: x2 - x1, height: y2 - y1, class: "rv-box" }));
    if (this.editable) {
      const s = this.handleSize();
      for (const [hx, hy] of RV.handlesOf(this.box)) {
        nodes.push(RV.svg("rect", { x: hx - s / 2, y: hy - s / 2, width: s, height: s, class: "rv-handle" }));
      }
    }
    this.svgEl.replaceChildren(...nodes);
  }

  onDown(ev) {
    if (!this.editable) return;
    const [px, py] = this.toFrame(ev);
    const s = this.handleSize();
    let mode = null;
    for (const [hx, hy, name] of RV.handlesOf(this.box)) {
      if (Math.abs(px - hx) <= s && Math.abs(py - hy) <= s) { mode = name; break; }
    }
    const [x1, y1, x2, y2] = this.box;
    if (!mode && px > x1 && px < x2 && py > y1 && py < y2) mode = "move";
    if (!mode) return;
    ev.preventDefault();
    this.svgEl.setPointerCapture(ev.pointerId);
    this.drag = { mode, start: [px, py], box: this.box.slice() };
  }

  onMove(ev) {
    if (!this.drag) return;
    const [px, py] = this.toFrame(ev);
    this.box = RV.dragBox(this.drag.box, this.drag.mode, px - this.drag.start[0],
      py - this.drag.start[1], this.frameW, this.frameH);
    this.draw();
  }

  onUp(ev) {
    if (!this.drag) return;
    this.drag = null;
    try { this.svgEl.releasePointerCapture(ev.pointerId); } catch (err) { /* already released */ }
  }
};

// ---- keys, views, the session ---------------------------------------------

RV.onKey = function onKey(view, handler) { RV.keyHandlers[view] = handler; };

document.addEventListener("keydown", (ev) => {
  const t = ev.target;
  const typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA"
    || t.tagName === "SELECT" || t.isContentEditable);
  if (typing || ev.ctrlKey || ev.metaKey || ev.altKey) return;
  const handler = RV.keyHandlers[RV.view];
  if (handler && handler(ev.key, ev) === true) ev.preventDefault();
});

RV.releaseHeld = function releaseHeld() {
  if (!RV.heldItemId) return;
  RV.post(`/api/review/items/${RV.heldItemId}/release`);
  RV.heldItemId = null;
};

RV.show = async function show(view, arg) {
  if (!RV.views[view]) return;
  for (const id of ["single", "frame", "browse"]) {
    const section = document.getElementById("rv-" + id);
    if (section) section.hidden = id !== view;
  }
  RV.view = view;
  for (const b of document.querySelectorAll("#rv-nav button")) {
    const on = b.dataset.view ? b.dataset.view === view
      : view === "single" && b.dataset.lane === RV.lane;
    b.classList.toggle("on", on);
    b.setAttribute("aria-pressed", on ? "true" : "false");
  }
  document.getElementById("rv-recent").hidden = view !== "single";
  await RV.views[view].enter(arg || {});
};

RV.refreshMine = async function refreshMine() {
  const r = await RV.get("/api/review/mine/recent");
  if (!r.ok) return;
  RV.mine = r.data;
  document.getElementById("rv-session-count").textContent = String(r.data.session_count || 0);
  if (RV.views.single && RV.views.single.renderRecent) RV.views.single.renderRecent();
};

window.addEventListener("pagehide", () => {
  if (!RV.heldItemId) return;
  fetch(`/api/review/items/${RV.heldItemId}/release`, {
    method: "POST", keepalive: true, credentials: "same-origin", headers: RV.HEADERS, body: "{}",
  });
});

document.addEventListener("DOMContentLoaded", async () => {
  for (const b of document.querySelectorAll("#rv-nav button")) {
    const view = b.dataset.view || "single";
    b.hidden = !RV.views[view];
    b.addEventListener("click", () => {
      RV.releaseHeld();
      if (b.dataset.lane) RV.lane = b.dataset.lane;
      RV.show(view);
    });
  }
  if (!(await RV.loadCatalogue())) {
    RV.banner("Couldn't load the species list. Reload the page to try again.");
    return;
  }
  await RV.refreshMine();
  RV.show("single");
});
```

- [ ] **Step 6: Write `static/review-single.js`**

```js
// services/dashboard/app/static/review-single.js
// The single-box view (layout B): the zoomed box is the work area; the
// side panel holds the frame, the model's guess, the species search, the
// hints and the answer buttons. Also owns the recent-boxes strip.
"use strict";

RV.views.single = (() => {
  const root = () => document.getElementById("rv-single");
  let current = null;       // the payload being answered
  let editor = null;
  let picker = null;
  let reopened = false;
  let notSure = false;
  let notes = "";
  let hintName = null;      // set while choosing a life stage for a hint
  let busy = false;

  const kbd = (key) => RV.el("kbd", { text: key });

  async function enter(arg) {
    if (arg.reopenEventId) return reopen(arg.reopenEventId);
    return loadNext();
  }

  function message(text) {
    root().replaceChildren(RV.el("p", { class: "rv-empty", text }));
  }

  async function loadNext() {
    reopened = false;
    message("Loading the next box…");
    const r = await RV.get(`/api/review/next?lane=${encodeURIComponent(RV.lane)}`);
    if (!r.ok) { current = null; message(r.data.error || "Couldn't load a box."); return; }
    if (!r.data.item) { current = null; RV.heldItemId = null; renderEmpty(r.data.pool); return; }
    current = r.data;
    RV.heldItemId = current.item.id;
    render();
  }

  async function reopen(eventId) {
    const r = await RV.get(`/api/review/boxes/${eventId}`);
    if (!r.ok) { RV.toast(r.data.error || "Couldn't reopen that box."); return loadNext(); }
    RV.releaseHeld();
    reopened = true;
    current = r.data;
    render();
  }

  function renderEmpty(pool) {
    const when = pool && pool.finished_at ? new Date(pool.finished_at).toLocaleTimeString() : null;
    root().replaceChildren(RV.el("div", { class: "rv-empty" },
      RV.el("p", { text: "No boxes are ready in this lane right now." }),
      RV.el("p", { class: "rv-muted", text: when
        ? `The pool was last topped up at ${when}; it refills every few minutes.`
        : "The pool is filling; check back in a few minutes." }),
      RV.el("button", { type: "button", onclick: () => loadNext() }, "Try again")));
  }

  function frameThumb(c) {
    const svg = RV.svg("svg", { class: "rv-thumb-svg", viewBox: `0 0 ${c.frame.width} ${c.frame.height}`,
      preserveAspectRatio: "none" });
    for (const o of c.other_boxes) {
      svg.append(RV.svg("rect", { x: o.bbox[0], y: o.bbox[1], width: o.bbox[2] - o.bbox[0],
        height: o.bbox[3] - o.bbox[1], class: "rv-other" }));
    }
    const b = c.box.model_bbox;
    svg.append(RV.svg("rect", { x: b[0], y: b[1], width: b[2] - b[0], height: b[3] - b[1], class: "rv-box" }));
    const wrap = RV.el("div", { class: "rv-thumb" },
      RV.el("img", { src: `${c.frame.url}?size=640`, alt: "The whole frame" }));
    wrap.append(svg);
    return wrap;
  }

  function progressBar(c) {
    if (!c.counts_toward) return null;
    const t = c.counts_toward;
    const fill = Math.min(100, Math.round((100 * t.count) / t.target));
    return RV.el("div", { class: "rv-progress" },
      RV.el("span", { class: "rv-label", text: "Counts toward" }),
      RV.el("span", { text: t.common || t.name }),
      RV.el("span", { class: "rv-bar", "aria-hidden": "true" }, RV.el("i", { style: `width:${fill}%` })),
      RV.el("span", { class: "rv-muted", text: `${t.count} / ${t.target}` }));
  }

  function guessCard(c) {
    if (!c.model) return null;
    const e = RV.entry(c.model.name, null);
    return RV.el("div", { class: "rv-guess" },
      e && e.photo_url ? RV.el("img", { src: e.photo_url, alt: "" }) : null,
      RV.el("div", { class: "rv-guess-text" },
        RV.el("div", { class: "rv-common", text: c.model.common || c.model.name }),
        RV.el("div", { class: "rv-latin", text: `${c.model.name} · model ${RV.pct(c.model.p)}` }),
        e && e.photo_credit ? RV.el("div", { class: "rv-credit", text: `Photo ${e.photo_credit}` }) : null),
      RV.el("button", { type: "button", class: "rv-primary", onclick: () => submit("accept", {}) },
        "✓ Looks right ", kbd("A")));
  }

  function chooseHint(h) {
    const entries = RV.catalogue.filter((e) => e.name === h.name);
    if (!entries.length) { RV.toast("That species isn't in the list any more."); return; }
    if (entries.length === 1) { pickSpecies(entries[0], "hint"); return; }
    // Several life stages: let the volunteer pick one, and remember the
    // choice came from a hint.
    hintName = h.name;
    picker.input.value = h.name;
    picker.update();
    picker.focus();
  }

  function hintChips(c) {
    if (!c.hints || !c.hints.length) return null;
    const title = c.model ? "The model's other guesses" : "The model's weak guesses (under 55%, often wrong)";
    return RV.el("div", { class: "rv-hints" },
      RV.el("div", { class: "rv-label", text: title }),
      RV.el("div", { class: "rv-chips" }, c.hints.map((h, i) => RV.el("button", {
        type: "button", class: "rv-chip", onclick: () => chooseHint(h),
      }, `${h.common || h.name} ${RV.pct(h.p)} `, kbd(String(i + 1))))));
  }

  function render() {
    const c = current;
    notSure = false;
    notes = "";
    hintName = null;
    const work = RV.el("div", { class: "rv-work" });
    editor = new RV.BoxEditor(work, {
      imageUrl: c.zoom.url, region: c.zoom.region, box: c.box.bbox,
      frameW: c.frame.width, frameH: c.frame.height, others: c.other_boxes.map((o) => o.bbox),
    });
    const side = RV.el("aside", { class: "rv-side" });
    if (reopened) {
      side.append(RV.el("p", { class: "rv-note", text: "Changing your answer: pick again and it replaces your last one." }));
    }
    if (c.item.pass === 2) {
      side.append(RV.el("p", { class: "rv-note", text: "Second opinion: answer from the picture alone." }));
    }
    side.append(frameThumb(c));
    for (const part of [progressBar(c), guessCard(c)]) if (part) side.append(part);
    const pickerSlot = RV.el("div", { class: "rv-picker" });
    side.append(RV.el("div", { class: "rv-label", text: c.model ? "Or pick the species" : "Pick the species" }),
      pickerSlot);
    picker = new RV.SpeciesPicker(pickerSlot, (entry) => {
      const source = hintName && entry.name === hintName ? "hint" : "search";
      hintName = null;
      pickSpecies(entry, source);
    });
    const chips = hintChips(c);
    if (chips) side.append(chips);
    side.append(RV.el("div", { class: "rv-row" },
      RV.el("button", { type: "button", onclick: () => submit("unidentifiable", {}) }, "Can't identify ", kbd("C")),
      RV.el("button", { type: "button", onclick: () => submit("no_animal", {}) }, "No animal here ", kbd("N"))));
    const noteBox = RV.el("textarea", { class: "rv-notes", rows: "2", placeholder: "Note (optional)", hidden: true });
    noteBox.addEventListener("input", () => { notes = noteBox.value; });
    side.append(RV.el("div", { class: "rv-row rv-small" },
      RV.el("label", { class: "rv-check" },
        RV.el("input", { type: "checkbox", onchange: (ev) => { notSure = ev.target.checked; } }), " Not sure"),
      RV.el("button", { type: "button", class: "rv-link", onclick: () => {
        noteBox.hidden = !noteBox.hidden;
        if (!noteBox.hidden) noteBox.focus();
      } }, "+ note"),
      RV.el("button", { type: "button", class: "rv-link", onclick: () => skip() }, "Skip ", kbd("S")),
      RV.views.frame
        ? RV.el("button", { type: "button", class: "rv-link", onclick: () => openFrame() }, "Whole frame ", kbd("F"))
        : null),
    noteBox, RV.el("div", { class: "rv-celeb", id: "rv-celeb" }));
    root().replaceChildren(RV.el("div", { class: "rv-layout" },
      RV.el("div", {}, work, RV.el("p", { class: "rv-cap",
        text: "Zoomed with room around the box. Drag an edge or corner to fix it; Esc puts it back." })),
      side));
  }

  function pickSpecies(entry, source) {
    const base = { label: entry.name, life_stage: entry.life_stage, label_source: source };
    const celebs = entry.celebrities || [];
    if (!celebs.length) { submit("species", base); return; }
    const who = entry.common || entry.name;
    document.getElementById("rv-celeb").replaceChildren(
      RV.el("span", { class: "rv-label", text: `Is this a named ${who}?` }),
      ...celebs.map((c) => RV.el("button", { type: "button",
        onclick: () => submit("species", { ...base, celebrity_id: c.id }) }, c.nickname)),
      RV.el("button", { type: "button", onclick: () => submit("species", base) }, `No, just a ${who}`));
  }

  async function submit(outcome, extra) {
    if (!current || busy) return;
    busy = true;
    try {
      const body = { event_id: current.item.event_id, review_item_id: reopened ? null : current.item.id,
        outcome, ...extra };
      if (outcome === "accept" || outcome === "species") body.not_sure = notSure;
      if (editor && editor.moved && outcome !== "no_animal") body.bbox = editor.box;
      if (notes.trim()) body.notes = notes.trim();
      const r = await RV.post("/api/review/verdicts", body);
      if (r.status === 409) {
        RV.toast("Someone else just answered this box. Here's the next one.");
        RV.heldItemId = null;
        await loadNext();
        return;
      }
      if (!r.ok) {
        RV.toast(r.data.error || "That answer wasn't saved.");
        // Review Focus 5: the species list may have changed since the page loaded.
        if (r.data.error === "pick a species from the list") await RV.loadCatalogue();
        return;
      }
      RV.heldItemId = null;
      await RV.refreshMine();
      await loadNext();
    } finally {
      busy = false;
    }
  }

  async function skip() {
    if (!current) return;
    if (!reopened && current.item.id) {
      await RV.post(`/api/review/items/${current.item.id}/skip`);
      RV.heldItemId = null;
    }
    await loadNext();
  }

  function openFrame() {
    if (current && RV.views.frame) RV.show("frame", { savedFrameId: current.box.saved_frame_id });
  }

  function renderRecent() {
    const strip = document.getElementById("rv-recent");
    const items = (RV.mine.recent || []).slice(0, 12);
    if (!items.length) { strip.replaceChildren(); return; }
    const word = (r) => (r.outcome === "species" ? (r.common || r.label)
      : r.outcome === "no_animal" ? "No animal" : "Can't ID");
    strip.replaceChildren(
      RV.el("span", { class: "rv-muted", text: "Your recent boxes:" }),
      ...items.map((r) => RV.el("button", {
        type: "button", class: "rv-recent-item", title: "Change this answer",
        onclick: () => RV.show("single", { reopenEventId: r.event_id }),
      }, RV.el("img", { src: r.zoom_url, alt: "", loading: "lazy" }), RV.el("span", { text: word(r) }))),
      RV.el("span", { class: "rv-muted", text: "click one to change your answer" }));
  }

  RV.onKey("single", (key) => {
    if (!current) return false;
    const k = key.length === 1 ? key.toLowerCase() : key;
    if (k === "a" && current.model) { submit("accept", {}); return true; }
    if (k === "/") { picker.focus(); return true; }
    if (["1", "2", "3"].includes(k) && current.hints && current.hints[Number(k) - 1]) {
      chooseHint(current.hints[Number(k) - 1]);
      return true;
    }
    if (k === "c") { submit("unidentifiable", {}); return true; }
    if (k === "n") { submit("no_animal", {}); return true; }
    if (k === "s") { skip(); return true; }
    if (k === "f" && RV.views.frame) { openFrame(); return true; }
    if (k === "Escape" && editor) { editor.reset(); return true; }
    return false;
  });

  return { enter, loadNext, renderRecent };
})();
```

- [ ] **Step 7: Cache-bust the new assets**

In `main.py`'s `_asset_version`, extend the tuple of paths with:

```python
            BASE_DIR / "static" / "review.css",
            BASE_DIR / "static" / "review-core.js",
            BASE_DIR / "static" / "review-single.js",
            BASE_DIR / "static" / "review-frame.js",
            BASE_DIR / "static" / "review-browse.js",
            BASE_DIR / "templates" / "review.html",
```

(Files that do not exist yet are skipped by the existing `try/except`.)

- [ ] **Step 8: Run the tests to verify they pass**

Run: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn timeout 900 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest tests/test_review_page.py tests/test_review_routes.py -q`
Expected: all pass.

- [ ] **Step 9: Write `scripts/dev/seed_review_demo.py`**

```python
#!/usr/bin/env python
"""Try the review page on real footage without touching production.

Copies a small, evenly spread sample of saved frames and their detections
from the live database (read-only session) into the test database, creates
two demo accounts, and writes their passwords to a mode-600 file. The
frames stay where they are on disk; only rows are copied. The target must
not be the production database.

    python scripts/dev/seed_review_demo.py --frames 300      # seed
    python scripts/dev/seed_review_demo.py --serve           # run a throwaway dashboard

--serve runs the dashboard in this process on 127.0.0.1:18090 against the
test database, with the sheet poller, workers and mail switched off, and
the pool topping up every 60 s. Stop it with Ctrl-C. Running the test
suite truncates the same database, so do not run both at once.
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
    with psycopg.connect(_env_value(source_env, "DATABASE_URL")) as src:
        src.execute("SET default_transaction_read_only = on")
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
```

- [ ] **Step 10: Seed the demo and check the page end to end**

```bash
timeout 600 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python scripts/dev/seed_review_demo.py --frames 300
```

Expected: "sampled ~300 frames with N detections" and the credentials line. Then start the throwaway dashboard in the background (the Bash tool's `run_in_background`, or `nohup ... &`), bounded by `timeout`:

```bash
timeout 7200 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python scripts/dev/seed_review_demo.py --serve > .superpowers/demo.log 2>&1
```

After about 90 seconds, log in with curl and ask for a box:

```bash
grep "review pool:" .superpowers/demo.log | tail -2
/raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python - <<'EOF'
import httpx
user, pw = open(".superpowers/demo_credentials").readline().strip().split(": ")
c = httpx.Client(base_url="http://127.0.0.1:18090", headers={"X-Requested-With": "wahoobay"})
assert c.post("/api/auth/login", json={"username": user, "password": pw}).status_code == 200
for lane in ("check", "identify"):
    r = c.get(f"/api/review/next?lane={lane}").json()
    print(lane, "->", (r.get("item") or {}).get("event_id"), (r.get("model") or {}).get("name"))
print("page:", c.get("/review", headers={"Accept": "text/html"}).status_code)
EOF
```

Expected: a `review pool:` line showing `check_added` and `identify_added` above zero, both lanes returning an event id (the check lane with a species name), and `page: 200`. Leave the demo running for Tasks 11–12; stop it (kill the process) before running the test suite again.

- [ ] **Step 11: Lint and commit**

```bash
/raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/ruff check scripts/dev/seed_review_demo.py services/dashboard/app/main.py tests/test_review_page.py
git add services/dashboard/app/templates/review.html services/dashboard/app/static/review.css services/dashboard/app/static/review-core.js services/dashboard/app/static/review-single.js services/dashboard/app/main.py scripts/dev/seed_review_demo.py tests/test_review_page.py
git commit -m "review: the page, with the fish-first single-box view

Layout B from the mockups: the zoomed box is the work area with drag
handles, and the side panel holds the frame, the model's guess, a
Species Log search with reference photos, hint buttons and one-key
answers. A strip of recent boxes lets volunteers change an answer. A
demo script copies a real sample into the test database and serves the
page on localhost, so it can be tried before it is deployed.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 11: Whole-frame view

**Files:**
- Create: `services/dashboard/app/static/review-frame.js`
- Modify: `services/dashboard/app/static/review.css` (append), `services/dashboard/app/templates/review.html` (script tag), `tests/test_review_page.py` (`SCRIPTS`)

**Interfaces:**
- Consumes: `RV.*` from Task 10; frame endpoints and frame-state shape from Task 8; `/api/review/verdicts` from Task 7.
- Produces: `RV.views.frame = {enter({savedFrameId})}`.

- [ ] **Step 1: Extend the page test**

In `tests/test_review_page.py`, change `SCRIPTS` to `["review-core.js", "review-single.js", "review-frame.js"]`.

Run: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn timeout 900 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest tests/test_review_page.py -q`
Expected: FAIL (the file does not exist; the page lacks the script tag).

- [ ] **Step 2: Write `static/review-frame.js`**

```js
// services/dashboard/app/static/review-frame.js
// Whole-frame mode: the frame is the work area. Every model box is
// numbered and outlined by state; the volunteer answers each one, draws
// boxes around animals the model missed (press D, then drag), and marks
// the frame complete once nothing is left to answer.
"use strict";

RV.views.frame = (() => {
  const root = () => document.getElementById("rv-frame");
  let state = null;
  let selected = null;       // {kind: "model"|"added"|"new", id, bbox, original}
  let drawing = false;
  let drag = null;
  let svgEl = null;
  let picker = null;
  let hintName = null;
  let notSure = false;

  const kbd = (key) => RV.el("kbd", { text: key });
  const inside = (bb, x, y) => x >= bb[0] && x <= bb[2] && y >= bb[1] && y <= bb[3];
  const modelBox = (id) => state.boxes.find((b) => b.event_id === id);
  const addedBox = (id) => state.added.find((a) => a.id === id);
  const STATE_CLASS = { needs_answer: "rv-fb-pending", answered: "rv-fb-done",
    answered_hidden: "rv-fb-done", locked: "rv-fb-locked" };

  function answerText(outcome, label, common) {
    if (outcome === "no_animal") return "no animal";
    if (outcome === "unidentifiable") return "can't identify";
    return common || label || "?";
  }

  async function enter(arg) {
    const id = arg.savedFrameId || (state && state.saved_frame_id);
    if (!id) { RV.show("single"); return; }
    const r = await RV.post(`/api/review/frames/${id}/open`);
    if (!r.ok) { RV.toast(r.data.error || "Couldn't open that frame."); RV.show("single"); return; }
    state = r.data;
    drawing = false;
    selectFirst();
    render();
  }

  async function reload(keepSelection) {
    const r = await RV.get(`/api/review/frames/${state.saved_frame_id}`);
    if (!r.ok) { RV.toast(r.data.error || "Couldn't refresh the frame."); return; }
    state = r.data;
    if (!keepSelection) selectFirst();
    render();
  }

  function selectFirst() {
    const box = state.boxes.find((b) => b.state === "needs_answer");
    selected = box ? { kind: "model", id: box.event_id, bbox: box.bbox.slice(), original: box.bbox.slice() } : null;
  }

  function select(target) {
    selected = target;
    hintName = null;
    notSure = false;
    render();
  }

  function editableSelection() {
    if (!selected || state.complete) return false;
    if (selected.kind === "new") return true;
    if (selected.kind === "model") {
      const b = modelBox(selected.id);
      return Boolean(b && b.state === "needs_answer");
    }
    const a = addedBox(selected.id);
    return Boolean(a && (a.mine || RV.role === "admin"));
  }

  // ---- the frame canvas ------------------------------------------------------

  function drawBoxes() {
    const W = state.frame.width;
    const fontSize = Math.max(14, W / 70);
    const nodes = [];
    const label = (x, y, text) => {
      const t = RV.svg("text", { x: x + 2, y: Math.max(fontSize, y - 4), class: "rv-fb-label", "font-size": fontSize });
      t.textContent = text;
      return t;
    };
    const rect = (bb, cls) => RV.svg("rect", { x: bb[0], y: bb[1], width: bb[2] - bb[0], height: bb[3] - bb[1], class: cls });
    for (const b of state.boxes) {
      const isSel = selected && selected.kind === "model" && selected.id === b.event_id;
      const bb = isSel ? selected.bbox : b.bbox;
      nodes.push(rect(bb, "rv-fb " + (isSel ? "rv-fb-selected" : STATE_CLASS[b.state])));
      const mark = b.state === "locked" ? " · busy" : b.state.startsWith("answered") ? " ✓" : "";
      nodes.push(label(bb[0], bb[1], `${b.n}${mark}`));
    }
    for (const a of state.added) {
      const isSel = selected && selected.kind === "added" && selected.id === a.id;
      const bb = isSel ? selected.bbox : a.bbox;
      nodes.push(rect(bb, "rv-fb rv-fb-added" + (isSel ? " rv-fb-selected" : "")));
      nodes.push(label(bb[0], bb[1], "+ " + answerText(a.outcome, a.label, a.common)));
    }
    if (selected && selected.kind === "new") nodes.push(rect(selected.bbox, "rv-fb rv-fb-added rv-fb-selected"));
    if (editableSelection()) {
      const s = W / 150;
      for (const [hx, hy] of RV.handlesOf(selected.bbox)) {
        nodes.push(RV.svg("rect", { x: hx - s / 2, y: hy - s / 2, width: s, height: s, class: "rv-handle" }));
      }
    }
    svgEl.replaceChildren(...nodes);
  }

  function frameCanvas() {
    svgEl = RV.svg("svg", { class: "rv-editor-svg" + (drawing ? " drawing" : ""),
      viewBox: `0 0 ${state.frame.width} ${state.frame.height}`, preserveAspectRatio: "none" });
    svgEl.addEventListener("pointerdown", onDown);
    svgEl.addEventListener("pointermove", onMove);
    svgEl.addEventListener("pointerup", onUp);
    svgEl.addEventListener("pointercancel", onUp);
    drawBoxes();
    const wrap = RV.el("div", { class: "rv-editor rv-frame-canvas" },
      RV.el("img", { src: `${state.frame.url}?size=1600`, alt: "The whole frame", draggable: "false" }));
    wrap.append(svgEl);
    return wrap;
  }

  function toFrame(ev) {
    const r = svgEl.getBoundingClientRect();
    return [((ev.clientX - r.left) / r.width) * state.frame.width,
      ((ev.clientY - r.top) / r.height) * state.frame.height];
  }

  function hit(px, py) {
    for (const a of [...state.added].reverse()) {
      if (inside(a.bbox, px, py)) return { kind: "added", id: a.id, bbox: a.bbox.slice(), original: a.bbox.slice() };
    }
    for (const b of [...state.boxes].reverse()) {
      if (inside(b.bbox, px, py)) return { kind: "model", id: b.event_id, bbox: b.bbox.slice(), original: b.bbox.slice() };
    }
    return null;
  }

  function startDrag(ev, dragState) {
    ev.preventDefault();
    svgEl.setPointerCapture(ev.pointerId);
    drag = dragState;
  }

  function onDown(ev) {
    const [px, py] = toFrame(ev);
    if (drawing) {
      startDrag(ev, { mode: "draw", start: [px, py] });
      selected = { kind: "new", id: null, bbox: [Math.round(px), Math.round(py), Math.round(px) + 4, Math.round(py) + 4] };
      drawBoxes();
      return;
    }
    if (editableSelection()) {
      const s = state.frame.width / 150;
      for (const [hx, hy, name] of RV.handlesOf(selected.bbox)) {
        if (Math.abs(px - hx) <= s && Math.abs(py - hy) <= s) {
          startDrag(ev, { mode: name, start: [px, py], box: selected.bbox.slice() });
          return;
        }
      }
      if (inside(selected.bbox, px, py)) {
        startDrag(ev, { mode: "move", start: [px, py], box: selected.bbox.slice() });
        return;
      }
    }
    const target = hit(px, py);
    if (target) select(target);
  }

  function onMove(ev) {
    if (!drag) return;
    const [px, py] = toFrame(ev);
    const W = state.frame.width;
    const H = state.frame.height;
    if (drag.mode === "draw") {
      const [sx, sy] = drag.start;
      selected.bbox = [Math.round(RV.clamp(Math.min(sx, px), 0, W)), Math.round(RV.clamp(Math.min(sy, py), 0, H)),
        Math.round(RV.clamp(Math.max(sx, px), 0, W)), Math.round(RV.clamp(Math.max(sy, py), 0, H))];
    } else {
      selected.bbox = RV.dragBox(drag.box, drag.mode, px - drag.start[0], py - drag.start[1], W, H);
    }
    drawBoxes();
  }

  function onUp(ev) {
    if (!drag) return;
    const mode = drag.mode;
    drag = null;
    try { svgEl.releasePointerCapture(ev.pointerId); } catch (err) { /* already released */ }
    if (mode === "draw") {
      drawing = false;
      const [x1, y1, x2, y2] = selected.bbox;
      if (x2 - x1 < 8 || y2 - y1 < 8) {
        selected = null;
        RV.toast("That box was too small; drag a bigger one.");
      }
      render();
    } else if (selected.kind === "added") {
      saveAddedBox(null);          // moving your own drawn box saves it at once
    } else {
      renderPanel();
    }
  }

  // ---- the side panel ----------------------------------------------------------

  function pickerAndButtons(hints, onSpecies, onCant, onNone) {
    const out = [];
    const slot = RV.el("div", { class: "rv-picker" });
    picker = new RV.SpeciesPicker(slot, (entry) => {
      const source = hintName && entry.name === hintName ? "hint" : "search";
      hintName = null;
      onSpecies(entry, source);
    });
    out.push(RV.el("div", { class: "rv-label", text: "Pick the species" }), slot);
    if (hints && hints.length) {
      out.push(RV.el("div", { class: "rv-chips" }, hints.map((h) => RV.el("button", {
        type: "button", class: "rv-chip", onclick: () => {
          const entries = RV.catalogue.filter((e) => e.name === h.name);
          if (entries.length === 1) { onSpecies(entries[0], "hint"); return; }
          hintName = h.name;
          picker.input.value = h.name;
          picker.update();
          picker.focus();
        },
      }, `${h.common || h.name} ${RV.pct(h.p)}`))));
    }
    const row = [RV.el("button", { type: "button", onclick: onCant }, "Can't identify ", kbd("C"))];
    if (onNone) row.push(RV.el("button", { type: "button", onclick: onNone }, "No animal here ", kbd("N")));
    out.push(RV.el("div", { class: "rv-row" }, row));
    out.push(RV.el("label", { class: "rv-check" },
      RV.el("input", { type: "checkbox", checked: notSure, onchange: (ev) => { notSure = ev.target.checked; } }),
      " Not sure"));
    return out;
  }

  function modelPanel(b) {
    const out = [RV.el("div", { class: "rv-label", text: `Box ${b.n} · drawn by the model` }),
      RV.el("div", { class: "rv-thumb" }, RV.el("img", { src: b.zoom.url, alt: `Box ${b.n}, zoomed` }))];
    if (b.state === "answered") {
      out.push(RV.el("p", { text: `Answered: ${answerText(b.outcome, b.label, b.common)}${b.mine ? " (by you)" : ""}.` }));
      if (b.mine) {
        out.push(RV.el("button", { type: "button", onclick: () => RV.show("single", { reopenEventId: b.event_id }) },
          "Change my answer"));
      }
      return out;
    }
    if (b.state === "answered_hidden") {
      out.push(RV.el("p", { text: "Answered. It is waiting for a second opinion, so the answer is hidden." }));
      return out;
    }
    if (b.state === "locked") {
      out.push(RV.el("p", { text: "Another volunteer is answering this box right now." }));
      return out;
    }
    if (b.model) {
      out.push(RV.el("div", { class: "rv-guess" },
        RV.el("div", { class: "rv-guess-text" },
          RV.el("div", { class: "rv-common", text: b.model.common || b.model.name }),
          RV.el("div", { class: "rv-latin", text: `${b.model.name} · model ${RV.pct(b.model.p)}` })),
        RV.el("button", { type: "button", class: "rv-primary", onclick: () => answerModel("accept", {}) },
          "✓ Looks right ", kbd("A"))));
    }
    out.push(...pickerAndButtons(b.hints,
      (entry, source) => answerModel("species", { label: entry.name, life_stage: entry.life_stage, label_source: source }),
      () => answerModel("unidentifiable", {}), () => answerModel("no_animal", {})));
    return out;
  }

  function addedPanel(a) {
    const out = [RV.el("div", { class: "rv-label", text: a ? `A box ${a.mine ? "you" : a.by} drew` : "The box you just drew" })];
    if (a) {
      out.push(RV.el("div", { class: "rv-thumb" }, RV.el("img", { src: a.zoom_url, alt: "The drawn box, zoomed" })),
        RV.el("p", { text: `Answer: ${answerText(a.outcome, a.label, a.common)}${a.not_sure ? " (not sure)" : ""}` }));
    }
    if (state.complete || (a && !a.mine && RV.role !== "admin")) return out;
    out.push(...pickerAndButtons(null,
      (entry) => saveAddedBox({ outcome: "species", label: entry.name, life_stage: entry.life_stage }),
      () => saveAddedBox({ outcome: "unidentifiable" }), null));
    out.push(a
      ? RV.el("button", { type: "button", class: "rv-link", onclick: removeAddedBox }, "Remove this box")
      : RV.el("button", { type: "button", class: "rv-link", onclick: () => { selected = null; render(); } }, "Discard"));
    return out;
  }

  function checklist() {
    const words = { needs_answer: "needs an answer", answered_hidden: "answered (second opinion pending)",
      locked: "another volunteer is on it" };
    const rows = state.boxes.map((b) => RV.el("li", {
      class: "rv-cl " + STATE_CLASS[b.state],
      onclick: () => select({ kind: "model", id: b.event_id, bbox: b.bbox.slice(), original: b.bbox.slice() }),
    }, `${b.n} · ${words[b.state] || answerText(b.outcome, b.label, b.common)}`));
    for (const a of state.added) {
      rows.push(RV.el("li", { class: "rv-cl rv-fb-added",
        onclick: () => select({ kind: "added", id: a.id, bbox: a.bbox.slice(), original: a.bbox.slice() }) },
      `+ drawn by ${a.mine ? "you" : a.by} · ${answerText(a.outcome, a.label, a.common)}`));
    }
    const done = state.boxes.length - state.blocking.length;
    return RV.el("div", {},
      RV.el("div", { class: "rv-label", text: `This frame · ${done} of ${state.boxes.length} model boxes answered` }),
      RV.el("ul", { class: "rv-checklist" }, rows));
  }

  function panelContent() {
    const out = [];
    if (state.complete) out.push(RV.el("p", { class: "rv-note", text: `Marked complete by ${state.complete.by}.` }));
    if (!selected) {
      out.push(RV.el("p", { class: "rv-muted",
        text: "Click a box to answer it, or press D and drag to draw a box around an animal the model missed." }));
    } else if (selected.kind === "model") {
      out.push(...modelPanel(modelBox(selected.id)));
    } else {
      out.push(...addedPanel(selected.kind === "added" ? addedBox(selected.id) : null));
    }
    out.push(checklist());
    const blocked = state.blocking.length > 0;
    out.push(RV.el("button", { type: "button", class: "rv-primary", disabled: Boolean(state.complete) || blocked,
      onclick: complete },
    state.complete ? "Frame complete"
      : blocked ? `Mark frame complete (answer ${state.blocking.join(", ")} first)` : "Mark frame complete"));
    return out;
  }

  function renderPanel() {
    const panel = document.getElementById("rv-frame-panel");
    if (panel) panel.replaceChildren(...panelContent());
  }

  function toggleDraw() {
    if (state.complete) return;
    drawing = !drawing;
    if (drawing) selected = null;
    render();
  }

  function render() {
    root().replaceChildren(
      RV.el("div", { class: "rv-frame-top" },
        RV.el("button", { type: "button", class: "rv-link", onclick: () => RV.show("single") },
          "← Back to single boxes ", kbd("F")),
        RV.el("span", { class: "rv-muted", text: `${state.source_name} · ${new Date(state.ts).toLocaleString()}` }),
        RV.el("button", { type: "button", class: drawing ? "on" : "", disabled: Boolean(state.complete), onclick: toggleDraw },
          drawing ? "Drawing: drag on the frame (Esc to cancel) " : "Draw a box ", kbd("D"))),
      RV.el("div", { class: "rv-layout rv-layout-frame" },
        RV.el("div", {}, frameCanvas(), RV.el("p", { class: "rv-cap",
          text: "Click a box to answer it. Yellow dashed: needs an answer. Green: answered. Blue: drawn by a volunteer." })),
        RV.el("aside", { class: "rv-side", id: "rv-frame-panel" }, ...panelContent())));
  }

  // ---- saving ------------------------------------------------------------------

  async function answerModel(outcome, extra) {
    const b = modelBox(selected.id);
    const body = { event_id: b.event_id, review_item_id: b.review_item_id, outcome, ...extra };
    if (outcome === "accept" || outcome === "species") body.not_sure = notSure;
    if (outcome !== "no_animal" && selected.bbox.some((v, i) => v !== b.bbox[i])) body.bbox = selected.bbox;
    const r = await RV.post("/api/review/verdicts", body);
    if (!r.ok) {
      RV.toast(r.data.error || "That answer wasn't saved.");
      if (r.data.error === "pick a species from the list") await RV.loadCatalogue();
      if (r.status === 409 || r.status === 403) await reload(false);
      return;
    }
    await RV.refreshMine();
    await reload(false);
  }

  async function saveAddedBox(answer) {
    const a = selected.kind === "added" ? addedBox(selected.id) : null;
    const payload = { bbox: selected.bbox };
    if (a) {
      Object.assign(payload, { outcome: a.outcome, label: a.label, life_stage: a.life_stage,
        not_sure: a.not_sure, celebrity_id: a.celebrity_id });
    }
    if (answer) Object.assign(payload, answer, { not_sure: notSure && answer.outcome === "species" });
    if (!payload.outcome) { renderPanel(); return; }         // a new box waits for its answer
    if (payload.outcome !== "species") Object.assign(payload, { label: null, life_stage: null, not_sure: false });
    const url = `/api/review/frames/${state.saved_frame_id}/boxes`;
    const r = a ? await RV.patch(`${url}/${a.id}`, payload) : await RV.post(url, payload);
    if (!r.ok) { RV.toast(r.data.error || "That box wasn't saved."); await reload(true); return; }
    selected = null;
    await reload(false);
  }

  async function removeAddedBox() {
    const r = await RV.del(`/api/review/frames/${state.saved_frame_id}/boxes/${selected.id}`);
    if (!r.ok) { RV.toast(r.data.error || "Couldn't remove that box."); return; }
    selected = null;
    await reload(false);
  }

  async function complete() {
    const r = await RV.post(`/api/review/frames/${state.saved_frame_id}/complete`);
    if (!r.ok) { RV.toast(r.data.error || "The frame isn't complete yet."); await reload(true); return; }
    RV.toast("Frame marked complete. Thank you!");
    RV.show("single");
  }

  RV.onKey("frame", (key) => {
    if (!state) return false;
    const k = key.length === 1 ? key.toLowerCase() : key;
    if (k === "f") { RV.show("single"); return true; }
    if (k === "d") { toggleDraw(); return true; }
    if (k === "Escape") {
      if (drawing) { drawing = false; render(); } else if (selected && selected.original) {
        selected.bbox = selected.original.slice();
        render();
      }
      return true;
    }
    if (k === "/" && picker) { picker.focus(); return true; }
    if (!selected) return false;
    if (selected.kind === "model") {
      const b = modelBox(selected.id);
      if (!b || b.state !== "needs_answer") return false;
      if (k === "a" && b.model) { answerModel("accept", {}); return true; }
      if (k === "c") { answerModel("unidentifiable", {}); return true; }
      if (k === "n") { answerModel("no_animal", {}); return true; }
    } else if (k === "c") {
      saveAddedBox({ outcome: "unidentifiable" });
      return true;
    }
    return false;
  });

  return { enter };
})();
```

- [ ] **Step 3: Append the frame styles to `review.css`**

```css
/* ---- whole-frame mode (Task 11) ---- */
.rv-frame-top { display: flex; gap: 12px; align-items: center; margin-bottom: 8px; flex-wrap: wrap; }
.rv-frame-top button.on { background: #3ad1ff; color: #062033; }
.rv-layout-frame { grid-template-columns: minmax(0, 1.9fr) minmax(280px, 1fr); }
.rv-frame-canvas .rv-editor-svg.drawing { cursor: crosshair; }
.rv-fb { fill: rgba(0, 0, 0, 0.001); stroke-width: 2px; vector-effect: non-scaling-stroke; cursor: pointer; }
.rv-fb-pending { stroke: #ffc400; stroke-dasharray: 6 4; }
.rv-fb-done { stroke: #34c759; }
.rv-fb-locked { stroke: #8aa3b5; stroke-dasharray: 2 4; }
.rv-fb-added { stroke: #3ad1ff; stroke-dasharray: 6 4; }
.rv-fb.rv-fb-selected { stroke: #ffc400; stroke-width: 3px; stroke-dasharray: none; }
.rv-fb-label { fill: #fff; paint-order: stroke; stroke: #000; stroke-width: 3px; font-weight: 700;
  pointer-events: none; }
.rv-checklist { list-style: none; margin: 4px 0 0; padding: 0; font-size: 13px; }
.rv-cl { padding: 4px 6px; border-left: 3px solid transparent; cursor: pointer; }
.rv-cl.rv-fb-pending { border-left-color: #ffc400; }
.rv-cl.rv-fb-done { border-left-color: #34c759; }
.rv-cl.rv-fb-locked { border-left-color: #8aa3b5; }
.rv-cl.rv-fb-added { border-left-color: #3ad1ff; }
```

- [ ] **Step 4: Load the script**

In `templates/review.html`, after the `review-single.js` tag, add:
`<script src="/static/review-frame.js?v={{ asset_version }}"></script>`

- [ ] **Step 5: Run the page tests**

Run: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn timeout 900 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest tests/test_review_page.py -q`
Expected: all pass (stop the demo server from Task 10 first; the suite truncates its database. Re-seed afterwards).

- [ ] **Step 6: Smoke-check whole-frame mode on the demo**

Re-seed and restart the demo (Task 10, Step 10). Then:

```bash
/raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python - <<'EOF'
import httpx
user, pw = open(".superpowers/demo_credentials").readline().strip().split(": ")
c = httpx.Client(base_url="http://127.0.0.1:18090", headers={"X-Requested-With": "wahoobay"})
c.post("/api/auth/login", json={"username": user, "password": pw})
box = c.get("/api/review/next?lane=check").json()
state = c.post(f"/api/review/frames/{box['box']['saved_frame_id']}/open").json()
print(len(state["boxes"]), "model boxes;", [b["state"] for b in state["boxes"]])
print("frame image:", c.get(state["frame"]["url"] + "?size=1600").status_code)
EOF
```

Expected: at least one model box, states printed, `frame image: 200`.

- [ ] **Step 7: Commit**

```bash
git add services/dashboard/app/static/review-frame.js services/dashboard/app/static/review.css services/dashboard/app/templates/review.html tests/test_review_page.py
git commit -m "review: whole-frame view for answering every box and drawing missed fish

The frame becomes the work area, boxes are numbered and coloured by
state, and pressing D lets a volunteer drag a box around an animal the
model missed. The checklist and the Mark frame complete button say
exactly which boxes still need an answer.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 12: Browse view and admin panels, and the manual checklist

**Files:**
- Create: `services/dashboard/app/static/review-browse.js`
- Modify: `services/dashboard/app/static/review.css` (append), `services/dashboard/app/templates/review.html` (script tag), `tests/test_review_page.py` (`SCRIPTS`)

**Interfaces:**
- Consumes: `RV.*` (Task 10); browse, progress and admin endpoints and the cell shape (Task 9); `RV.views.single` and `RV.views.frame`.
- Produces: `RV.views.browse = {enter()}`.

- [ ] **Step 1: Extend the page test**

In `tests/test_review_page.py`, change `SCRIPTS` to `["review-core.js", "review-single.js", "review-frame.js", "review-browse.js"]`.

Run: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn timeout 900 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest tests/test_review_page.py -q`
Expected: FAIL (missing file and script tag).

- [ ] **Step 2: Write `static/review-browse.js`**

```js
// services/dashboard/app/static/review-browse.js
// Browse: headline counts, filters, a grid of boxes with their status and
// species progress; for administrators, disputes, agreement, the date
// window and pool status.
"use strict";

RV.views.browse = (() => {
  const root = () => document.getElementById("rv-browse");
  const filters = { status: "any", species: "", camera: "", who: "anyone", lane: "any", date_from: "", date_to: "" };
  let page = 0;
  let progress = null;

  const BADGES = { single: ["✓ verified", "ok"], agreed: ["✓✓ agreed", "two"], adjudicated: ["settled", "two"],
    disputed: ["disputed", "disp"], in_queue: ["in queue", "open"],
    awaiting_second_opinion: ["awaiting 2nd opinion", "open"] };

  function answerText(side) {
    if (side.outcome === "no_animal") return "no animal";
    if (side.outcome === "unidentifiable") return "can't identify";
    return side.common || side.label || "?";
  }

  async function enter() {
    root().replaceChildren(RV.el("p", { class: "rv-empty", text: "Loading…" }));
    const r = await RV.get("/api/review/progress");
    if (!r.ok) { root().replaceChildren(RV.el("p", { class: "rv-empty", text: r.data.error || "Couldn't load progress." })); return; }
    progress = r.data;
    page = 0;
    render();
    await loadGrid();
    if (RV.role === "admin") await loadAdmin();
  }

  const kpi = (value, label) => RV.el("div", { class: "rv-kpi" },
    RV.el("b", { text: String(value) }), RV.el("span", { class: "rv-muted", text: label }));

  function render() {
    const k = progress.kpis;
    root().replaceChildren(
      RV.el("div", { class: "rv-kpis" },
        kpi(k.verified_boxes, "verified boxes"), kpi(k.species_with_verified, "species with verified boxes"),
        kpi(k.complete_frames, "complete frames"), kpi(k.session_verified, "answered this session"),
        kpi(k.open.check + k.open.identify, "open in the queue")),
      filterBar(),
      RV.el("div", { class: "rv-browse-body" },
        RV.el("div", {}, RV.el("div", { id: "rv-grid", class: "rv-grid" }), RV.el("div", { id: "rv-pager", class: "rv-row rv-small" })),
        speciesList()),
      RV.role === "admin" ? RV.el("section", { id: "rv-admin", class: "rv-admin" }) : null);
  }

  function select(name, options, value, onchange) {
    return RV.el("label", { class: "rv-filter" }, `${name} `,
      RV.el("select", { onchange: (ev) => onchange(ev.target.value) },
        options.map(([v, text]) => RV.el("option", { value: v, selected: v === value }, text))));
  }

  function filterBar() {
    const set = (key) => (value) => { filters[key] = value; page = 0; loadGrid(); };
    const species = RV.el("input", { type: "search", class: "rv-search rv-search-small", id: "rv-species-filter",
      placeholder: "Species (Latin name)", value: filters.species, list: "rv-species-names" });
    species.addEventListener("change", () => set("species")(species.value.trim()));
    const names = RV.el("datalist", { id: "rv-species-names" },
      [...new Set(RV.catalogue.map((e) => e.name))].map((n) => RV.el("option", { value: n })));
    const date = (key, label) => {
      const input = RV.el("input", { type: "date", value: filters[key] });
      input.addEventListener("change", () => set(key)(input.value));
      return RV.el("label", { class: "rv-filter" }, `${label} `, input);
    };
    return RV.el("div", { class: "rv-filters" },
      select("Status", [["any", "Any"], ["verified", "Verified"], ["agreed", "Agreed on spot-check"],
        ["adjudicated", "Settled by an admin"], ["disputed", "Disputed"], ["unidentifiable", "Can't identify"],
        ["no_animal", "No animal"], ["in_queue", "In the queue"]], filters.status, set("status")),
      species, names,
      select("Camera", [["", "Both cameras"], ["seahivecam", "SEAHIVECAM"], ["pier_cam", "Pier"]], filters.camera, set("camera")),
      select("Who", [["anyone", "Anyone"], ["me", "Me"], ["session", "This session"]], filters.who, set("who")),
      select("Lane", [["any", "Both lanes"], ["check", "Model's guesses"], ["identify", "New fish"]], filters.lane, set("lane")),
      date("date_from", "From"), date("date_to", "To"));
  }

  function cell(c) {
    let badge = BADGES[c.status] || [c.status, "open"];
    if (c.status !== "disputed" && c.outcome === "unidentifiable") badge = ["can't identify", "cant"];
    if (c.status !== "disputed" && c.outcome === "no_animal") badge = ["no animal", "cant"];
    const name = c.label ? (c.common || c.label) : c.status === "in_queue" ? "" : "—";
    const who = c.reviewers.length ? ` · ${c.reviewers.join(", ")}` : "";
    return RV.el("button", { type: "button", class: "rv-cell", title: `${c.camera} · ${new Date(c.ts).toLocaleString()}${who}`,
      onclick: () => open(c) },
    RV.el("img", { src: c.zoom_url, alt: "", loading: "lazy" }),
    RV.el("span", { class: "rv-cell-label" }, RV.el("span", { text: name }),
      RV.el("span", { class: `rv-badge rv-b-${badge[1]}`, text: badge[0] })));
  }

  function open(c) {
    if (c.kind === "box" && c.mine) { RV.show("single", { reopenEventId: c.event_id }); return; }
    if (c.saved_frame_id && RV.views.frame) { RV.show("frame", { savedFrameId: c.saved_frame_id }); return; }
    RV.toast("Only your own answers can be changed.");
  }

  async function loadGrid() {
    const q = new URLSearchParams({ status: filters.status, who: filters.who, lane: filters.lane, page: String(page) });
    for (const key of ["species", "camera", "date_from", "date_to"]) if (filters[key]) q.set(key, filters[key]);
    const r = await RV.get(`/api/review/browse?${q}`);
    const grid = document.getElementById("rv-grid");
    const pager = document.getElementById("rv-pager");
    if (!grid) return;
    if (!r.ok) { grid.replaceChildren(RV.el("p", { text: r.data.error || "Couldn't load boxes." })); return; }
    const cells = r.data.cells || [];
    grid.replaceChildren(...(cells.length ? cells.map(cell)
      : [RV.el("p", { class: "rv-muted", text: "Nothing matches these filters." })]));
    pager.replaceChildren(
      RV.el("button", { type: "button", disabled: page === 0, onclick: () => { page -= 1; loadGrid(); } }, "← Newer"),
      RV.el("span", { class: "rv-muted", text: `Page ${page + 1}` }),
      RV.el("button", { type: "button", disabled: !r.data.has_more, onclick: () => { page += 1; loadGrid(); } }, "Older →"));
  }

  function speciesList() {
    const rows = progress.species.slice(0, 60).map((s) => {
      const base = Math.min(100, (100 * s.baseline) / s.target);
      const ver = Math.min(100 - base, (100 * s.verified) / s.target);
      return RV.el("button", { type: "button", class: "rv-sp", onclick: () => {
        filters.species = s.name;
        const input = document.getElementById("rv-species-filter");
        if (input) input.value = s.name;
        page = 0;
        loadGrid();
      } },
      RV.el("span", {}, RV.el("span", { text: s.common || s.name }), " ", RV.el("span", { class: "rv-latin", text: s.name })),
      RV.el("span", { class: "rv-bar", "aria-hidden": "true" },
        RV.el("i", { class: "rv-bar-base", style: `width:${base}%` }), RV.el("i", { class: "rv-bar-ver", style: `width:${ver}%` })),
      RV.el("span", { class: "rv-muted", text: `${s.baseline} + ${s.verified}` }));
    });
    return RV.el("aside", { class: "rv-species" },
      RV.el("div", { class: "rv-label", text: `Species progress · thinnest first · target ${progress.target}` }),
      ...rows,
      RV.el("p", { class: "rv-muted rv-small",
        text: "Grey: crops already in the training set. Green: verified here. Click a species to filter the grid." }));
  }

  // ---- administrator panels ----------------------------------------------------

  async function loadAdmin() {
    const section = document.getElementById("rv-admin");
    if (!section) return;
    const [disputes, agreement, status] = await Promise.all([
      RV.get("/api/review/disputes"), RV.get("/api/review/agreement"), RV.get("/api/review/status")]);
    section.replaceChildren(
      RV.el("h3", { text: "Administrator" }),
      disputesPanel(disputes.ok ? disputes.data.disputes : []),
      agreementPanel(agreement.ok ? agreement.data : null),
      windowPanel(status.ok ? status.data.window : { start: null, end: null }),
      statusPanel(status.ok ? status.data : null));
  }

  async function adjudicate(eventId, body) {
    const r = await RV.post(`/api/review/disputes/${eventId}/adjudicate`, body);
    if (!r.ok) { RV.toast(r.data.error || "That wasn't saved."); return; }
    RV.toast("Settled.");
    await loadAdmin();
  }

  function disputesPanel(list) {
    const body = list.length ? list.map((d) => {
      const use = (side) => adjudicate(d.event_id, { outcome: side.outcome, label: side.label, life_stage: side.life_stage });
      const slot = RV.el("div", { class: "rv-picker" });
      return RV.el("div", { class: "rv-dispute" },
        RV.el("img", { src: d.zoom_url, alt: "" }),
        RV.el("div", {},
          RV.el("button", { type: "button", onclick: () => use(d.first) }, `Use answer 1: ${answerText(d.first)} (${d.first.by})`),
          RV.el("button", { type: "button", onclick: () => use(d.second) }, `Use answer 2: ${answerText(d.second)} (${d.second.by})`),
          RV.el("button", { type: "button", onclick: () => {
            slot.replaceChildren();
            new RV.SpeciesPicker(slot, (e) => adjudicate(d.event_id,
              { outcome: "species", label: e.name, life_stage: e.life_stage })).focus();
          } }, "Relabel…"),
          slot));
    }) : [RV.el("p", { class: "rv-muted", text: "No disputes." })];
    return RV.el("div", { class: "rv-panel" }, RV.el("h4", { text: `Disputes (${list.length})` }), ...body);
  }

  function agreementPanel(a) {
    const panel = RV.el("div", { class: "rv-panel" }, RV.el("h4", { text: "Agreement on spot-checks" }));
    if (!a) { panel.append(RV.el("p", { text: "Unavailable." })); return panel; }
    const fmt = (b) => (b.pairs < a.min_pairs
      ? `not enough spot-checks yet (${b.pairs} of ${a.min_pairs})`
      : `${Math.round(100 * b.percent)}% agree · alpha ${b.alpha === null ? "n/a" : b.alpha.toFixed(2)} · ${b.pairs} pairs`);
    const how = { model_guess: "accepted the model's guess", hint: "clicked a hint", search: "searched", none: "gave no species" };
    panel.append(RV.el("p", { text: `Overall: ${fmt(a.overall)}` }),
      RV.el("ul", {}, Object.entries(a.by_label_source).map(([k, b]) =>
        RV.el("li", { text: `When the first volunteer ${how[k] || k}: ${fmt(b)}` }))));
    return panel;
  }

  function windowPanel(w) {
    // Stored start: local midnight of the first day. Stored end: local
    // midnight of the day AFTER the last day (the end day is inclusive).
    const day = (iso) => (iso ? iso.slice(0, 10) : "");
    const dayBefore = (iso) => {
      if (!iso) return "";
      const d = new Date(`${iso.slice(0, 10)}T12:00:00Z`);
      d.setUTCDate(d.getUTCDate() - 1);
      return d.toISOString().slice(0, 10);
    };
    const start = RV.el("input", { type: "date", value: day(w.start) });
    const end = RV.el("input", { type: "date", value: dayBefore(w.end) });
    const save = async (clear) => {
      const body = clear ? { start: null, end: null } : { start: start.value || null, end: end.value || null };
      const r = await RV.put("/api/review/settings/window", body);
      if (!r.ok) { RV.toast(r.data.error || "Not saved."); return; }
      RV.toast(`Window saved; ${r.data.retired} open boxes outside it were retired.`);
      await loadAdmin();
    };
    return RV.el("div", { class: "rv-panel" }, RV.el("h4", { text: "Date window" }),
      RV.el("p", { class: "rv-muted",
        text: "Only footage from these days (inclusive, local time) enters the queue. Live water readings exist from 2026-05-17 to 2026-06-25." }),
      RV.el("div", { class: "rv-row rv-small" }, "From ", start, " to ", end,
        RV.el("button", { type: "button", onclick: () => save(false) }, "Save"),
        RV.el("button", { type: "button", onclick: () => save(true) }, "Clear")));
  }

  function statusPanel(s) {
    const panel = RV.el("div", { class: "rv-panel" }, RV.el("h4", { text: "Pool status" }));
    if (!s) { panel.append(RV.el("p", { text: "Unavailable." })); return panel; }
    const run = s.last_run || {};
    const lines = [
      run.finished_at
        ? `Last top-up ${new Date(run.finished_at).toLocaleString()}: ${run.check_added} + ${run.identify_added} boxes added, `
          + `${run.probes} probes, ${run.duration_s}s, ${run.timeouts} timeouts, ${run.errors} errors`
        : "No top-up has finished yet.",
      `Boxes without hints: ${s.unscored}. Last hint written: ${s.last_hinted_at
        ? new Date(s.last_hinted_at).toLocaleString() : "never (the scorer arrives in phase 2)"}.`,
      ...s.items.map((i) => `${i.lane} · pass ${i.pass} · ${i.status}: ${i.count}`),
    ];
    panel.append(RV.el("ul", {}, lines.map((t) => RV.el("li", { text: t }))));
    return panel;
  }

  return { enter };
})();
```

- [ ] **Step 3: Append the browse styles to `review.css`**

```css
/* ---- browse and admin (Task 12) ---- */
.rv-kpis { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px; margin-bottom: 10px; }
@media (max-width: 900px) { .rv-kpis { grid-template-columns: repeat(2, 1fr); } }
.rv-kpi { border: 1px solid #1f3b4f; border-radius: 8px; padding: 8px 10px; background: var(--panel); }
.rv-kpi b { display: block; font-size: 20px; }
.rv-filters { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 10px; font-size: 13px; }
.rv-filter select, .rv-filter input { background: var(--bg); color: var(--ink); border: 1px solid #1f3b4f;
  border-radius: 6px; padding: 4px 6px; }
.rv-search.rv-search-small { width: 200px; }
.rv-browse-body { display: grid; grid-template-columns: minmax(0, 1.6fr) minmax(260px, 1fr); gap: 14px; }
@media (max-width: 900px) { .rv-browse-body { grid-template-columns: 1fr; } }
.rv-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 8px; }
.rv-cell { display: flex; flex-direction: column; padding: 0; border: 1px solid #1f3b4f; border-radius: 6px;
  overflow: hidden; background: var(--panel); color: var(--ink); cursor: pointer; text-align: left; }
.rv-cell img { width: 100%; aspect-ratio: 4 / 3; object-fit: cover; display: block; background: #000; }
.rv-cell-label { display: flex; justify-content: space-between; gap: 4px; padding: 4px 6px; font-size: 11px; }
.rv-badge { font-size: 10px; padding: 0 5px; border-radius: 999px; white-space: nowrap; }
.rv-b-ok { background: rgba(122, 239, 161, .18); color: var(--good); }
.rv-b-two { background: rgba(122, 239, 161, .3); color: var(--good); }
.rv-b-disp { background: rgba(255, 90, 90, .2); color: #ff9a9a; }
.rv-b-cant { background: rgba(255, 180, 87, .2); color: var(--warn); }
.rv-b-open { background: #1f3b4f; color: var(--muted); }
.rv-species { display: flex; flex-direction: column; gap: 2px; }
.rv-sp { display: grid; grid-template-columns: 1fr 90px 64px; gap: 6px; align-items: center; padding: 4px 2px;
  background: none; border: none; border-bottom: 1px solid #1f3b4f; color: var(--ink); font-size: 12px;
  text-align: left; cursor: pointer; }
.rv-bar i.rv-bar-base { background: #8aa3b5; }
.rv-bar i.rv-bar-ver { background: var(--good); }
.rv-admin { margin-top: 18px; display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 12px; }
.rv-admin h3 { grid-column: 1 / -1; margin: 0; }
.rv-panel { border: 1px solid #1f3b4f; border-radius: 8px; padding: 10px; background: var(--panel); }
.rv-panel h4 { margin: 0 0 8px; }
.rv-panel button { padding: 5px 8px; border-radius: 6px; border: 1px solid #1f3b4f; background: var(--bg);
  color: var(--ink); cursor: pointer; }
.rv-dispute { display: flex; gap: 8px; margin-bottom: 8px; }
.rv-dispute img { width: 120px; height: 90px; object-fit: cover; border-radius: 4px; }
.rv-dispute button { display: block; margin-bottom: 4px; }
#rv-pager button { padding: 4px 8px; border-radius: 6px; border: 1px solid #1f3b4f; background: var(--bg);
  color: var(--ink); cursor: pointer; }
#rv-pager button:disabled { opacity: .4; cursor: default; }
```

- [ ] **Step 4: Load the script**

In `templates/review.html`, after the `review-frame.js` tag, add:
`<script src="/static/review-browse.js?v={{ asset_version }}"></script>`

- [ ] **Step 5: Run the page tests**

Run: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn timeout 900 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest tests/test_review_page.py -q`
Expected: all pass (demo server stopped first).

- [ ] **Step 6: Commit**

```bash
git add services/dashboard/app/static/review-browse.js services/dashboard/app/static/review.css services/dashboard/app/templates/review.html tests/test_review_page.py
git commit -m "review: browse view with species progress, and the admin panels

Headline counts, filters (status, species, camera, me or this session,
lane, dates), a grid of boxes with their status, and each species'
progress toward its target. Administrators settle disputes, read
agreement by how labels were chosen, set the date window, and see the
pool's last top-up.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 7: Hand the manual checklist to the user**

Re-seed and restart the demo (Task 10, Step 10). The user opens it through VS Code's **Ports** tab (forward port 18090, then open `http://localhost:18090/review`), signs in with the accounts in `.superpowers/demo_credentials`, and works through this list. Record anything that fails as a finding for this task.

1. **Check lane.** A box loads with the zoomed fish on the left and the frame, guess, search and buttons on the right. The "Counts toward" bar names a species.
2. **Keys.** `A` accepts and loads the next box; `/` focuses the search; typing and `Enter` picks a species; `1`–`3` pick hint buttons when present; `C`, `N`, `S` do their jobs; the session count rises.
3. **Box editing.** Drag a corner, an edge and the middle of the box; it stays inside the image; `Esc` puts it back; the answer after a drag is saved with the new box (reopen it from the strip to see).
4. **Life stages and named fish.** Searching "French" offers adult and juvenile separately. Picking Blue Tang asks about Bobby.
5. **Identify lane.** No "Looks right"; hints (when any) are labelled as weak guesses.
6. **Recent strip.** Clicking a recent box reopens it with "Changing your answer", and a new answer replaces the old one.
7. **Whole frame.** `F` opens the frame with numbered boxes; answering one turns it green; `D` then a drag draws a new box, which asks for a species; the complete button stays disabled until every model box is answered, then succeeds; `F` returns.
8. **Browse.** Counts show; filters change the grid; clicking one of your boxes reopens it; clicking someone else's opens its frame; species progress sorts thinnest first and filters the grid.
9. **Admin (demo_admin only).** Disputes, agreement, the date window (set 2026-05-17 to 2026-06-25, save, see boxes retired) and pool status all render.
10. **A second browser (or a private window) as the other account** is never shown a box the first account is holding.

---

### Task 13: Documentation and configuration

**Files:**
- Modify: `docs/operations.md`, `.env.example`, `README.md`, `services/dashboard/app/main.py` (module docstring)
- Test: `tests/test_review_docs.py`

**Interfaces:**
- Consumes: the `REVIEW_*` and `DASHBOARD_DB_POOL_MAX` variables read in Task 6.
- Produces: documentation only.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_review_docs.py
"""Every review-tool setting the dashboard reads is documented."""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_every_review_setting_is_in_env_example():
    main = (REPO / "services" / "dashboard" / "app" / "main.py").read_text()
    names = set(re.findall(r'_env\(\s*"((?:REVIEW|DASHBOARD_DB_POOL)[A-Z_]*)"', main))
    assert names, "no review settings found in main.py"
    example = (REPO / ".env.example").read_text()
    missing = sorted(n for n in names if not re.search(rf"^#?\s*{n}=", example, re.M))
    assert not missing, f"undocumented in .env.example: {missing}"


def test_operations_doc_covers_the_pool_task():
    ops = (REPO / "docs" / "operations.md").read_text()
    for phrase in ("REVIEW_POOL_ENABLED", "2026-09-24-review-tool.sql", "/api/review/status",
                   "seed_review_demo.py"):
        assert phrase in ops, phrase
```

Run: `timeout 300 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest tests/test_review_docs.py -q`
Expected: FAIL (settings undocumented).

- [ ] **Step 2: Document the settings in `.env.example`**

Append:

```bash
# ---- Review tool (docs/superpowers/specs/2026-09-24-reviewer-annotation-dataset-design.md) ----
# Connections in the dashboard's database pool. The review pool task holds
# one for up to 30 s per run and the matview refresher one per REFRESH.
DASHBOARD_DB_POOL_MAX=6
# The background task that keeps boxes ready for /review. Set to false to
# stop it (restart the dashboard); the page keeps serving what is queued.
REVIEW_POOL_ENABLED=true
REVIEW_POOL_TOPUP_INTERVAL_S=300
# Per-run limits: index probes, and wall-clock seconds.
REVIEW_POOL_MAX_PROBES=200
REVIEW_POOL_MAX_RUN_S=30
# How many open boxes to keep per species (check lane), and in the identify lane.
REVIEW_POOL_CHECK_OPEN_PER_SPECIES=20
REVIEW_POOL_IDENTIFY_OPEN=500
# No two boxes from the same camera within this many seconds (the
# duplicate guard: frame and track ids restart with the workers).
REVIEW_POOL_SPACING_S=120
# Smallest box offered, in pixels on its shorter side.
REVIEW_MIN_BOX_PX=32
# Unguessed boxes below this detector confidence are not offered.
REVIEW_IDENTIFY_MIN_DET_CONF=0.4
# Crops per species the queue works toward (training set + verified).
REVIEW_TARGET_PER_SPECIES=200
# How long a served box stays held for the volunteer who got it.
REVIEW_CLAIM_S=600
# Share of first answers re-checked blind, and share of servings that are re-checks.
REVIEW_SPOTCHECK_RATE=0.10
REVIEW_SPOTCHECK_SERVE_RATE=0.10
# Where the species catalogue and the training-set counts are read from.
# The defaults are the repo's assets/ and data/ folders.
# REVIEW_ASSETS_DIR=/raid/scratch/dzimmerman2021/wahoobay/assets
# REVIEW_TAXONOMY_CSV=/raid/scratch/dzimmerman2021/wahoobay/assets/taxonomy.csv
# REVIEW_BASELINE_MANIFEST=/raid/scratch/dzimmerman2021/wahoobay/data/fine_tune/_dataset_manifest.json
```

- [ ] **Step 3: Add a "Review tool" section to `docs/operations.md`**

Add the section after the auth sections, and add "review pool" to the list of background tasks in the Stopping section (the sentence that names "SLO checker, matview refresher, sheet poller, session pruner").

```markdown
## Review tool (/review)

Volunteers with the reviewer or admin role label boxes at `/review`. The
design is `docs/superpowers/specs/2026-09-24-reviewer-annotation-dataset-design.md`.

### The pool task

A background task in the dashboard keeps `review_items` topped up, every
`REVIEW_POOL_TOPUP_INTERVAL_S` (300 s). Each run is capped at
`REVIEW_POOL_MAX_PROBES` index lookups and `REVIEW_POOL_MAX_RUN_S`
seconds, every query has a 5 s timeout scoped to its own transaction,
and a run never overlaps the previous one. Its log lines start with
`review pool:` in `logs/app/dashboard.log`; each run logs what it added,
how many probes it used, and any timeouts or errors.

- **Status:** `GET /api/review/status` (admin) shows the last run, open
  boxes by lane and pass, and the date window. The Browse tab shows the
  same to admins.
- **Turn it off:** set `REVIEW_POOL_ENABLED=false` in `.env` and restart
  the dashboard. Queued boxes keep being served; nothing new is added.
- **Date window:** admins can limit the queue to a range of days on the
  Browse tab (for example 2026-05-17 to 2026-06-25, the period with live
  sonde readings). Open boxes outside the window are retired at once.
- **Settings:** see the `REVIEW_*` block in `.env.example`.

### Schema

The review tool's tables and columns are in
`db/migrations/2026-09-24-review-tool.sql` (mirrored in `db/init.sql`).
Apply it to the live database with the procedure in "Applying a schema
change to the live database" above:

    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -1 -f db/migrations/2026-09-24-review-tool.sql

It is idempotent. It changes `detection_corrections.event_id` to
`ON DELETE RESTRICT`: deleting a detection that has a human verdict now
fails instead of silently deleting the verdict.

### Trying changes before deploying

`scripts/dev/seed_review_demo.py` copies an evenly spread sample of real
frames and detections into the test database (reading production in a
read-only session) and creates two demo accounts, whose passwords go to
`.superpowers/demo_credentials`. `--serve` then runs a throwaway
dashboard on `127.0.0.1:18090` against that database, with the sheet
poller, workers and mail off. Forward the port in VS Code to use it. The
test suite truncates the same database, so stop the demo before running
tests.
```

- [ ] **Step 4: Update `README.md` and the `main.py` docstring**

In `README.md`, in the dashboard section's list of pages, add a line: "`/review` — the reviewer annotation tool (reviewer or admin role): a queue of boxes to verify, whole-frame mode, and browse. See docs/operations.md, 'Review tool'."

In `main.py`'s module docstring, add to the "Major surfaces" list:

```
  * **Review tool** (`routes_review.py`): `/review` and `/api/review/*`,
    for volunteers with the reviewer or admin role. Rules live in the
    `review_*` modules.
```

and change the background-tasks line to "SLO checker, matview refresher, Google Sheet poller, session/token pruner, review pool."

- [ ] **Step 5: Run the test and the whole suite**

Run: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn timeout 900 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest tests -q`
Expected: every test passes.

- [ ] **Step 6: Commit**

```bash
git add docs/operations.md .env.example README.md services/dashboard/app/main.py tests/test_review_docs.py
git commit -m "docs: operating the review tool

How the pool task works and how to watch it, switch it off or narrow it
to a date range, how to apply the migration, and how to try changes on
real footage with the demo script before deploying. Every review setting
is in .env.example, and a test keeps it that way.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 14: Deploy to production (only with the user's go-ahead)

**Files:** none changed. This task runs commands against production.

**Interfaces:**
- Consumes: the merged branch; `db/migrations/2026-09-24-review-tool.sql`; `scripts/dev/explain_review_probes.py`; the restart recipe in `docs/operations.md`.
- Produces: the review tool live on the public dashboard.

- [ ] **Step 1: Final verification in the worktree**

Run: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn timeout 900 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest tests -q` and `/raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/ruff check services/dashboard/app scripts tests`.
Expected: all tests pass; ruff clean on the files this plan touched.

- [ ] **Step 2: Stop and ask the user**

Report: the manual checklist results (Task 12, Step 7), the test count, and exactly what the next steps do to production: a schema migration (brief insert lock on `detection_events` and `saved_frames` while the foreign keys are added, bounded by a 5 s lock timeout), a dashboard restart (about 20 s of downtime), and a new background task. Ask whether to merge (squash, as spec 1 was) and deploy now. Do not continue without a yes.

- [ ] **Step 3: Merge**

Merge the worktree branch into `reports-panel-and-weather` the way the user chose, in the main checkout at `/raid/scratch/dzimmerman2021/wahoobay`. Stage nothing from `assets/` except `assets/taxonomy.csv` and `assets/taxonomy_unresolved.txt` (which arrive with the merge).

- [ ] **Step 4: Check the probe plans against production**

```bash
cd /raid/scratch/dzimmerman2021/wahoobay
timeout 120 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python scripts/dev/explain_review_probes.py --env .env
```

Expected: `RESULT: index scans only`. If it reports a sequential scan, add `REVIEW_POOL_ENABLED=false` to `.env` before the restart and report the plans to the user.

- [ ] **Step 5: Apply the migration**

```bash
cd /raid/scratch/dzimmerman2021/wahoobay
set -a; . ./.env; set +a
/raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -1 -f db/migrations/2026-09-24-review-tool.sql
/raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/psql "$DATABASE_URL" -At -c "SELECT count(*) FROM information_schema.tables WHERE table_name LIKE 'review_%'" -c "SELECT confdeltype FROM pg_constraint WHERE conname = 'detection_corrections_event_id_fkey'"
```

Expected: the migration exits 0; the checks print `5` and `r`. If the migration fails on the lock timeout, wait a minute and run it again (it is idempotent).

- [ ] **Step 6: Restart the dashboard**

Record the public endpoints' latency first, then restart with the recipe in `docs/operations.md` ("Restarting one service"), then time them again:

```bash
for i in 1 2 3; do curl -s -o /dev/null -w '%{http_code} %{time_total}\n' http://127.0.0.1:18080/api/live.json; done
ss -ltnp 2>/dev/null | grep ':18080' | grep -oP 'pid=\K\d+' | xargs -r kill -TERM
while ss -ltn 2>/dev/null | grep -q ':18080 '; do sleep 1; done
nohup ./scripts/dev/run_dashboard.sh >> logs/app/dashboard.log 2>&1 &
for i in $(seq 1 60); do curl -s -o /dev/null http://127.0.0.1:18080/healthz && break; sleep 1; done
for i in 1 2 3; do curl -s -o /dev/null -w '%{http_code} %{time_total}\n' http://127.0.0.1:18080/api/live.json; done
```

Expected: 200s before and after at similar latency.

- [ ] **Step 7: Watch the first pool runs**

```bash
sleep 90; grep "review pool:" logs/app/dashboard.log | tail -3
```

Expected: a line with `check_added` and `identify_added` above zero and `errors: 0`. After 15–30 minutes, read-only:

```bash
/raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/psql "$DATABASE_URL" -At -c "SELECT lane, pass, status, count(*) FROM review_items GROUP BY 1,2,3 ORDER BY 1,2,3"
```

- [ ] **Step 8: Hand over**

The user signs in at `https://<tunnel>/review`, runs checklist items 1–3, 7 and 8 on production, and approves a one- or two-volunteer pilot. Report what was deployed, the pool numbers, and the latency comparison.

