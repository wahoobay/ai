"""The public read-only API under /api/public/: open to a signed-out visitor, cached, and free of
reviewer and personal data (spec §4.2 item 3). The new public website calls it."""

from __future__ import annotations

import csv
import inspect
import io
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from review_fixtures import T0, TEST_DSN, add_item, add_verdict, make_user, seed_frame

pytestmark = pytest.mark.anyio
EMBED = "https://www.youtube.com/embed/abc123"
WORKER = "http://worker.test"
PIER = "http://pier.test"
# The worker's /live.json (services/worker/app/main.py): two classified boxes (one species with
# a common name, one without) and one the classifier skipped. A bbox is [x1, y1, x2, y2] in
# frame pixels, as the detector (services/worker/app/fishial.py) produces it.
LIVE = {
    "frame_id": 123,
    "ts": "2026-10-31T14:00:00+00:00",
    "source_name": "seahivecam",
    "infer_ms": 41.5,
    "detections": [
        {
            "bbox": [10, 20, 110, 220],
            "det_conf": 0.88,
            "best_name": "Abudefduf saxatilis",
            "best_species_id": "abudefduf_saxatilis",
            "best_accuracy": 0.91,
            "topk": [
                {
                    "name": "Abudefduf saxatilis",
                    "species_id": "abudefduf_saxatilis",
                    "accuracy": 0.91,
                }
            ],
        },
        {
            "bbox": [400, 300, 460, 340],
            "det_conf": 0.73,
            "best_name": "Testfish innominatus",
            "best_species_id": "testfish_innominatus",
            "best_accuracy": 0.55,
            "topk": [],
        },
        {
            "bbox": [300, 40, 360, 90],
            "det_conf": 0.41,
            "best_name": None,
            "best_species_id": None,
            "best_accuracy": None,
            "topk": [],
        },
    ],
}


# The answers the site reads as JSON, each open to a signed-out visitor.
PUBLIC_JSON_PATHS = [
    "/api/public/detections/recent",
    "/api/public/detections/summary?hours=24",
    "/api/public/species",
    "/api/public/species/Seriola%20zonata",
    "/api/public/species/counts",
]
# What a detection in /detections/recent and in a species' "recent" carries, and nothing else.
DETECTION_KEYS = {
    "id",
    "ts",
    "camera",
    "species_id",
    "latin",
    "common",
    "confidence",
    "crop",
    "frame",
}
SPECIES_KEYS = {
    "species_id",
    "latin",
    "common",
    "family",
    "order",
    "aphia_id",
    "photo",
    "sightings_total",
    "sightings_7d",
    "first_seen",
    "last_seen",
}
# Who labelled what (exports.IDENTITY_COLUMNS), and the names, note and address the tests use.
IDENTITY_RE = re.compile(
    r"reviewer|notes|user_id|session_id|acknowledged_by|alice|secret note|@", re.IGNORECASE
)
NEW_YORK = "America/New_York"


@pytest.fixture
def public_client(rdb, review_assets, monkeypatch):
    """A signed-out client of the app built like rclient's, with the camera settings a test
    names (conftest points WORKER_URL at a closed port; these tests choose their own)."""
    from app.main import build_app

    def build(**env) -> TestClient:
        monkeypatch.setenv("DATABASE_URL", TEST_DSN)
        monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
        for key, value in {**review_assets["env"], **env}.items():
            monkeypatch.setenv(key, value)
        return TestClient(build_app())

    return build


