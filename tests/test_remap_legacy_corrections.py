"""scripts/dev/remap_legacy_corrections.py (follow-up spec A1b)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import psycopg
import pytest
from app.review_catalogue import CatalogueStore
from review_fixtures import TEST_DSN, add_verdict, catalogue_paths, make_user, seed_frame

pytestmark = pytest.mark.anyio

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dev" / "remap_legacy_corrections.py"


def _script():
    spec = importlib.util.spec_from_file_location("remap_legacy_corrections", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # dataclasses' field-annotation check (postponed annotations, via this
    # module's own `from __future__ import annotations`) looks the module
    # up in sys.modules; a dynamically loaded module isn't there unless
    # this registers it first.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


UUID_SPECIES_ID = "a3f1e9c0-3b2a-4c9d-8f10-1e2d3c4b5a6f"


async def _world(rdb, tmp_path):
    f = await seed_frame(rdb, tmp_path, boxes=[{"bbox": [10 + 100 * i, 10, 90 + 100 * i, 90]}
                                               for i in range(7)])
    e = f["event_ids"]
    uid = await make_user(rdb, "one")
    ids = {
        "maps": await add_verdict(rdb, e[0], None, name="Pomatomus saltarix"),
        "same": await add_verdict(rdb, e[1], None, name="Seriola zonata"),
        "lost": await add_verdict(rdb, e[2], None, name="Mystery fish"),
        # a review-tool row records its label source: never touched
        "tool": await add_verdict(rdb, e[3], uid, name="Pomatomus saltarix",
                                  label_source="search"),
        # an adjudication records no label source either, but isn't an
        # old-form row -- the remap must not touch it (ruling T3b)
        "adjudication": await add_verdict(rdb, e[4], uid, name="Pomatomus saltarix",
                                          is_adjudication=True),
    }
    # Already-accepted names whose corrected_species_id holds something the
    # remap must never read or write (ruling T3a): a NULL id, and a
    # Fishial-style UUID that doesn't match the name at all.
    async with rdb.connection() as conn:
        cur = await conn.execute(
            """INSERT INTO detection_corrections (event_id, corrected_name, corrected_species_id)
               VALUES (%s, 'Seriola zonata', NULL) RETURNING id""", (e[5],))
        ids["null_species_id"] = (await cur.fetchone())[0]
        cur = await conn.execute(
            """INSERT INTO detection_corrections (event_id, corrected_name, corrected_species_id)
               VALUES (%s, 'Seriola zonata', %s) RETURNING id""", (e[6], UUID_SPECIES_ID))
        ids["uuid_species_id"] = (await cur.fetchone())[0]
    return ids


async def _names(rdb):
    async with rdb.connection() as conn:
        cur = await conn.execute(
            "SELECT id, corrected_name, corrected_species_id FROM detection_corrections")
        return {r[0]: r[1:] for r in await cur.fetchall()}


async def test_a_dry_run_lists_the_changes_and_changes_nothing(rdb, tmp_path, review_assets):
    ids = await _world(rdb, tmp_path)
    before = await _names(rdb)
    cat = CatalogueStore(catalogue_paths(review_assets)).get()
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        report = _script().remap(conn, cat, apply=False)
    assert report.changes == [(ids["maps"], "Pomatomus saltarix", "Pomatomus saltatrix")]
    assert report.unmapped == [(ids["lost"], "Mystery fish")]
    # same, null_species_id, uuid_species_id: already-accepted names, whatever
    # their species id holds (ruling T3a); tool and adjudication are excluded
    # entirely, not counted here either (they never reach LEGACY_ROWS_SQL).
    assert report.unchanged == 3
    assert await _names(rdb) == before


async def test_apply_maps_corrected_name_and_leaves_the_species_id_alone(
        rdb, tmp_path, review_assets):
    ids = await _world(rdb, tmp_path)
    cat = CatalogueStore(catalogue_paths(review_assets)).get()
    mod = _script()
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        mod.remap(conn, cat, apply=True)
        assert mod.remap(conn, cat, apply=True).changes == []
    names = await _names(rdb)
    # corrected_name is mapped; corrected_species_id keeps its raw, seeded
    # value -- it is provenance, never read or written by the remap.
    assert names[ids["maps"]] == ("Pomatomus saltatrix", "Pomatomus saltarix")
    assert names[ids["lost"]][0] == "Mystery fish"
    assert names[ids["tool"]][0] == "Pomatomus saltarix"
    assert names[ids["adjudication"]][0] == "Pomatomus saltarix"
    assert names[ids["null_species_id"]] == ("Seriola zonata", None)
    assert names[ids["uuid_species_id"]] == ("Seriola zonata", UUID_SPECIES_ID)


async def test_the_command_line_prints_a_dry_run(rdb, tmp_path, review_assets, monkeypatch,
                                                 capsys):
    await _world(rdb, tmp_path)
    for key, value in review_assets["env"].items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    env = tmp_path / "test.env"
    env.write_text("DATABASE_URL=unused-because-already-set\n")
    assert _script().main(["--env", str(env)]) == 0
    out = capsys.readouterr().out
    assert "'Pomatomus saltarix' -> 'Pomatomus saltatrix'" in out
    assert "'Mystery fish': not in the species list; left alone" in out
    assert "1 would change, 3 already accepted, 1 unmapped" in out
    assert TEST_DSN not in out
