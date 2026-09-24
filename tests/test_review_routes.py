"""The review tool over HTTP: roles, CSRF, the page, images, and the
missing-frame fallback."""
from __future__ import annotations

import io
import json
import os
from datetime import UTC, datetime, timedelta

import pytest
from PIL import Image
from review_fixtures import CSRF, T0, add_item, add_verdict, login, make_user, seed_frame

pytestmark = pytest.mark.anyio


async def _status(rdb, item_id):
    async with rdb.connection() as conn:
        cur = await conn.execute("SELECT status FROM review_items WHERE id = %s", (item_id,))
        return (await cur.fetchone())[0]


async def test_the_review_api_needs_a_reviewer(rdb, rclient):
    assert rclient.get("/api/review/next", headers=CSRF).status_code == 401
    await make_user(rdb, "watcher", role="viewer")
    login(rclient, "watcher")
    assert rclient.get("/api/review/next", headers=CSRF).status_code == 403
    await make_user(rdb, "rev")
    login(rclient, "rev")
    r = rclient.get("/api/review/next", headers=CSRF)
    assert r.status_code == 200 and r.json()["item"] is None


async def test_next_also_needs_the_csrf_header_because_it_writes(rdb, rclient):
    """GET /next sweeps lapsed claims, claims a box and retires unreadable
    ones -- every mutation carries require_csrf, and this GET is one."""
    await make_user(rdb, "rev")
    login(rclient, "rev")
    assert rclient.get("/api/review/next").status_code == 403


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
    nxt = rclient.get("/api/review/next?lane=check", headers=CSRF).json()
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
    r = rclient.get("/api/review/next?lane=check", headers=CSRF)
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


async def test_skip_release_and_reopen_over_http(rdb, rclient, tmp_path):
    """Minor (carried): skip, release and reopen had no HTTP-level coverage
    of their CSRF and role wiring."""
    await make_user(rdb, "one")
    await make_user(rdb, "two")
    f1 = await seed_frame(rdb, tmp_path, boxes=[{"bbox": [100, 100, 300, 250],
                                                 "species": "Seriola zonata"}])
    f2 = await seed_frame(rdb, tmp_path, ts=T0 + timedelta(minutes=5), frame_id=2,
                          boxes=[{"bbox": [100, 100, 300, 250], "species": "Seriola zonata"}])
    await add_item(rdb, f1, predicted="Seriola zonata", route="Seriola zonata")
    await add_item(rdb, f2, predicted="Seriola zonata", route="Seriola zonata")

    login(rclient, "one")
    nxt1 = rclient.get("/api/review/next?lane=check", headers=CSRF).json()
    claimed1 = nxt1["item"]["id"]
    assert rclient.post(f"/api/review/items/{claimed1}/skip").status_code == 403
    r = rclient.post(f"/api/review/items/{claimed1}/skip", headers=CSRF)
    assert r.status_code == 200
    assert await _status(rdb, claimed1) == "open"

    nxt2 = rclient.get("/api/review/next?lane=check", headers=CSRF).json()
    claimed2 = nxt2["item"]["id"]
    assert claimed2 != claimed1          # the skipped box is not offered again
    assert rclient.post(f"/api/review/items/{claimed2}/release").status_code == 403
    r = rclient.post(f"/api/review/items/{claimed2}/release", headers=CSRF)
    assert r.status_code == 200
    assert await _status(rdb, claimed2) == "open"

    login(rclient, "two")
    nxt = rclient.get("/api/review/next?lane=check", headers=CSRF).json()
    body = {"event_id": nxt["item"]["event_id"], "review_item_id": nxt["item"]["id"],
            "outcome": "accept"}
    v = rclient.post("/api/review/verdicts", json=body, headers=CSRF)
    assert v.status_code == 200

    r = rclient.get(f"/api/review/boxes/{nxt['item']['event_id']}")
    assert r.status_code == 200
    reopened = r.json()
    assert reopened["mine"]["label"] == "Seriola zonata"
    assert reopened["editable"] is True and reopened["reason"] is None


async def test_a_pass_2_payload_through_next_carries_no_model_information(rdb, rclient, tmp_path):
    """Review Focus 2, over HTTP: a blind item served through the real
    endpoint carries no guess, no hints, no species it counts toward, no
    earlier verdict, and a null lane (Important 6)."""
    one = await make_user(rdb, "one")
    await make_user(rdb, "two")
    f = await seed_frame(rdb, tmp_path, boxes=[{"bbox": [100, 100, 300, 250],
                                                "species": "Seriola zonata"}])
    first = await add_verdict(rdb, f["event_ids"][0], one, name="Seriola zonata")
    await add_item(rdb, f, pass_=2, exclude_user_id=one, first_correction_id=first,
                   predicted="Seriola zonata", route="Seriola zonata",
                   hints=[{"name": "Seriola zonata", "p": 0.9}],
                   added_at=datetime.now(UTC) - timedelta(hours=49))
    login(rclient, "two")
    r = rclient.get("/api/review/next?lane=check", headers=CSRF)
    assert r.status_code == 200
    body = r.json()
    assert body["item"]["pass"] == 2 and body["item"]["lane"] is None
    assert body["model"] is None and body["hints"] is None and body["counts_toward"] is None
    assert "mine" not in body    # no sign of the earlier verdict either
    text = json.dumps(body)
    for word in ("Seriola", "zonata", "Rudderfish"):
        assert word not in text


async def test_a_duplicate_verdict_does_not_double_count_progress(rdb, rclient, tmp_path,
                                                                    monkeypatch):
    """Finding 2's side effect (round 2, item 5a): a dedup hit must not
    call note_verified a second time."""
    calls = []
    monkeypatch.setattr("app.review_pool.ReviewPool.note_verified",
                        lambda self, name: calls.append(name))
    await make_user(rdb, "rev")
    login(rclient, "rev")
    f = await seed_frame(rdb, tmp_path, boxes=[{"bbox": [100, 100, 300, 250],
                                                "species": "Seriola zonata"}])
    body = {"event_id": f["event_ids"][0], "outcome": "accept"}
    r1 = rclient.post("/api/review/verdicts", json=body, headers=CSRF)
    assert r1.status_code == 200 and r1.json()["duplicate"] is False
    r2 = rclient.post("/api/review/verdicts", json=body, headers=CSRF)
    assert r2.status_code == 200 and r2.json()["duplicate"] is True
    assert calls == ["Seriola zonata"]