def fake_workers(client, monkeypatch, answers: dict) -> list[httpx.Request]:
    """Point the app's shared httpx client at canned worker answers (host -> status code,
    exception or a whole httpx.Response) and return the requests it sends. build_app keeps its state in a closure, as
    test_privacy_exports notes; the public routes read the client from it."""
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        answer = answers[request.url.host]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(answer, json=LIVE if answer == 200 else {"error": "x"})

    route = next(r for r in client.app.routes if getattr(r, "path", None) == "/api/public/live")
    state = inspect.getclosurevars(route.endpoint).nonlocals["state"]
    monkeypatch.setitem(state, "client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return sent


async def test_public_routes_answer_signed_out_with_cache_control(rclient):
    for path in ["/api/public/cameras", *PUBLIC_JSON_PATHS]:
        r = rclient.get(path)
        assert r.status_code == 200, path
        assert r.headers["cache-control"].startswith("public, max-age="), path


async def test_an_unknown_public_path_is_a_cached_404(rclient):
    for path in ("/api/public/", "/api/public/nope", "/api/public/live/extra"):
        r = rclient.get(path)
        assert r.status_code == 404, path
        assert r.headers["cache-control"] == "public, max-age=10", path
        assert r.json() == {"error": "not found"}, path


async def test_cameras_say_embed_without_a_worker(public_client):
    with public_client(WORKER_URL="", LIVE_VIDEO_EMBED_URL=EMBED) as client:
        r = client.get("/api/public/cameras")
    assert r.headers["cache-control"] == "public, max-age=10"
    [cam] = r.json()["cameras"]
    assert cam["id"] == "seahivecam" and cam["status"] == "embed"
    assert cam["embed_url"] and "autoplay=1" in cam["embed_url"]
    assert set(cam) == {"id", "status", "stream_mjpeg", "snapshot", "embed_url", "live"}
    # No worker, nothing to fetch.
    assert cam["stream_mjpeg"] is None and cam["snapshot"] is None
    assert cam["live"] == "/api/public/live?camera=seahivecam"


async def test_on_the_stream_origin_a_worker_that_answers_is_live(public_client, monkeypatch):
    """On AWS (STREAM_BASE_URL=/stream) CloudFront hands /stream/* to the worker, open to a
    signed-out visitor; the camera is live when its worker answers, offline when not."""
    with public_client(WORKER_URL=WORKER, LIVE_VIDEO_EMBED_URL="", STREAM_BASE_URL="/stream") as c:
        sent = fake_workers(c, monkeypatch, {"worker.test": 200})
        [cam] = c.get("/api/public/cameras").json()["cameras"]
        assert cam["status"] == "live" and cam["embed_url"] is None
        assert cam["stream_mjpeg"] == "/stream/stream_raw.mjpeg"
        assert cam["snapshot"] == "/stream/snapshot_raw.jpg"
        # One GET of the worker's /live.json, cut off after 2 seconds.
        assert [(q.method, str(q.url)) for q in sent] == [("GET", f"{WORKER}/live.json")]
        assert sent[0].extensions["timeout"]["read"] == 2.0
        for answer in (500, httpx.ConnectError("refused"), httpx.ReadTimeout("slow")):
            fake_workers(c, monkeypatch, {"worker.test": answer})
            [cam] = c.get("/api/public/cameras").json()["cameras"]
            assert cam["status"] == "offline", answer


async def test_behind_the_dashboards_own_proxy_a_worker_is_not_a_public_stream(
    public_client, monkeypatch
):
    """STREAM_BASE_URL=/api (the default: the DGX, a laptop) is the dashboard's proxy, behind
    sign-in: no media paths, and a worker that answers shows as the embed, or offline."""
    for embed, status in ((EMBED, "embed"), ("", "offline")):
        with public_client(
            WORKER_URL=WORKER, LIVE_VIDEO_EMBED_URL=embed, STREAM_BASE_URL="/api"
        ) as c:
            fake_workers(c, monkeypatch, {"worker.test": 200})
            [cam] = c.get("/api/public/cameras").json()["cameras"]
        assert cam["status"] == status, embed
        assert cam["stream_mjpeg"] is None and cam["snapshot"] is None, embed


async def test_the_pier_camera_is_listed_only_when_it_has_a_worker(public_client, monkeypatch):
    with public_client(
        WORKER_URL="", WORKER_URL_PIER=PIER, LIVE_VIDEO_EMBED_URL=EMBED, STREAM_BASE_URL="/stream"
    ) as c:
        sent = fake_workers(c, monkeypatch, {"pier.test": 200})
        reef, pier = c.get("/api/public/cameras").json()["cameras"]
        assert [str(q.url) for q in sent] == [f"{PIER}/live.json"]  # the pier is still probed
        live = c.get("/api/public/live?camera=pier_cam")
    assert reef["id"] == "seahivecam" and reef["status"] == "embed"
    assert reef["stream_mjpeg"] is None and reef["snapshot"] is None
    # No stream origin serves the pier camera yet, and the site's one embed is the reef
    # camera's: an answering pier worker has nothing public to show.
    assert pier == {
        "id": "pier_cam",
        "status": "offline",
        "stream_mjpeg": None,
        "snapshot": None,
        "embed_url": None,
        "live": "/api/public/live?camera=pier_cam",
    }
    assert live.status_code == 200 and live.json()["camera"] == "pier_cam"


async def test_live_without_a_worker_is_503_and_cached_briefly(public_client):
    with public_client(WORKER_URL="") as client:
        r = client.get("/api/public/live?camera=seahivecam")
    assert r.status_code == 503 and r.headers["cache-control"] == "public, max-age=10"
    assert r.json() == {"error": "no camera pipeline"}


async def test_live_for_an_unknown_camera_is_404(public_client):
    with public_client(WORKER_URL=WORKER) as client:
        for camera in ("nope", "pier_cam"):  # no WORKER_URL_PIER, so no pier camera
            r = client.get(f"/api/public/live?camera={camera}")
            assert r.status_code == 404, camera
            assert r.headers["cache-control"] == "public, max-age=10"


async def test_live_normalises_the_workers_frame(public_client, monkeypatch):
    with public_client(WORKER_URL=WORKER) as client:
        sent = fake_workers(client, monkeypatch, {"worker.test": 200})
        r = client.get("/api/public/live?camera=seahivecam")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=1"
    body = r.json()
    assert body["camera"] == "seahivecam"
    assert body["ts"] == LIVE["ts"] and body["frame_id"] == 123
    # The worker's /stats reports no frame size.
    assert body["frame"] is None
    assert body["detections"] == [
        {
            "bbox": [10, 20, 110, 220],
            "confidence": 0.91,
            "species_id": "abudefduf_saxatilis",
            "latin": "Abudefduf saxatilis",
            "common": "Sergeant Major",
        },
        {
            "bbox": [400, 300, 460, 340],
            "confidence": 0.55,
            "species_id": "testfish_innominatus",
            "latin": "Testfish innominatus",
            "common": None,  # no common name known
        },
    ]
    assert set(body) == {"camera", "ts", "frame_id", "frame", "detections"}
    assert [str(q.url) for q in sent] == [f"{WORKER}/live.json"]


async def test_live_when_the_worker_does_not_answer_is_503_and_cached_briefly(
    public_client, monkeypatch, caplog
):
    caplog.set_level(logging.DEBUG, logger="app.routes_public")
    with public_client(WORKER_URL=WORKER) as client:
        for answer in (500, httpx.ConnectError("refused")):
            fake_workers(client, monkeypatch, {"worker.test": answer})
            r = client.get("/api/public/live?camera=seahivecam")
            assert r.status_code == 503, answer
            assert r.headers["cache-control"] == "public, max-age=10"
            assert r.json() == {"error": "camera offline"}
    # A worker that does not answer leaves a trace naming its camera.
    failures = [rec for rec in caplog.records if rec.name == "app.routes_public"]
    assert len(failures) == 2 and all("seahivecam" in rec.getMessage() for rec in failures)
    assert all(rec.levelno == logging.DEBUG for rec in failures)


async def test_live_from_a_worker_answering_the_wrong_shape_is_503(public_client, monkeypatch):
    good = LIVE["detections"][0]
    no_bbox = {k: v for k, v in good.items() if k != "bbox"}
    wrong = {
        "not JSON": httpx.Response(200, text="<html>"),
        "a list": httpx.Response(200, json=[LIVE]),
        "detections not a list": httpx.Response(200, json=dict(LIVE, detections={"a": 1})),
        "a detection without bbox": httpx.Response(200, json=dict(LIVE, detections=[no_bbox])),
        "a detection not a dict": httpx.Response(200, json=dict(LIVE, detections=[good, 7])),
        "a bbox of three": httpx.Response(
            200, json=dict(LIVE, detections=[{**good, "bbox": [1, 2, 3]}])
        ),
    }
    with public_client(WORKER_URL=WORKER) as client:
        for what, answer in wrong.items():
            fake_workers(client, monkeypatch, {"worker.test": answer})
            r = client.get("/api/public/live?camera=seahivecam")
            assert r.status_code == 503, what
            assert r.headers["cache-control"] == "public, max-age=10", what
            assert r.json() == {"error": "camera offline"}, what


# ---- detections, crops, species (Task 2) -------------------------------------------------


async def seed_track(rdb, species, track_id, start, *, frames=3, source="seahivecam", p=0.8):
    """One fish followed over `frames` frames a second apart: one row of species_sightings_mat
    (a sighting once it has 3 frames). No frame of it is saved."""
    async with rdb.connection() as conn:
        for i in range(frames):
            await conn.execute(
                """INSERT INTO detection_events (ts, frame_id, source_name, det_conf, bbox, topk,
                                                 best_name, best_species_id, best_accuracy,
                                                 track_id)
                   VALUES (%s, %s, %s, 0.9, '{10,10,60,60}', '[]', %s, %s, %s, %s)""",
                (
                    start + timedelta(seconds=i),
                    90000 + track_id * 10 + i,
                    source,
                    species,
                    species,
                    p,
                    track_id,
                ),
            )


async def refresh_sightings(rdb):
    """What the jobs runner does every minute (app.jobs MATVIEWS, in its order)."""
    async with rdb.connection() as conn:
        await conn.execute("REFRESH MATERIALIZED VIEW species_sightings_mat")
        await conn.execute("REFRESH MATERIALIZED VIEW species_sightings_hourly_mat")


def with_orders(review_assets, orders: dict[str, str]) -> None:
    """Give the fixture taxonomy its orders (by family); the catalogue rebuilds on the change."""
    path = review_assets["assets"] / "taxonomy.csv"
    rows = list(csv.DictReader(path.open(newline="")))
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=list(rows[0]))
    w.writeheader()
    for row in rows:
        w.writerow({**row, "order": orders.get(row["family"], "")})
    path.write_text(out.getvalue())


