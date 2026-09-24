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
