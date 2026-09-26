"""Public exports must not publish who labelled what (spec §12.1), and a
box's frame is the one captured at the box's own timestamp (spec §12.3)."""
from __future__ import annotations

import csv
import inspect
import io
from datetime import UTC, datetime, timedelta

import pytest
from PIL import Image
from review_fixtures import (
    CELEBRITIES,
    CSRF,
    T0,
    add_item,
    add_verdict,
    login,
    make_user,
    seed_frame,
)

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


def _app_state(client) -> dict:
    """build_app keeps its state in a closure. The celebrity routes read
    state["celebrities_by_id"], which startup loads from the repo's own
    assets/celebrities.json rather than REVIEW_ASSETS_DIR, so a test that
    tags the fixture's named individual has to register it there too."""
    route = next(r for r in client.app.routes
                 if getattr(r, "path", None) == "/api/celebrities/{celebrity_id}")
    return inspect.getclosurevars(route.endpoint).nonlocals["state"]


async def test_the_celebrity_gallery_names_no_reviewer_except_to_admins(rdb, rclient, tmp_path,
                                                                         monkeypatch):
    """Final review C1: GET /api/celebrities/{id} is public, and the review
    page tags named individuals with reviewer = the volunteer's display
    name. Nobody has opted in to public credit (spec §1), so the gallery
    drops the key for everyone but admins (spec §12.1: removed, not
    blanked)."""
    bobby = CELEBRITIES["celebrities"][0]
    monkeypatch.setitem(_app_state(rclient), "celebrities_by_id", {bobby["id"]: bobby})
    frame = await seed_frame(rdb, tmp_path, boxes=[{"bbox": [100, 100, 300, 250],
                                                    "species": "Acanthurus coeruleus"}])
    await make_user(rdb, "alice")
    login(rclient, "alice")
    r = rclient.post("/api/review/verdicts", headers=CSRF, json={
        "event_id": frame["event_ids"][0], "outcome": "species",
        "label": "Acanthurus coeruleus", "label_source": "search", "celebrity_id": bobby["id"]})
    assert r.status_code == 200, r.text

    url = f"/api/celebrities/{bobby['id']}"
    # a signed-in volunteer is not an admin either
    mine = rclient.get(url).json()["gallery"]
    assert mine and all("reviewer" not in g for g in mine)
    rclient.cookies.clear()
    r = rclient.get(url)
    assert r.status_code == 200
    gallery = r.json()["gallery"]
    assert [g["event_id"] for g in gallery] == frame["event_ids"]
    assert all("reviewer" not in g for g in gallery)
    assert "Alice" not in r.text

    await make_user(rdb, "boss", role="admin")
    login(rclient, "boss")
    assert [g["reviewer"] for g in rclient.get(url).json()["gallery"]] == ["Alice"]


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


def _csv_events(text: str) -> set[int]:
    return {int(r["event_id"]) for r in csv.DictReader(io.StringIO(text))}


async def _spot_pending(rdb, tmp_path, status="open"):
    frame = await _seed_named_verdict(rdb, tmp_path)
    async with rdb.connection() as conn:
        cur = await conn.execute("SELECT id, user_id FROM detection_corrections")
        first, alice = await cur.fetchone()
    spot = await add_item(rdb, frame, pass_=2, status=status, exclude_user_id=alice,
                          first_correction_id=first)
    return frame["event_ids"][0], spot


@pytest.mark.parametrize("status", ["open", "claimed"])
async def test_a_pending_spot_checks_first_answer_stays_out_of_public_lists(rdb, rclient,
                                                                           tmp_path, status):
    event, spot = await _spot_pending(rdb, tmp_path, status)
    assert event not in {r["event_id"] for r in rclient.get("/api/corrections").json()}
    assert event not in _csv_events(rclient.get("/api/export/corrections.csv").text)
    assert event not in _csv_events(rclient.get("/api/export/labeled_corrections.csv").text)
    await make_user(rdb, "boss", role="admin")
    login(rclient, "boss")
    assert event in {r["event_id"] for r in rclient.get("/api/corrections").json()}
    assert event in _csv_events(rclient.get("/api/export/corrections.csv").text)
    rclient.cookies.clear()
    async with rdb.connection() as conn:
        await conn.execute("UPDATE review_items SET status = 'done' WHERE id = %s", (spot,))
    assert event in {r["event_id"] for r in rclient.get("/api/corrections").json()}


async def test_cant_identify_shows_as_such_in_public_outputs(rdb, rclient, tmp_path):
    frame = await seed_frame(rdb, tmp_path, boxes=[{"bbox": [100, 100, 300, 250]}])
    await add_verdict(rdb, frame["event_ids"][0], await make_user(rdb, "alice"),
                      unidentifiable=True)
    [row] = rclient.get("/api/corrections").json()
    assert row["unidentifiable"] is True and row["corrected_name"] is None
    [csv_row] = list(csv.DictReader(io.StringIO(
        rclient.get("/api/export/corrections.csv").text)))
    assert csv_row["unidentifiable"] == "True"


