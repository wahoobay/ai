"""Frame reading helpers (review_images.py)."""
from __future__ import annotations

import os

import pytest
from app.review_images import frame_size
from review_fixtures import jpeg


def test_frame_size_notices_a_replaced_or_deleted_frame(tmp_path):
    """Phase-1 triage 146: frames deleted or replaced by hand must be seen
    as missing or changed, not served from the cache."""
    path = jpeg(tmp_path / "f.jpg", size=(1280, 720))
    assert frame_size(path) == (1280, 720)
    st = os.stat(path)
    jpeg(tmp_path / "f.jpg", size=(640, 480))
    # a later modification time, even where the clock is coarse
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert frame_size(path) == (640, 480)
    os.remove(path)
    with pytest.raises(FileNotFoundError):
        frame_size(path)
