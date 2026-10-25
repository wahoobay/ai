"""Frame and zoom images for the review page.

The zoom is the volunteer's work area (layout B): the model's box with
half a box of margin on each side, clamped to the frame, scaled up to 3x
and never past 960 px on its long edge. The page receives the region in
frame pixels so it can map drags back to frame coordinates; the zoom
endpoint recomputes the same region from the same inputs, so the two
always agree."""

from __future__ import annotations

import io
import os
from functools import lru_cache

from PIL import Image

from .frames import frame_exists, frame_url, open_frame

ZOOM_MARGIN = 0.5
ZOOM_MAX_SCALE = 3.0
ZOOM_MAX_EDGE = 960


def frame_size(image_path: str) -> tuple[int, int]:
    """(width, height), read from the file header through FRAMES_URL (a key) or a
    legacy absolute path. Raises FileNotFoundError for a missing frame, PIL's
    UnidentifiedImageError (an OSError subclass) for a zero-byte or corrupt one,
    or ValueError for a header PIL parses but cannot size sensibly. Cached per
    frame: a local file by its modification time, so one replaced by hand is
    noticed (follow-up C5, phase-1 triage 146); an object by its key, since
    objects are written once."""
    url = frame_url(image_path)
    if url.startswith("file://"):
        stamp = os.stat(url.removeprefix("file://")).st_mtime_ns
    else:
        if not frame_exists(image_path):
            raise FileNotFoundError(image_path)
        stamp = 0
    return _frame_size_at(image_path, stamp)


@lru_cache(maxsize=4096)
def _frame_size_at(image_path: str, stamp: int) -> tuple[int, int]:
    with Image.open(open_frame(image_path)) as im:
        return im.size


def zoom_region(
    bbox, width: int, height: int, margin: float = ZOOM_MARGIN
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


def render_zoom(image_path: str, region) -> bytes:
    scale = zoom_scale(region)
    with Image.open(open_frame(image_path)) as src:
        crop = src.convert("RGB").crop(tuple(region))
    w, h = crop.size
    crop = crop.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    return _jpeg(crop, 88)


def render_frame(image_path: str, max_edge: int) -> bytes:
    with Image.open(open_frame(image_path)) as src:
        im = src.convert("RGB")
    if max(im.size) > max_edge:
        im.thumbnail((max_edge, max_edge), Image.LANCZOS)
    return _jpeg(im, 85)
