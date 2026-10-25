"""The dashboard reads frames through FRAMES_URL by key (Plan 2a, Task 6); rows
written before this change hold absolute paths and still open."""

from __future__ import annotations

import io

from PIL import Image


def _jpeg(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 20, 30)).save(buf, format="JPEG")
    return buf.getvalue()


def test_frames_open_by_key_and_by_legacy_absolute_path(tmp_path, monkeypatch):
    from app.frames import frame_exists, open_frame
    from app.review_images import frame_size
    from common.storage import put

    monkeypatch.setenv("FRAMES_URL", f"file://{tmp_path}/frames")
    key = "reef_b/2026/10/24/15/20261024T150405Z_f0000000042_seahivecam.jpg"
    put(f"file://{tmp_path}/frames/{key}", _jpeg(8, 6), "image/jpeg")
    assert frame_exists(key) and not frame_exists("reef_b/nope.jpg")
    assert Image.open(open_frame(key)).size == (8, 6)
    assert frame_size(key) == (8, 6)
    legacy = tmp_path / "old" / "frame.jpg"
    legacy.parent.mkdir()
    legacy.write_bytes(_jpeg(5, 7))
    assert frame_exists(str(legacy)) and frame_size(str(legacy)) == (5, 7)


def test_review_tool_routes_are_absent_when_the_flag_is_off(monkeypatch):
    from app.main import build_app

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/d")
    monkeypatch.setenv("FEATURE_REVIEW_TOOL", "false")
    paths = {r.path for r in build_app().routes}
    assert not any(p.startswith(("/review", "/api/review")) for p in paths)
    monkeypatch.setenv("FEATURE_REVIEW_TOOL", "true")
    assert "/review" in {r.path for r in build_app().routes}