def when(d: dict) -> datetime:
    return datetime.fromisoformat(d["ts"])


def single_pixel(content: bytes) -> tuple[int, int, int]:
    return Image.open(io.BytesIO(content)).convert("RGB").resize((1, 1)).getpixel((0, 0))


async def test_recent_detections_carry_no_reviewer_fields_and_link_crops(
    rdb, rclient, review_assets, tmp_path
):
    now = datetime.now(UTC)
    saved = await seed_frame(
        rdb,
        tmp_path,
        ts=now - timedelta(minutes=5),
        frame_id=10,
        boxes=[{"bbox": [100, 100, 300, 250], "species": "Seriola zonata", "p": 0.9}],
    )
    unsaved = await seed_frame(
        rdb,
        tmp_path,
        ts=now - timedelta(minutes=2),
        frame_id=11,
        save=False,
        boxes=[{"bbox": [10, 10, 90, 90], "species": "Acanthurus coeruleus", "p": 0.7}],
    )
    # A box the classifier did not name is not a detection of anything.
    await seed_frame(
        rdb, tmp_path, ts=now - timedelta(minutes=1), frame_id=12, boxes=[{"bbox": [1, 1, 9, 9]}]
    )
    r = rclient.get("/api/public/detections/recent")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=15"
    newest, older = r.json()["detections"]
    assert set(newest) == DETECTION_KEYS and set(older) == DETECTION_KEYS
    sid = saved["event_ids"][0]
    assert when(older) == saved["ts"]
    assert {k: v for k, v in older.items() if k != "ts"} == {
        "id": sid,
        "camera": "seahivecam",
        "species_id": "Seriola zonata",
        "latin": "Seriola zonata",
        "common": "Banded Rudderfish",  # not curated: the Species Log's name
        "confidence": pytest.approx(0.9),
        "crop": f"/api/public/detections/{sid}/crop.jpg",
        "frame": f"/api/public/detections/{sid}/frame.jpg",
    }
    # Its frame was never saved: nothing to link.
    assert newest["id"] == unsaved["event_ids"][0] and newest["common"] == "Blue Tang"
    assert newest["crop"] is None and newest["frame"] is None
    assert rclient.get(older["crop"]).status_code == 200


