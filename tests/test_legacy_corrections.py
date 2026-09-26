"""The dashboard's older correction form, saved through the review rules
(follow-up spec A1)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from app.review_catalogue import Catalogue, CatalogueEntry, CatalogueStore
from app.review_queue import Invalid, legacy_verdict
from review_fixtures import (
    CSRF,
    TEST_DSN,
    add_item,
    add_verdict,
    catalogue_paths,
    login,
    make_user,
    seed_frame,
)

pytestmark = pytest.mark.anyio

BOX = [{"bbox": [100, 100, 300, 250], "species": "Seriola zonata"}]


def _form(event_id, **kw):
    body = {"event_id": event_id, "corrected_name": None, "corrected_species_id": None,
            "not_a_fish": False, "is_endorsement": False, "corrected_bbox": None,
            "confidence": "probable", "notes": None, "celebrity_id": None}
    body.update(kw)
    return body


async def _rows(rdb, event_id):
    async with rdb.connection() as conn:
        cur = await conn.execute(
            """SELECT corrected_name, not_a_fish, is_endorsement, corrected_bbox, confidence,
                      label_source, user_id, review_item_id
                 FROM detection_corrections WHERE event_id = %s ORDER BY id""", (event_id,))
        return await cur.fetchall()


# ---- the adapter ------------------------------------------------------------

@pytest.fixture
def cat(review_assets):
    return CatalogueStore(catalogue_paths(review_assets)).get()


def _ns(**kw):
    return SimpleNamespace(**_form(1, **kw))


def test_an_endorsement_is_an_accept(cat):
    v = legacy_verdict(_ns(corrected_name="Seriola zonata", is_endorsement=True,
                           confidence="certain"), catalogue=cat)
    assert (v.outcome, v.label, v.label_source, v.not_sure) == ("accept", None, None, False)


def test_a_correction_is_a_search_pick_under_its_accepted_name(cat):
    v = legacy_verdict(_ns(corrected_name="Pomatomus saltarix", confidence="uncertain"),
                       catalogue=cat)
    assert (v.outcome, v.label, v.label_source, v.not_sure) == (
        "species", "Pomatomus saltatrix", "search", True)


def test_not_a_fish_drops_the_box_and_the_doubt(cat):
    v = legacy_verdict(_ns(not_a_fish=True, corrected_bbox=[1, 2, 30, 40],
                           confidence="uncertain"), catalogue=cat)
    assert (v.outcome, v.bbox, v.not_sure) == ("no_animal", None, False)


def test_a_name_outside_the_species_list_is_refused(cat):
    with pytest.raises(Invalid, match="not in the species list"):
        legacy_verdict(_ns(corrected_name="Mystery fish"), catalogue=cat)


def test_a_species_with_several_life_stages_is_sent_to_the_review_page(cat):
    # Both of the fixture catalogue's Pomacanthus paru entries carry a
    # stage (adult, juvenile) -- neither is stage-less -- so this still
    # exercises the real refusal (follow-up F2).
    with pytest.raises(Invalid, match="choose a life stage for Pomacanthus paru on the review page"):
        legacy_verdict(_ns(corrected_name="Pomacanthus paru"), catalogue=cat)


def test_a_stage_less_entry_among_several_needs_no_life_stage_choice():
    # Follow-up F2: unlike Pomacanthus paru above, a species whose several
    # catalogue entries include a stage-less one is resolved the way the
    # review page resolves it -- life_stage None, no refusal -- rather
    # than being sent to the review page for a choice that doesn't apply.
    mixed = Catalogue(
        [CatalogueEntry(name="Mixed stage fish", life_stage=None),
         CatalogueEntry(name="Mixed stage fish", life_stage="adult")],
        {}, {})
    v = legacy_verdict(_ns(corrected_name="Mixed stage fish"), catalogue=mixed)
    assert (v.outcome, v.label, v.life_stage) == ("species", "Mixed stage fish", None)


# ---- over HTTP ----------------------------------------------------------------

async def test_an_endorsement_is_saved_as_a_review_verdict_and_closes_the_queue_item(
        rdb, rclient, tmp_path):
    f = await seed_frame(rdb, tmp_path, boxes=BOX)
    item = await add_item(rdb, f, predicted="Seriola zonata")
    await make_user(rdb, "alice")
    login(rclient, "alice")
    r = rclient.post("/api/corrections", headers=CSRF, json=_form(
        f["event_ids"][0], corrected_name="Seriola zonata", corrected_species_id="Seriola zonata",
        is_endorsement=True, confidence="certain"))
    assert r.status_code == 200, r.text
    assert r.json()["is_endorsement"] is True
    [row] = await _rows(rdb, f["event_ids"][0])
    assert row[:3] == ("Seriola zonata", False, True) and row[5] == "model_guess"
    async with rdb.connection() as conn:
        cur = await conn.execute("SELECT status FROM review_items WHERE id = %s", (item,))
        assert (await cur.fetchone())[0] == "done"


async def test_a_misspelled_name_is_stored_under_its_accepted_name(rdb, rclient, tmp_path):
    f = await seed_frame(rdb, tmp_path, boxes=BOX)
    await make_user(rdb, "alice")
    login(rclient, "alice")
    r = rclient.post("/api/corrections", headers=CSRF, json=_form(
        f["event_ids"][0], corrected_name="Pomatomus saltarix", confidence="uncertain"))
    assert r.status_code == 200, r.text
    [row] = await _rows(rdb, f["event_ids"][0])
    assert (row[0], row[4], row[5]) == ("Pomatomus saltatrix", "uncertain", "search")


async def test_the_form_gets_the_review_rules_refusals(rdb, rclient, tmp_path):
    f = await seed_frame(rdb, tmp_path, boxes=BOX)
    g = await seed_frame(rdb, tmp_path, frame_id=2, boxes=BOX)
    other = await make_user(rdb, "other")
    await add_verdict(rdb, f["event_ids"][0], other, name="Seriola zonata")
    await add_item(rdb, g, status="claimed", claimed_by=other,
                   claimed_until=datetime.now(UTC) + timedelta(minutes=5))
    await make_user(rdb, "alice")
    login(rclient, "alice")
    r = rclient.post("/api/corrections", headers=CSRF, json=_form(f["event_ids"][0],
                                                                  not_a_fish=True))
    assert r.status_code == 403
    assert r.json()["error"] == "another volunteer already answered this box"
    r = rclient.post("/api/corrections", headers=CSRF, json=_form(g["event_ids"][0],
                                                                  not_a_fish=True))
    assert r.status_code == 409
    r = rclient.post("/api/corrections", headers=CSRF, json=_form(
        g["event_ids"][0], corrected_name="Mystery fish"))
    assert r.status_code == 400 and "not in the species list" in r.json()["error"]


async def test_a_detection_without_a_saved_frame_is_refused_readably(rdb, rclient, tmp_path):
    f = await seed_frame(rdb, tmp_path, boxes=BOX, save=False)
    await make_user(rdb, "alice")
    login(rclient, "alice")
    r = rclient.post("/api/corrections", headers=CSRF, json=_form(f["event_ids"][0],
                                                                  not_a_fish=True))
    assert r.status_code == 400
    assert r.json()["error"] == "this detection's frame was not saved"
    assert await _rows(rdb, f["event_ids"][0]) == []


async def test_a_double_click_saves_one_row(rdb, rclient, tmp_path):
    f = await seed_frame(rdb, tmp_path, boxes=BOX)
    await make_user(rdb, "alice")
    login(rclient, "alice")
    body = _form(f["event_ids"][0], corrected_name="Seriola zonata", confidence="certain")
    assert rclient.post("/api/corrections", headers=CSRF, json=body).status_code == 200
    assert rclient.post("/api/corrections", headers=CSRF, json=body).status_code == 200
    assert len(await _rows(rdb, f["event_ids"][0])) == 1


async def test_a_too_long_note_is_a_400_not_a_500(rdb, rclient, tmp_path):
    f = await seed_frame(rdb, tmp_path, boxes=BOX)
    await make_user(rdb, "alice")
    login(rclient, "alice")
    r = rclient.post("/api/corrections", headers=CSRF, json=_form(
        f["event_ids"][0], not_a_fish=True, notes="x" * 3000))
    assert r.status_code == 400 and r.json()["error"]


@pytest.fixture
def spotty_client(rdb, review_assets, monkeypatch):
    """rclient, but every first answer through the review page is spot-checked."""
    from app.main import build_app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    for key, value in review_assets["env"].items():
        monkeypatch.setenv(key, value)
    # After the loop, so this wins over review_assets["env"]'s pinned 0
    # (follow-up F1): this fixture exists to force spot-checks, not skip
    # them.
    monkeypatch.setenv("REVIEW_SPOTCHECK_RATE", "1.0")
    with TestClient(build_app()) as client:
        yield client


async def test_the_old_form_is_never_spot_checked(rdb, spotty_client, tmp_path):
    f = await seed_frame(rdb, tmp_path, boxes=BOX)
    g = await seed_frame(rdb, tmp_path, frame_id=2, boxes=BOX)
    await make_user(rdb, "alice")
    login(spotty_client, "alice")
    assert spotty_client.post("/api/corrections", headers=CSRF, json=_form(
        f["event_ids"][0], not_a_fish=True)).status_code == 200
    # the control: the review page's own no-item answer is spot-checked at this rate
    assert spotty_client.post("/api/review/verdicts", headers=CSRF, json={
        "event_id": g["event_ids"][0], "outcome": "no_animal"}).json()["spot_check_scheduled"]
    async with rdb.connection() as conn:
        cur = await conn.execute("SELECT event_id FROM review_items WHERE pass = 2")
        assert [r[0] for r in await cur.fetchall()] == [g["event_ids"][0]]
