"""Frames by key, through FRAMES_URL.

saved_frames.image_path holds a key (<location>/YYYY/MM/DD/HH/<name>.jpg) for
frames written since storage moved behind URLs; rows from before hold an
absolute path on the machine that wrote them, which still opens here.
"""

from __future__ import annotations

import io
import os

from common.storage import exists, join, open_read

DEFAULT_FRAMES_URL = "file:///data/frames"


def frames_url() -> str:
    return os.environ.get("FRAMES_URL", "").strip() or DEFAULT_FRAMES_URL


def frame_url(image_path: str) -> str:
    return f"file://{image_path}" if image_path.startswith("/") else join(frames_url(), image_path)


def open_frame(image_path: str) -> io.BytesIO:
    """The frame's bytes, ready for PIL; FileNotFoundError when it is gone."""
    return io.BytesIO(open_read(frame_url(image_path)))


def frame_exists(image_path: str) -> bool:
    return exists(frame_url(image_path))