async def test_recent_detections_filter_by_window_confidence_camera_and_limit(
    rdb, rclient, tmp_path
):
    now = datetime.now(UTC)

    async def one(minutes_ago, frame_id, p=0.9, source="seahivecam"):
        f = await seed_frame(
            rdb,
            tmp_path,
            ts=now - timedelta(minutes=minutes_ago),
            frame_id=frame_id,
            source=source,
            boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata", "p": p}],
        )
        return f["event_ids"][0]

    old = await one(30 * 60, 1)  # 30 hours ago
    unsure = await one(3, 2, p=0.3)
    pier = await one(2, 3, source="pier_cam")
    reef = await one(1, 4)

    def ids(query=""):
        r = rclient.get(f"/api/public/detections/recent{query}")
        assert r.status_code == 200, r.text
        return [d["id"] for d in r.json()["detections"]]

    assert ids() == [reef, pier]  # the last 24 hours, confidence 0.5 and up, newest first
    assert ids("?hours=48") == [reef, pier, old]
    assert ids("?min_confidence=0.2") == [reef, pier, unsure]
    assert ids("?limit=1") == [reef]
    assert ids("?camera=pier_cam") == [pier]
    assert ids("?camera=seahivecam") == [reef]
    r = rclient.get("/api/public/detections/recent?camera=pier_cam")
    assert [d["camera"] for d in r.json()["detections"]] == ["pier_cam"]
    r = rclient.get("/api/public/detections/recent?camera=nope")
    assert r.status_code == 404 and r.json() == {"error": "unknown camera"}
    assert r.headers["cache-control"] == "public, max-age=10"


