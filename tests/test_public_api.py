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
    "/api/public/environment/latest",
    "/api/public/environment/history",
    "/api/public/exports",
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
    "latest_crop",
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


async def test_public_routes_answer_signed_out_with_cache_control(rdb, rclient):
    await seed_track(rdb, "Seriola zonata", 1, datetime.now(UTC) - timedelta(minutes=30))
    await refresh_sightings(rdb)
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
    rdb, public_client, tmp_path
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
    # Without a pier worker the site has no pier camera, here as on /live.
    with public_client(WORKER_URL_PIER="") as client:
        r = client.get("/api/public/detections/recent?camera=pier_cam")
        assert r.status_code == 404 and r.json() == {"error": "unknown camera"}
        assert client.get("/api/public/live?camera=pier_cam").status_code == 404
    with public_client(WORKER_URL_PIER=PIER) as client:

        def ids(query=""):
            r = client.get(f"/api/public/detections/recent{query}")
            assert r.status_code == 200, r.text
            return [d["id"] for d in r.json()["detections"]]

        assert ids() == [reef, pier]  # the last 24 hours, confidence 0.5 and up, newest first
        assert ids("?hours=48") == [reef, pier, old]
        assert ids("?min_confidence=0.2") == [reef, pier, unsure]
        assert ids("?limit=1") == [reef]
        assert ids("?camera=pier_cam") == [pier]
        assert ids("?camera=seahivecam") == [reef]
        r = client.get("/api/public/detections/recent?camera=pier_cam")
        assert [d["camera"] for d in r.json()["detections"]] == ["pier_cam"]
        r = client.get("/api/public/detections/recent?camera=nope")
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


async def test_summary_counts_tracks_in_the_window(rdb, public_client):
    now = datetime.now(UTC)
    await seed_track(rdb, "Seriola zonata", 1, now - timedelta(minutes=30))
    await seed_track(rdb, "Seriola zonata", 2, now - timedelta(minutes=20))
    await seed_track(rdb, "Seriola zonata", 3, now - timedelta(minutes=15), source="pier_cam")
    await seed_track(rdb, "Acanthurus coeruleus", 4, now - timedelta(minutes=10))
    await seed_track(rdb, "Acanthurus coeruleus", 5, now - timedelta(minutes=5), frames=2)
    await seed_track(rdb, "Acanthurus coeruleus", 6, now - timedelta(hours=3))
    await refresh_sightings(rdb)
    with public_client(WORKER_URL_PIER=PIER) as client:

        def summary(query=""):
            r = client.get(f"/api/public/detections/summary{query}")
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
        r = client.get("/api/public/detections/summary?camera=nope")
        assert r.status_code == 404 and r.json() == {"error": "unknown camera"}


async def test_species_list_has_taxonomy_and_lifetime_counts(rdb, rclient, review_assets, tmp_path):
    with_orders(review_assets, {"Carangidae": "Carangiformes", "Pomatomidae": "Scombriformes"})
    now = datetime.now(UTC)
    await seed_track(rdb, "Seriola zonata", 1, now - timedelta(hours=1))
    await seed_track(rdb, "Seriola zonata", 2, now - timedelta(days=10))
    await seed_track(rdb, "Seriola zonata", 3, now - timedelta(minutes=5), frames=2)
    await seed_track(rdb, "Pomatomus saltarix", 4, now - timedelta(hours=2))
    await seed_track(rdb, "Testfish innominatus", 5, now - timedelta(days=30))
    # The latest crop: the newest saved box of the species that no verdict calls not a fish.
    shown, hidden = [
        await seed_frame(
            rdb,
            tmp_path,
            ts=now - timedelta(minutes=minutes),
            frame_id=60 + minutes,
            boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata", "p": 0.9}],
        )
        for minutes in (20, 10)
    ]
    await add_verdict(rdb, hidden["event_ids"][0], await make_user(rdb, "alice"), not_a_fish=True)
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
        "latest_crop": f"/api/public/detections/{shown['event_ids'][0]}/crop.jpg",
        "sightings_total": 2,
        "sightings_7d": 1,
    }
    assert pomatomus["latest_crop"] is None and testfish["latest_crop"] is None
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
    assert photo.headers["cache-control"] == "public, max-age=86400, immutable"
    r = rclient.get("/api/public/species/Pomatomus%20saltarix/photo.jpg")
    assert r.status_code == 404 and r.headers["cache-control"] == "public, max-age=60"


