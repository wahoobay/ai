"""The public read-only API under /api/public/: open to a signed-out visitor, cached, and free of
reviewer and personal data (spec §4.2 item 3). The new public website calls it."""

from __future__ import annotations

import inspect
import logging

import httpx
import pytest
from fastapi.testclient import TestClient
from review_fixtures import TEST_DSN

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
    for path in ["/api/public/cameras"]:
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