async def test_a_corrected_detection_shows_the_corrected_species_not_the_reviewer(
    rdb, rclient, tmp_path
):
    now = datetime.now(UTC)
    frame = await seed_frame(
        rdb,
        tmp_path,
        ts=now - timedelta(minutes=10),
        frame_id=20,
        boxes=[
            {"bbox": [10, 10, 90, 90], "species": "Seriola zonata", "p": 0.8},
            {"bbox": [100, 10, 190, 90], "species": "Seriola zonata", "p": 0.8},
            {"bbox": [200, 10, 290, 90], "species": "Seriola zonata", "p": 0.8},
            {"bbox": [300, 10, 390, 90], "species": "Pomatomus saltarix", "p": 0.8},
            {"bbox": [400, 10, 490, 90], "species": "Pomatomus saltarix", "p": 0.8},
        ],
    )
    renamed, not_a_fish, unknown, endorsed, untouched = frame["event_ids"]
    alice = await make_user(rdb, "alice")
    who = {"reviewer": "Alice Example", "notes": "secret note"}
    # An answer, then the volunteer changing it: the latest one counts.
    await add_verdict(
        rdb, renamed, alice, name="Pomacanthus paru", created_at=now - timedelta(minutes=5), **who
    )
    await add_verdict(rdb, renamed, alice, name="Acanthurus coeruleus", **who)
    await add_verdict(rdb, not_a_fish, alice, not_a_fish=True, **who)
    await add_verdict(rdb, unknown, alice, unidentifiable=True, **who)
    # Verdicts carry the accepted name; the class keeps the classifier's spelling.
    await add_verdict(rdb, endorsed, alice, name="Pomatomus saltatrix", **who)
    r = rclient.get("/api/public/detections/recent")
    by_id = {d["id"]: d for d in r.json()["detections"]}
    assert set(by_id) == {renamed, unknown, endorsed, untouched}  # not a fish: gone
    species = {i: (d["species_id"], d["latin"], d["common"]) for i, d in by_id.items()}
    assert species[renamed] == ("Acanthurus coeruleus", "Acanthurus coeruleus", "Blue Tang")
    assert species[unknown] == (None, None, None)  # a fish nobody could name
    assert species[endorsed] == ("Pomatomus saltarix", "Pomatomus saltatrix", "Bluefish")
    assert species[untouched] == species[endorsed]
    assert by_id[renamed]["confidence"] == pytest.approx(0.8)  # the model's own score
    assert not IDENTITY_RE.search(r.text)


async def test_an_answer_under_a_pending_spot_check_does_not_show(rdb, rclient, tmp_path):
    """The second volunteer sees the box's event id (exports.hide_pending_spot_checks), so until
    the spot-check is answered the box shows as the model saw it."""
    now = datetime.now(UTC)
    frame = await seed_frame(
        rdb,
        tmp_path,
        ts=now - timedelta(minutes=10),
        frame_id=30,
        boxes=[
            {"bbox": [10, 10, 90, 90], "species": "Seriola zonata", "p": 0.8},
            {"bbox": [100, 10, 190, 90], "species": "Seriola zonata", "p": 0.8},
        ],
    )
    renamed, not_a_fish = frame["event_ids"]
    alice = await make_user(rdb, "alice")
    first = await add_verdict(rdb, renamed, alice, name="Acanthurus coeruleus")
    await add_item(rdb, frame, 0, pass_=2, exclude_user_id=alice, first_correction_id=first)
    first = await add_verdict(rdb, not_a_fish, alice, not_a_fish=True)
    spot = await add_item(rdb, frame, 1, pass_=2, exclude_user_id=alice, first_correction_id=first)
    by_id = {d["id"]: d for d in rclient.get("/api/public/detections/recent").json()["detections"]}
    assert by_id[renamed]["latin"] == "Seriola zonata" and not_a_fish in by_id
    async with rdb.connection() as conn:
        await conn.execute("UPDATE review_items SET status = 'done' WHERE id = %s", (spot,))
    by_id = {d["id"]: d for d in rclient.get("/api/public/detections/recent").json()["detections"]}
    assert not_a_fish not in by_id