async def test_the_celebrity_gallery_counts_each_event_once_and_hides_a_pending_spot_check(
        rdb, rclient, tmp_path, monkeypatch):
    bobby = CELEBRITIES["celebrities"][0]
    state = _app_state(rclient)
    monkeypatch.setitem(state, "celebrities_by_id", {bobby["id"]: bobby})
    monkeypatch.setitem(state, "celebrities", [bobby])
    frame = await seed_frame(rdb, tmp_path, boxes=[{"bbox": [100, 100, 300, 250],
                                                    "species": "Acanthurus coeruleus"}])
    alice = await make_user(rdb, "alice")
    event = frame["event_ids"][0]
    async with rdb.connection() as conn:
        for _ in range(2):   # an answer, then the same volunteer changing it
            await conn.execute(
                """INSERT INTO detection_corrections (event_id, corrected_name, user_id,
                                                      celebrity_id)
                   VALUES (%s, 'Acanthurus coeruleus', %s, %s)""", (event, alice, bobby["id"]))
    detail = rclient.get(f"/api/celebrities/{bobby['id']}").json()
    assert detail["sightings"] == 1 and [g["event_id"] for g in detail["gallery"]] == [event]
    [listed] = rclient.get("/api/celebrities").json()["celebrities"]
    assert listed["sightings"] == 1
    async with rdb.connection() as conn:
        cur = await conn.execute("SELECT min(id) FROM detection_corrections")
        first = (await cur.fetchone())[0]
    await add_item(rdb, frame, pass_=2, exclude_user_id=alice, first_correction_id=first)
    assert rclient.get(f"/api/celebrities/{bobby['id']}").json()["gallery"] == []
    await make_user(rdb, "boss", role="admin")
    login(rclient, "boss")
    assert len(rclient.get(f"/api/celebrities/{bobby['id']}").json()["gallery"]) == 1


async def test_the_species_gallery_uses_the_frame_saved_at_the_detections_own_time(
        rdb, rclient, tmp_path):
    now = datetime.now(UTC)
    mine = await seed_frame(rdb, tmp_path, ts=now - timedelta(hours=2), frame_id=7,
                            boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata"}])
    # frame_id restarts with every worker run: the same camera and frame_id
    # an hour later is another frame, with no Seriola in it
    await seed_frame(rdb, tmp_path, ts=now - timedelta(hours=1), frame_id=7, boxes=[])
    r = rclient.get("/api/species/Seriola%20zonata/profile?hours=24")
    assert r.status_code == 200, r.text
    frames = r.json()["recent_frames"]
    assert [fr["image_path"] for fr in frames] == [mine["image_path"]]


async def _alert(rdb):
    async with rdb.connection() as conn:
        await conn.execute(
            """INSERT INTO alerts (name, severity, message, first_seen, last_seen,
                                   acknowledged_by, acknowledged_at)
               VALUES ('privacy_test_alert', 'warning', 'no frames for 5 min', now(), now(),
                       'Boss Person', now())""")
    # a name no SLO rule uses, so the app's own checker never resolves it


async def test_the_alert_acknowledger_is_named_to_admins_only(rdb, rclient):
    await _alert(rdb)
    r = rclient.get("/api/alerts/active")
    assert r.status_code == 200 and r.json()
    assert all("acknowledged_by" not in a for a in r.json()) and "Boss Person" not in r.text
    text = rclient.get("/api/export/alerts.csv").text
    assert "acknowledged_by" not in _header(text) and "Boss Person" not in text
    await make_user(rdb, "boss", role="admin")
    login(rclient, "boss")
    rows = rclient.get("/api/alerts/active").json()
    # The dashboard's own SLOChecker fires a critical pipeline_silence alert
    # the moment it starts against an empty test database (see conftest's
    # _isolate_network docstring), which always sorts ahead of this test's
    # own warning-severity alert -- so look ours up by name rather than
    # assuming it is rows[0].
    mine = next(a for a in rows if a["name"] == "privacy_test_alert")
    assert mine["acknowledged_by"] == "Boss Person"
    assert "Boss Person" in rclient.get("/api/export/alerts.csv").text


async def test_a_reviewer_session_is_never_treated_as_admin(rdb, rclient, tmp_path):
    """Phase-1 triage 77: a signed-in reviewer is the likeliest non-admin
    caller of every identity-filtered endpoint."""
    await _seed_named_verdict(rdb, tmp_path)
    await _alert(rdb)
    login(rclient, "alice")
    rows = rclient.get("/api/corrections").json()
    assert rows and all("reviewer" not in r and "notes" not in r for r in rows)
    assert rclient.get("/api/corrections?reviewer=Alice%20Example").status_code == 403
    for path in ("/api/export/corrections.csv", "/api/export/labeled_corrections.csv",
                 "/api/export/alerts.csv"):
        text = rclient.get(path).text
        assert not ((IDENTITY | {"acknowledged_by"}) & _header(text)), path
        assert "Alice Example" not in text and "Boss Person" not in text, path
    assert all("acknowledged_by" not in a for a in rclient.get("/api/alerts/active").json())
