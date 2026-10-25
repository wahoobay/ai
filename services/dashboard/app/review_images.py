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

ZOOM_MARGIN = 0.5
ZOOM_MAX_SCALE = 3.0
ZOOM_MAX_EDGE = 960


def frame_size(path: str) -> tuple[int, int]:
    """(width, height), read from the file header. Raises OSError for a
    missing, zero-byte or corrupt file -- FileNotFoundError for a plain
    missing one, PIL's UnidentifiedImageError (an OSError subclass) for a
    zero-byte or corrupt one -- or ValueError for a header PIL parses but
    cannot size sensibly. Cached per (path, modification time), so a frame
    deleted or replaced by hand is noticed (follow-up C5, phase-1 triage
    146): a deleted one fails the stat here with FileNotFoundError."""
    return _frame_size_at(path, os.stat(path).st_mtime_ns)


@lru_cache(maxsize=4096)
def _frame_size_at(path: str, mtime_ns: int) -> tuple[int, int]:
    with Image.open(path) as im:
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


def render_zoom(path: str, region) -> bytes:
    scale = zoom_scale(region)
    with Image.open(path) as src:
        crop = src.convert("RGB").crop(tuple(region))
    w, h = crop.size
    crop = crop.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    return _jpeg(crop, 88)


def render_frame(path: str, max_edge: int) -> bytes:
    with Image.open(path) as src:
        im = src.convert("RGB")
    if max(im.size) > max_edge:
        im.thumbnail((max_edge, max_edge), Image.LANCZOS)
    return _jpeg(im, 85)