async def test_crop_is_immutable_cached_and_404_without_a_saved_frame(rdb, rclient, tmp_path):
    # A worker restart reused frame_id 500: the red frame is from before the restart, the
    # green one from after. The detection belongs to the red one.
    box = [{"bbox": [100, 100, 300, 250], "species": "Seriola zonata"}]
    red = await seed_frame(rdb, tmp_path, ts=T0, frame_id=500, color=(220, 10, 10), boxes=box)
    await seed_frame(
        rdb, tmp_path, ts=T0 + timedelta(hours=2), frame_id=500, color=(10, 220, 10), boxes=box
    )
    orphan = await seed_frame(
        rdb, tmp_path, ts=T0 + timedelta(hours=1), frame_id=500, save=False, boxes=box
    )
    gone = await seed_frame(rdb, tmp_path, ts=T0 + timedelta(hours=3), frame_id=501, boxes=box)
    Path(gone["image_path"]).unlink()  # saved, then lost from storage
    sizes = {}
    for kind in ("crop", "frame"):
        r = rclient.get(f"/api/public/detections/{red['event_ids'][0]}/{kind}.jpg")
        assert r.status_code == 200, (kind, r.text)
        assert r.headers["content-type"] == "image/jpeg"
        assert r.headers["cache-control"] == "public, max-age=86400, immutable"
        r_, g_, _ = single_pixel(r.content)
        assert r_ > 150 and g_ < 80, kind
        sizes[kind] = Image.open(io.BytesIO(r.content)).size
        for missing in (orphan["event_ids"][0], gone["event_ids"][0], 987654):
            r = rclient.get(f"/api/public/detections/{missing}/{kind}.jpg")
            assert r.status_code == 404, (kind, missing)
            assert r.headers["cache-control"] == "public, max-age=60"
            assert r.json() == {"error": "no saved frame"}
    # The crop is the box with a margin; the frame is the whole scene.
    assert max(sizes["crop"]) < 384 and sizes["frame"] == (1280, 720)


async def test_summary_counts_tracks_in_the_window(rdb, rclient):
    now = datetime.now(UTC)
    await seed_track(rdb, "Seriola zonata", 1, now - timedelta(minutes=30))
    await seed_track(rdb, "Seriola zonata", 2, now - timedelta(minutes=20))
    await seed_track(rdb, "Seriola zonata", 3, now - timedelta(minutes=15), source="pier_cam")
    await seed_track(rdb, "Acanthurus coeruleus", 4, now - timedelta(minutes=10))
    await seed_track(rdb, "Acanthurus coeruleus", 5, now - timedelta(minutes=5), frames=2)
    await seed_track(rdb, "Acanthurus coeruleus", 6, now - timedelta(hours=3))
    await refresh_sightings(rdb)

    def summary(query=""):
        r = rclient.get(f"/api/public/detections/summary{query}")
        assert r.status_code == 200, r.text
        assert r.headers["cache-control"] == "public, max-age=30"
        return r.json()

    body = summary()  # the last hour; a two-frame flicker is not a sighting
    assert body["hours"] == 1 and body["total"] == 4
    assert [(s["species_id"], s["count"]) for s in body["species"]] == [
        ("Seriola zonata", 3),
        ("Acanthurus coeruleus", 1),
    ]
    seriola, acanthurus = body["species"]
    assert set(seriola) == {"species_id", "latin", "common", "count", "last_seen"}
    assert seriola["latin"] == "Seriola zonata" and seriola["common"] == "Banded Rudderfish"
    last = now - timedelta(minutes=15) + timedelta(seconds=2)  # the pier track's last frame
    assert datetime.fromisoformat(seriola["last_seen"]) == last
    assert acanthurus["common"] == "Blue Tang"
    assert summary("?hours=6")["species"][1] == {**acanthurus, "count": 2}
    assert summary("?hours=6")["total"] == 5
    by_camera = summary("?camera=seahivecam")
    assert by_camera["total"] == 3
    assert [(s["species_id"], s["count"]) for s in by_camera["species"]] == [
        ("Seriola zonata", 2),
        ("Acanthurus coeruleus", 1),
    ]
    assert summary("?camera=pier_cam")["total"] == 1
    r = rclient.get("/api/public/detections/summary?camera=nope")
    assert r.status_code == 404 and r.json() == {"error": "unknown camera"}