async def test_species_profile_has_24_hours_12_months_and_recent(rdb, public_client, tmp_path):
    tz = ZoneInfo(NEW_YORK)
    now = datetime.now(UTC)
    late = (now - timedelta(days=2)).replace(hour=3, minute=30, second=0, microsecond=0)
    earlier = now - timedelta(days=45)
    oldest = now - timedelta(days=75)  # past the hourly rollup's 60 days
    starts = [late, late + timedelta(minutes=5), earlier, oldest]
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
    assert body["latest_crop"] == recent[0]["crop"]
    assert unknown.status_code == 404 and unknown.json() == {"error": "unknown species"}
    assert unknown.headers["cache-control"] == "public, max-age=60"


async def test_species_counts_buckets_in_the_sites_time_zone(rdb, public_client):
    tz = ZoneInfo(NEW_YORK)
    now = datetime.now(UTC)
    # A sighting at 03:30Z is the evening before in New York (UTC-4 or -5).
    late = (now - timedelta(days=3)).replace(hour=3, minute=30, second=0, microsecond=0)
    evening = late.astimezone(tz).date()
    assert evening == late.date() - timedelta(days=1)
    # And one past the hourly rollup's 60 days, also on its own evening.
    long_ago = (now - timedelta(days=75)).replace(hour=3, minute=30, second=0, microsecond=0)
    track = iter(range(1, 100))
    for species, n, start in (
        ("Seriola zonata", 1, long_ago),
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
        hundred = client.get("/api/public/species/counts?days=100&top=8").json()
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
    seriola = next(s for s in hundred["series"] if s["species_id"] == "Seriola zonata")
    by_day = dict(zip(hundred["buckets"], seriola["counts"], strict=True))
    assert by_day[(long_ago.date() - timedelta(days=1)).isoformat()] == 1
    assert by_day[evening.isoformat()] == 3 and sum(seriola["counts"]) == 4


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
    await clear_readings(rdb)
    await seed_water(rdb, now - timedelta(hours=1), water_temp_c=26.0)
    await seed_weather(rdb, now - timedelta(hours=1), air_temp_c=29.0)
    for path in PUBLIC_JSON_PATHS:
        r = rclient.get(path)
        assert r.status_code == 200, (path, r.text)
        assert not IDENTITY_RE.search(r.text), path
    [seriola] = rclient.get("/api/public/species").json()["species"]
    photo = rclient.get(seriola["photo"]["url"])  # a JPEG, and nothing else about anyone
    assert photo.status_code == 200 and photo.headers["content-type"] == "image/jpeg"
    assert not IDENTITY_RE.search(str(dict(photo.headers)))


async def test_out_of_range_parameters_are_refused(rclient):
    for path in (
        "/api/public/detections/recent?limit=101",
        "/api/public/detections/recent?hours=169",
        "/api/public/detections/recent?min_confidence=1.5",
        "/api/public/detections/summary?hours=169",
        "/api/public/species/counts?days=401",
        "/api/public/species/counts?top=9",
        "/api/public/species/counts?granularity=month",
        "/api/public/environment/history?days=801",
        "/api/public/environment/history?days=0",
        "/api/public/environment/history?max_points=2001",
        "/api/public/environment/history?max_points=9",
        "/api/public/exports/events.csv?days=401",
        "/api/public/exports/events.parquet?days=401",
        "/api/public/exports/sightings.csv?days=0",
    ):
        assert rclient.get(path).status_code == 422, path


async def test_live_and_recent_name_a_species_by_its_accepted_name(
    rdb, public_client, monkeypatch, tmp_path
):
    """The classifier's class keeps the Species Log's misspelling (Pomatomus saltarix); the
    public sees the WoRMS accepted name taxonomy.csv maps it to, and its id unchanged. A name
    the catalogue does not map passes through as given."""
    misspelt = {
        **LIVE["detections"][0],
        "best_name": "Pomatomus saltarix",
        "best_species_id": "Pomatomus saltarix",
    }
    frame = dict(LIVE, detections=[misspelt, LIVE["detections"][1]])
    await seed_frame(
        rdb,
        tmp_path,
        ts=datetime.now(UTC) - timedelta(minutes=1),
        frame_id=50,
        boxes=[{"bbox": [10, 10, 90, 90], "species": "Pomatomus saltarix", "p": 0.9}],
    )
    with public_client(WORKER_URL=WORKER) as client:
        fake_workers(client, monkeypatch, {"worker.test": httpx.Response(200, json=frame)})
        live = client.get("/api/public/live?camera=seahivecam").json()["detections"]
        [recent] = client.get("/api/public/detections/recent").json()["detections"]
    assert [(d["species_id"], d["latin"], d["common"]) for d in live] == [
        ("Pomatomus saltarix", "Pomatomus saltatrix", "Bluefish"),
        ("testfish_innominatus", "Testfish innominatus", None),
    ]
    assert (recent["species_id"], recent["latin"], recent["common"]) == (
        "Pomatomus saltarix",
        "Pomatomus saltatrix",
        "Bluefish",
    )


async def seed_saved_track(rdb, tmp_path, species, track_id, start, frame_id):
    """A track of three boxes a second apart, each in a saved frame (seed_frame's dicts)."""
    box = {"bbox": [10, 10, 90, 90], "species": species, "p": 0.9, "track_id": track_id}
    return [
        await seed_frame(
            rdb,
            tmp_path,
            ts=start + timedelta(seconds=i),
            frame_id=frame_id + i,
            boxes=[box],
        )
        for i in range(3)
    ]


async def test_a_not_a_fish_verdict_takes_its_track_out_of_every_sighting(
    rdb, public_client, tmp_path
):
    """A track whose latest visible verdict calls it not a fish is no sighting: not in the
    summary, the species list, the profile or the counts, and none of its boxes is a recent
    detection; the box called so has no crop or frame. A verdict under a pending spot-check
    does not count until the spot-check is answered."""
    now = datetime.now(UTC)
    tracks = {
        "kept": await seed_saved_track(
            rdb, tmp_path, "Seriola zonata", 1, now - timedelta(minutes=40), 100
        ),
        "gone": await seed_saved_track(
            rdb, tmp_path, "Seriola zonata", 2, now - timedelta(minutes=30), 200
        ),
        "spotted": await seed_saved_track(
            rdb, tmp_path, "Seriola zonata", 3, now - timedelta(minutes=20), 300
        ),
    }
    gone, spotted = tracks["gone"], tracks["spotted"]
    alice = await make_user(rdb, "alice")
    # A fish on one box, then not a fish on another: the track's latest verdict counts.
    await add_verdict(
        rdb,
        gone[0]["event_ids"][0],
        alice,
        name="Seriola zonata",
        created_at=now - timedelta(minutes=5),
    )
    await add_verdict(rdb, gone[1]["event_ids"][0], alice, not_a_fish=True)
    first = await add_verdict(rdb, spotted[1]["event_ids"][0], alice, not_a_fish=True)
    spot = await add_item(
        rdb, spotted[1], 0, pass_=2, exclude_user_id=alice, first_correction_id=first
    )
    await refresh_sightings(rdb)

    def among(ids: set[int]) -> set[str]:
        return {
            name for name, frames in tracks.items() if ids & {f["event_ids"][0] for f in frames}
        }

    def seen(client) -> dict:
        [entry] = client.get("/api/public/species").json()["species"]
        profile = client.get("/api/public/species/Seriola%20zonata").json()
        [series] = client.get("/api/public/species/counts?days=2").json()["series"]
        recent = client.get("/api/public/detections/recent").json()["detections"]
        return {
            "summary": client.get("/api/public/detections/summary").json()["total"],
            "list": (entry["sightings_total"], entry["sightings_7d"]),
            "by_hour": sum(profile["by_hour"]),
            "by_month": sum(m["sightings"] for m in profile["by_month"]),
            "counts": sum(series["counts"]),
            "recent": among({d["id"] for d in recent}),
            "species recent": among({d["id"] for d in profile["recent"]}),
        }

    def image(client, frame, kind) -> int:
        r = client.get(f"/api/public/detections/{frame['event_ids'][0]}/{kind}.jpg")
        assert r.status_code == 200 or r.headers["cache-control"] == "public, max-age=60"
        return r.status_code

    with public_client() as client:
        assert seen(client) == {
            "summary": 2,
            "list": (2, 2),
            "by_hour": 2,
            "by_month": 2,
            "counts": 2,
            "recent": {"kept", "spotted"},
            "species recent": {"kept", "spotted"},
        }
        for kind in ("crop", "frame"):
            assert image(client, gone[1], kind) == 404, kind
            assert image(client, spotted[1], kind) == 200, kind  # its spot-check is pending
        async with rdb.connection() as conn:
            await conn.execute("UPDATE review_items SET status = 'done' WHERE id = %s", (spot,))
        assert seen(client) == {
            "summary": 1,
            "list": (1, 1),
            "by_hour": 1,
            "by_month": 1,
            "counts": 1,
            "recent": {"kept"},
            "species recent": {"kept"},
        }
        assert image(client, spotted[1], "crop") == 404


# ---- environment and exports ----------------------------------------------------------------

# The sonde's columns and the weather station's (with the forecast layer's), as the dashboard's
# /api/water_quality and /api/weather routes return them; a reading adds its time, `ts`.
WATER_COLUMNS = {
    "water_temp_c",
    "ph",
    "do_pct",
    "chlorophyll_rfu",
    "phycoerythrin_rfu",
    "turbidity_fnu",
    "spcond_ms_cm",
}
WEATHER_COLUMNS = {
    "bar_press_hpa",
    "wind_dir_avg_deg",
    "wind_dir_min_deg",
    "wind_dir_max_deg",
    "wind_speed_avg_ms",
    "wind_speed_min_ms",
    "wind_speed_max_ms",
    "air_temp_c",
    "rel_humidity_pct",
    "rain_accum_mm",
    "rain_dur_frac_hr",
    "rain_int_mm_hr",
    "rain_peak_int_mm_hr",
    "hail_accum",
    "hail_dur",
    "hail_int",
    "hail_peak_int",
    "water_level_mm",
    "solar_rad_wm2",
    "cloud_cover_pct",
    "dew_point_c",
    "forecast_humidity_pct",
    "precip_intensity_mm_hr",
    "precip_prob_pct",
    "press_sea_level_hpa",
    "press_surface_hpa",
    "temp_apparent_c",
    "uv_index",
    "weather_code",
    "weather_description",
}
# The site's deployment in the suite (conftest's SENSESTREAM_DEPLOYMENT_URI).
SITE = "test_site"
# The exports the public may download, in the order the site lists them.
PUBLIC_EXPORTS = [
    "events",
    "species_counts",
    "sightings",
    "hourly_summary",
    "water_quality",
    "weather",
    "frame_stats",
    "tracks_timeline",
    "topk_long",
]
# The dashboard's exports that are never public: who labelled what, alerts, the saved frames'
# storage paths, the camera's metadata.
PRIVATE_EXPORTS = ["corrections", "labeled_corrections", "alerts", "saved_frames"]
EXPORTS_README = (
    Path(__file__).resolve().parents[1] / "services" / "dashboard" / "app" / "data" / "README.md"
)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")


async def clear_readings(rdb):
    """The suite's TRUNCATE leaves the sensor tables alone (the seed tests fill them)."""
    async with rdb.connection() as conn:
        await conn.execute("TRUNCATE sensor_readings, weather_readings, frame_stats")


async def seed_water(rdb, ts, deployment=SITE, source="live", **values):
    cols = ", ".join(["ts", "deployment_uri", "source", *values])
    marks = ", ".join(["%s"] * (3 + len(values)))
    async with rdb.connection() as conn:
        await conn.execute(
            f"INSERT INTO sensor_readings ({cols}) VALUES ({marks})",
            (ts, deployment, source, *values.values()),
        )


async def seed_weather(rdb, ts, deployment=SITE, source="live", **values):
    cols = ", ".join(["ts", "deployment_uri", "source", *values])
    marks = ", ".join(["%s"] * (3 + len(values)))
    async with rdb.connection() as conn:
        await conn.execute(
            f"INSERT INTO weather_readings ({cols}) VALUES ({marks})",
            (ts, deployment, source, *values.values()),
        )


async def test_environment_latest_is_the_sites_newest_reading_of_each_feed(rdb, rclient):
    """The newest sonde and weather readings of the site's deployment, every column of each,
    and nothing about where they came from but the deployment."""
    await clear_readings(rdb)
    now = datetime.now(UTC).replace(microsecond=0)
    await seed_water(rdb, now - timedelta(hours=2), water_temp_c=25.0, ph=8.0)
    await seed_water(rdb, now - timedelta(hours=1), water_temp_c=26.5, ph=8.1, do_pct=97.0)
    await seed_water(rdb, now, deployment="another_site", water_temp_c=30.0)
    await seed_weather(rdb, now - timedelta(hours=3), air_temp_c=27.0)
    await seed_weather(
        rdb,
        now - timedelta(minutes=30),
        air_temp_c=29.5,
        wind_speed_avg_ms=4.0,
        weather_code=3,
        weather_description="overcast",
    )
    r = rclient.get("/api/public/environment/latest")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=60"
    body = r.json()
    assert set(body) == {"deployment", "water", "weather"}
    assert body["deployment"] == SITE
    water, weather = body["water"], body["weather"]
    assert set(water) == {"ts", *WATER_COLUMNS}
    assert set(weather) == {"ts", *WEATHER_COLUMNS}
    assert when(water) == now - timedelta(hours=1)
    assert (water["water_temp_c"], water["ph"], water["do_pct"]) == (26.5, pytest.approx(8.1), 97.0)
    assert water["turbidity_fnu"] is None
    assert when(weather) == now - timedelta(minutes=30)
    assert weather["air_temp_c"] == 29.5 and weather["wind_speed_avg_ms"] == 4.0
    assert (weather["weather_code"], weather["weather_description"]) == (3, "overcast")


async def test_environment_without_readings_is_null_and_empty(rdb, rclient):
    await clear_readings(rdb)
    latest = rclient.get("/api/public/environment/latest")
    assert latest.status_code == 200
    assert latest.json() == {"deployment": SITE, "water": None, "weather": None}
    history = rclient.get("/api/public/environment/history")
    assert history.status_code == 200
    assert history.headers["cache-control"] == "public, max-age=600"
    assert history.json() == {"days": 7, "water": [], "weather": []}


async def test_environment_history_averages_each_bucket_of_the_window(rdb, rclient):
    """`days` back from now in at most `max_points` even buckets (the private history routes'
    rule): each point is a bucket's start and its means; the weather code and description
    are the bucket's latest. Other deployments and older readings are left out."""
    await clear_readings(rdb)
    days, max_points = 2, 10
    bucket_s = days * 24 * 3600 // max_points
    now = datetime.now(UTC)
    # A bucket that starts a day ago, wholly inside the window.
    start = datetime.fromtimestamp(
        (now - timedelta(days=1)).timestamp() // bucket_s * bucket_s, UTC
    )
    await seed_water(rdb, start + timedelta(minutes=1), water_temp_c=24.0, ph=8.0)
    await seed_water(rdb, start + timedelta(minutes=2), water_temp_c=26.0, ph=8.2)
    await seed_water(rdb, start + timedelta(minutes=3), deployment="another_site", ph=1.0)
    await seed_water(rdb, now - timedelta(days=3), water_temp_c=99.0)
    await seed_water(rdb, now - timedelta(minutes=5), water_temp_c=27.0)
    await seed_weather(
        rdb, start + timedelta(minutes=1), air_temp_c=28.0, weather_code=1, weather_description="a"
    )
    await seed_weather(rdb, start + timedelta(minutes=2), air_temp_c=30.0, weather_code=2)
    r = rclient.get(f"/api/public/environment/history?days={days}&max_points={max_points}")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=600"
    body = r.json()
    assert set(body) == {"days", "water", "weather"} and body["days"] == days
    assert all(set(p) == {"ts", *WATER_COLUMNS} for p in body["water"])
    assert all(set(p) == {"ts", *WEATHER_COLUMNS} for p in body["weather"])
    first, last = body["water"]
    assert when(first) == start and when(last) <= now
    assert first["water_temp_c"] == 25.0 and first["ph"] == pytest.approx(8.1)
    assert last["water_temp_c"] == 27.0
    [weather] = body["weather"]
    assert when(weather) == start and weather["air_temp_c"] == 29.0
    # The code is the newest reading's; the description the newest one that has one.
    assert (weather["weather_code"], weather["weather_description"]) == (2, "a")


async def test_exports_list_the_nine_public_exports_and_where_to_fetch_them(rclient):
    r = rclient.get("/api/public/exports")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=3600"
    body = r.json()
    assert set(body) == {"exports", "readme", "license"}
    assert [e["name"] for e in body["exports"]] == PUBLIC_EXPORTS
    for e in body["exports"]:
        name = e["name"]
        assert set(e) == {"name", "description", "csv", "parquet", "default_days"}, name
        assert e["description"] and "\n" not in e["description"], name
        assert e["csv"] == f"/api/public/exports/{name}.csv"
        assert e["parquet"] == f"/api/public/exports/{name}.parquet"
        assert e["default_days"] == 90
    assert body["readme"] == "/api/public/exports/README.md"
    # assets/LICENSES_AND_CREDITS.md licenses the classifier's training inputs, not this data.
    assert body["license"] is None


def test_the_licence_is_the_first_line_that_licenses_the_data(tmp_path):
    from app.routes_public import data_licence

    doc = tmp_path / "LICENSES.md"
    doc.write_text(
        "# Dataset — licences and credits\n"
        "| Source | Licence(s) |\n"
        "|---|---|\n"
        "| photos | CC-BY |\n"
        "The photos' licences are listed per photo.\n"
        "Questions about the data licence: someone@example.org\n"
        "> The exported **data** are licensed under [CC BY 4.0](https://example.org/by/4.0/).\n"
        "Data licence, second mention.\n"
    )
    assert data_licence(doc) == "The exported data are licensed under CC BY 4.0."
    assert data_licence(tmp_path / "missing.md") is None


async def test_private_exports_are_never_served_publicly(rclient):
    for name in PRIVATE_EXPORTS:
        for ext in ("csv", "parquet"):
            r = rclient.get(f"/api/public/exports/{name}.{ext}")
            assert r.status_code == 404, (name, ext)
            assert r.headers["cache-control"].startswith("public, max-age="), (name, ext)
    for path in ("/api/public/exports/camera_metadata.json", "/api/public/exports/nope.csv"):
        assert rclient.get(path).status_code == 404, path


async def test_every_public_export_downloads_without_identity(rdb, rclient, tmp_path):
    """Each of the nine as CSV and Parquet: an attachment, cached an hour, with the same rows
    in both, and no column or value that says who labelled what."""
    from app.exports import IDENTITY_COLUMNS

    await clear_readings(rdb)
    now = datetime.now(UTC)
    frame = await seed_frame(
        rdb,
        tmp_path,
        ts=now - timedelta(minutes=10),
        frame_id=40,
        boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata", "p": 0.9, "track_id": 7}],
    )
    for i in range(1, 3):
        await seed_frame(
            rdb,
            tmp_path,
            ts=now - timedelta(minutes=10) + timedelta(seconds=i),
            frame_id=40 + i,
            save=False,
            boxes=[{"bbox": [12, 12, 92, 92], "species": "Seriola zonata", "track_id": 7}],
        )
    alice = await make_user(rdb, "alice")
    await add_verdict(
        rdb,
        frame["event_ids"][0],
        alice,
        name="Acanthurus coeruleus",
        reviewer="Alice Example",
        notes="secret note",
    )
    await refresh_sightings(rdb)
    await seed_water(rdb, now - timedelta(minutes=20), water_temp_c=26.0)
    await seed_weather(rdb, now - timedelta(minutes=20), air_temp_c=29.0)
    async with rdb.connection() as conn:
        await conn.execute(
            "INSERT INTO frame_stats (ts, source_name, frame_id, mean_luma, num_detections) "
            "VALUES (%s, 'seahivecam', 40, 0.4, 1)",
            (now - timedelta(minutes=10),),
        )
    import pandas as pd

    for name in PUBLIC_EXPORTS:
        r = rclient.get(f"/api/public/exports/{name}.csv")
        assert r.status_code == 200, (name, r.text)
        assert r.headers["content-type"].startswith("text/csv"), name
        assert r.headers["content-disposition"] == f'attachment; filename="{name}.csv"'
        assert r.headers["cache-control"] == "public, max-age=3600", name
        rows = list(csv.reader(io.StringIO(r.text)))
        assert len(rows) > 1, f"{name}: expected the seeded rows"
        assert not set(rows[0]) & set(IDENTITY_COLUMNS), name
        assert not IDENTITY_RE.search(r.text), name
        p = rclient.get(f"/api/public/exports/{name}.parquet")
        assert p.status_code == 200, (name, p.text)
        assert p.headers["content-type"] == "application/vnd.apache.parquet", name
        assert p.headers["content-disposition"] == f'attachment; filename="{name}.parquet"'
        assert p.headers["cache-control"] == "public, max-age=3600", name
        frame_ = pd.read_parquet(io.BytesIO(p.content))
        assert not set(frame_.columns) & set(IDENTITY_COLUMNS), name
        assert len(frame_) == len(rows) - 1, name


async def test_an_export_covers_the_last_days_asked_for(rdb, rclient):
    await clear_readings(rdb)
    now = datetime.now(UTC)
    await seed_water(rdb, now - timedelta(days=2), water_temp_c=24.0)
    await seed_water(rdb, now - timedelta(hours=1), water_temp_c=26.0)
    await seed_water(rdb, now - timedelta(days=91), water_temp_c=20.0)

    def temperatures(query: str) -> list[str]:
        r = rclient.get(f"/api/public/exports/water_quality.csv{query}")
        assert r.status_code == 200, query
        return [row["water_temp_c"] for row in csv.DictReader(io.StringIO(r.text))]

    assert temperatures("") == ["24.0", "26.0"]
    assert temperatures("?days=1") == ["26.0"]
    assert temperatures("?days=400") == ["20.0", "24.0", "26.0"]


async def test_the_exports_readme_is_public_without_addresses(rclient, monkeypatch, tmp_path):
    """The README the dashboard's own downloads carry, less any line that names an e-mail
    address: the public API names no one's address."""
    r = rclient.get("/api/public/exports/README.md")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert r.headers["content-disposition"] == 'attachment; filename="README.md"'
    assert r.headers["cache-control"] == "public, max-age=3600"
    source = EXPORTS_README.read_text().splitlines()
    assert r.text.splitlines() == [line for line in source if not EMAIL.search(line)]
    assert not EMAIL.search(r.text)

    import app.routes_public as routes_public

    readme = tmp_path / "README.md"
    readme.write_text("# Data\n\nCite it.\nCorrections: contact someone@example.org.\nThanks.\n")
    monkeypatch.setattr(routes_public, "EXPORTS_README", readme)
    assert rclient.get("/api/public/exports/README.md").text == "# Data\n\nCite it.\nThanks.\n"
    monkeypatch.setattr(routes_public, "EXPORTS_README", tmp_path / "missing.md")
    gone = rclient.get("/api/public/exports/README.md")
    assert gone.status_code == 404 and gone.headers["cache-control"] == "public, max-age=60"
