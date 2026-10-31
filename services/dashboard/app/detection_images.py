"""A detection's image from its saved frame: the box cropped out, or the whole frame with the
box drawn on it. Synchronous (PIL and storage reads); route handlers run them in a thread.
The dashboard's reviewer views (main.py) and the public API (routes_public.py) both use them."""

from __future__ import annotations

from PIL import Image

from .frames import open_frame


def crop_roi(
    image_path: str,
    bbox: list[int],
    pad_frac: float,
    max_edge: int,
) -> Image.Image:
    """Crop a detection bbox out of a source frame with a fractional pad.

    Synchronous; run in a threadpool from the route handler. Bbox is
    [x1, y1, x2, y2] in source pixel coordinates. Pad expands the crop by
    `pad_frac * max(width, height)` on each side, clamped to image bounds.
    Output is downscaled so the longer edge is at most `max_edge` px.
    """
    x1, y1, x2, y2 = bbox
    with Image.open(open_frame(image_path)) as src:
        src.load()
        W, H = src.size
        w = max(1, x2 - x1)
        h = max(1, y2 - y1)
        pad = int(round(max(w, h) * pad_frac))
        cx1 = max(0, x1 - pad)
        cy1 = max(0, y1 - pad)
        cx2 = min(W, x2 + pad)
        cy2 = min(H, y2 + pad)
        roi = src.crop((cx1, cy1, cx2, cy2)).convert("RGB")
    rw, rh = roi.size
    longest = max(rw, rh)
    if longest > max_edge:
        scale = max_edge / longest
        roi = roi.resize((max(1, int(rw * scale)), max(1, int(rh * scale))), Image.LANCZOS)
    return roi


def resize_frame(image_path: str, max_edge: int) -> Image.Image:
    """Return the source frame downscaled so the longest edge is at most
    ``max_edge`` pixels. No bbox is drawn. Used by the reviewer modal's
    edit-bbox mode — the SVG overlay handles the bbox interactively, so
    the underlying image must be unannotated.
    """
    with Image.open(open_frame(image_path)) as src:
        src.load()
        frame = src.convert("RGB").copy()
    W, H = frame.size
    if max(W, H) > max_edge:
        scale = max_edge / max(W, H)
        frame = frame.resize((max(1, int(W * scale)), max(1, int(H * scale))), Image.LANCZOS)
    return frame


def annotate_frame(
    image_path: str,
    bbox: list[int],
    label: str | None,
    max_edge: int,
) -> Image.Image:
    """Draw the detection bbox on the full source frame for context view.

    Lets a reviewer see the scene around the fish — water clarity, other
    fish, depth — alongside the cropped ROI. Stroke + label use the
    dashboard's accent color.
    """
    from PIL import ImageDraw, ImageFont

    x1, y1, x2, y2 = bbox
    with Image.open(open_frame(image_path)) as src:
        src.load()
        frame = src.convert("RGB").copy()
    W, H = frame.size
    if max(W, H) > max_edge:
        scale = max_edge / max(W, H)
        nw, nh = max(1, int(W * scale)), max(1, int(H * scale))
        frame = frame.resize((nw, nh), Image.LANCZOS)
        x1, y1, x2, y2 = (int(c * scale) for c in (x1, y1, x2, y2))
    draw = ImageDraw.Draw(frame)
    stroke_color = (79, 193, 255)  # dashboard accent
    stroke_width = max(2, int(max(frame.size) / 400))
    for inset in range(stroke_width):
        draw.rectangle(
            [(x1 - inset, y1 - inset), (x2 + inset, y2 + inset)],
            outline=stroke_color,
        )
    if label:
        try:
            font = ImageFont.load_default(size=max(12, int(max(frame.size) / 70)))
        except (TypeError, OSError):
            font = ImageFont.load_default()
        bbox_txt = draw.textbbox((0, 0), label, font=font)
        tw = bbox_txt[2] - bbox_txt[0]
        th = bbox_txt[3] - bbox_txt[1]
        pad = 4
        ly1 = max(0, y1 - th - 2 * pad)
        draw.rectangle(
            [(x1, ly1), (x1 + tw + 2 * pad, ly1 + th + 2 * pad)],
            fill=stroke_color,
        )
        draw.text((x1 + pad, ly1 + pad), label, fill=(11, 26, 36), font=font)
    return frame