async def test_species_list_has_taxonomy_and_lifetime_counts(rdb, rclient, review_assets):
    with_orders(review_assets, {"Carangidae": "Carangiformes", "Pomatomidae": "Scombriformes"})
    now = datetime.now(UTC)
    await seed_track(rdb, "Seriola zonata", 1, now - timedelta(hours=1))
    await seed_track(rdb, "Seriola zonata", 2, now - timedelta(days=10))
    await seed_track(rdb, "Seriola zonata", 3, now - timedelta(minutes=5), frames=2)
    await seed_track(rdb, "Pomatomus saltarix", 4, now - timedelta(hours=2))
    await seed_track(rdb, "Testfish innominatus", 5, now - timedelta(days=30))
    await refresh_sightings(rdb)
    r = rclient.get("/api/public/species")
    assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=3600"
    species = r.json()["species"]
    assert all(set(s) == SPECIES_KEYS for s in species)
    # Most seen first; then by name.
    assert [s["species_id"] for s in species] == [
        "Seriola zonata",
        "Pomatomus saltarix",
        "Testfish innominatus",
    ]
    seriola, pomatomus, testfish = species
    assert datetime.fromisoformat(seriola["first_seen"]) == now - timedelta(days=10)
    assert datetime.fromisoformat(seriola["last_seen"]) == now - timedelta(hours=1, seconds=-2)
    assert {k: seriola[k] for k in SPECIES_KEYS - {"first_seen", "last_seen"}} == {
        "species_id": "Seriola zonata",
        "latin": "Seriola zonata",
        "common": "Banded Rudderfish",
        "family": "Carangidae",
        "order": "Carangiformes",
        "aphia_id": 900005,
        "photo": {
            "url": "/api/public/species/Seriola%20zonata/photo.jpg",
            "attribution": "(c) tester, some rights reserved (CC BY-NC)",
        },
        "sightings_total": 2,
        "sightings_7d": 1,
    }
    # The classifier's misspelling, under its WoRMS accepted name; no reference photo.
    assert (pomatomus["latin"], pomatomus["common"], pomatomus["aphia_id"]) == (
        "Pomatomus saltatrix",
        "Bluefish",
        900004,
    )
    assert (pomatomus["family"], pomatomus["order"], pomatomus["photo"]) == (
        "Pomatomidae",
        "Scombriformes",
        None,
    )
    # Nothing in the catalogue knows this one.
    assert {k: testfish[k] for k in ("common", "family", "order", "aphia_id", "photo")} == {
        "common": None,
        "family": None,
        "order": None,
        "aphia_id": None,
        "photo": None,
    }
    assert (testfish["sightings_total"], testfish["sightings_7d"]) == (1, 0)
    photo = rclient.get(seriola["photo"]["url"])
    assert photo.status_code == 200 and photo.headers["content-type"] == "image/jpeg"
    assert photo.headers["cache-control"] == "public, max-age=3600"
    r = rclient.get("/api/public/species/Pomatomus%20saltarix/photo.jpg")
    assert r.status_code == 404 and r.headers["cache-control"] == "public, max-age=60"


async def test_species_profile_has_24_hours_12_months_and_recent(rdb, public_client, tmp_path):
    tz = ZoneInfo(NEW_YORK)
    now = datetime.now(UTC)
    late = (now - timedelta(days=2)).replace(hour=3, minute=30, second=0, microsecond=0)
    earlier = now - timedelta(days=45)
    starts = [late, late + timedelta(minutes=5), earlier]
    for i, start in enumerate(starts, 1):
        await seed_track(rdb, "Seriola zonata", i, start)
    await seed_track(rdb, "Acanthurus coeruleus", 9, late)
    # Fourteen saved frames of the species this week (no track: not sightings); the newest
    # twelve are its recent detections. A box a volunteer renamed is no longer this species.
    frames = [
        await seed_frame(
            rdb,
            tmp_path,
            ts=now - timedelta(hours=i + 1),
            frame_id=300 + i,
            boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata", "p": 0.9}],
        )
        for i in range(14)
    ]
    renamed = frames[0]["event_ids"][0]
    await add_verdict(rdb, renamed, await make_user(rdb, "alice"), name="Acanthurus coeruleus")
    await refresh_sightings(rdb)
    with public_client(TZ=NEW_YORK) as client:
        r = client.get("/api/public/species/Seriola%20zonata")
        listed = client.get("/api/public/species").json()["species"]
        unknown = client.get("/api/public/species/Nonesuch%20fish")
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "public, max-age=3600"
    body = r.json()
    assert set(body) == SPECIES_KEYS | {"by_hour", "by_month", "recent"}
    entry = next(s for s in listed if s["species_id"] == "Seriola zonata")
    assert {k: body[k] for k in SPECIES_KEYS} == entry
    by_hour = [0] * 24
    for start in starts:
        by_hour[start.astimezone(tz).hour] += 1
    assert body["by_hour"] == by_hour
    this_month = now.astimezone(tz).date().replace(day=1)
    months = []
    for back in range(11, -1, -1):
        y, m = divmod(this_month.year * 12 + this_month.month - 1 - back, 12)
        months.append(f"{y:04d}-{m + 1:02d}")
    expected = dict.fromkeys(months, 0)
    for start in starts:
        expected[start.astimezone(tz).strftime("%Y-%m")] += 1
    assert body["by_month"] == [{"month": k, "sightings": v} for k, v in expected.items()]
    recent = body["recent"]
    assert [d["id"] for d in recent] == [f["event_ids"][0] for f in frames[1:13]]
    assert all(set(d) == DETECTION_KEYS for d in recent)
    assert all(d["crop"] and d["latin"] == "Seriola zonata" for d in recent)
    assert unknown.status_code == 404 and unknown.json() == {"error": "unknown species"}
    assert unknown.headers["cache-control"] == "public, max-age=60"


async def test_species_counts_buckets_in_the_sites_time_zone(rdb, public_client):
    tz = ZoneInfo(NEW_YORK)
    now = datetime.now(UTC)
    # A sighting at 03:30Z is the evening before in New York (UTC-4 or -5).
    late = (now - timedelta(days=3)).replace(hour=3, minute=30, second=0, microsecond=0)
    evening = late.astimezone(tz).date()
    assert evening == late.date() - timedelta(days=1)
    track = iter(range(1, 100))
    for species, n, start in (
        ("Seriola zonata", 3, late),
        ("Acanthurus coeruleus", 2, late - timedelta(days=1)),
        ("Pomatomus saltarix", 1, late),
        ("Testfish innominatus", 1, late + timedelta(minutes=10)),
    ):
        for _ in range(n):
            await seed_track(rdb, species, next(track), start)
    await refresh_sightings(rdb)
    with public_client(TZ=NEW_YORK) as client:
        r = client.get("/api/public/species/counts?days=7&top=2")
        weeks = client.get("/api/public/species/counts?days=14&granularity=week&top=8").json()
        default = client.get("/api/public/species/counts").json()
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "public, max-age=3600"
    body = r.json()
    today = now.astimezone(tz).date()
    days = [today - timedelta(days=6 - i) for i in range(7)]
    assert (body["granularity"], body["tz"]) == ("day", NEW_YORK)
    assert body["buckets"] == [d.isoformat() for d in days]

    def on(day, n):
        return [n if d == day else 0 for d in days]

    assert body["series"] == [
        {
            "species_id": "Seriola zonata",
            "latin": "Seriola zonata",
            "common": "Banded Rudderfish",
            "counts": on(evening, 3),
        },
        {
            "species_id": "Acanthurus coeruleus",
            "latin": "Acanthurus coeruleus",
            "common": "Blue Tang",
            "counts": on(evening - timedelta(days=1), 2),
        },
    ]
    assert body["other"] == on(evening, 2)  # Pomatomus and Testfish, the same evening
    # ISO weeks, starting Monday, labelled by their Monday.
    first = today - timedelta(days=13)
    mondays = [first - timedelta(days=first.weekday())]
    while mondays[-1] + timedelta(days=7) <= today:
        mondays.append(mondays[-1] + timedelta(days=7))
    assert weeks["granularity"] == "week"
    assert weeks["buckets"] == [m.isoformat() for m in mondays]
    assert [len(s["counts"]) for s in weeks["series"]] == [len(mondays)] * 4
    assert sum(sum(s["counts"]) for s in weeks["series"]) == 7 and sum(weeks["other"]) == 0
    assert len(default["buckets"]) == 30 and len(default["series"]) == 4


async def test_no_public_response_mentions_identity_columns(rdb, rclient, tmp_path):
    now = datetime.now(UTC)
    frame = await seed_frame(
        rdb,
        tmp_path,
        ts=now - timedelta(minutes=10),
        frame_id=40,
        boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata", "p": 0.9, "track_id": 1}],
    )
    alice = await make_user(rdb, "alice")
    async with rdb.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO user_sessions (user_id, token_hash, expires_at) "
            "VALUES (%s, 'x', now() + interval '1 day') RETURNING id",
            (alice,),
        )
        session = (await cur.fetchone())[0]
    await add_verdict(
        rdb,
        frame["event_ids"][0],
        alice,
        name="Acanthurus coeruleus",
        reviewer="Alice Example",
        notes="secret note",
        session_id=session,
    )
    await seed_track(rdb, "Seriola zonata", 2, now - timedelta(minutes=30))
    await refresh_sightings(rdb)
    for path in PUBLIC_JSON_PATHS:
        r = rclient.get(path)
        assert r.status_code == 200, (path, r.text)
        assert not IDENTITY_RE.search(r.text), path


async def test_out_of_range_parameters_are_refused(rclient):
    for path in (
        "/api/public/detections/recent?limit=101",
        "/api/public/detections/recent?hours=169",
        "/api/public/detections/recent?min_confidence=1.5",
        "/api/public/detections/summary?hours=169",
        "/api/public/species/counts?days=401",
        "/api/public/species/counts?top=9",
        "/api/public/species/counts?granularity=month",
    ):
        assert rclient.get(path).status_code == 422, path
