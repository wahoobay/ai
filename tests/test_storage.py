"""Frames and models live behind URLs (Plan 2a, Task 6): file:// locally, s3:// on
AWS, the same keys either way; boto3 is only needed for s3://."""

from __future__ import annotations

import io
from datetime import UTC, datetime

import pytest
from common import storage
from common.storage import exists, frame_key, join, open_read, put


def test_frame_key_shape_is_the_same_for_file_and_s3():
    k = frame_key("wahoobay", datetime(2026, 10, 24, 15, 4, 5, tzinfo=UTC), 42, "seahivecam")
    assert k == "wahoobay/2026/10/24/15/20261024T150405Z_f0000000042_seahivecam.jpg"
    assert join("file:///data/frames", k) == "file:///data/frames/" + k
    assert join("s3://b/", k) == "s3://b/" + k
    assert join("s3://b/prefix", k) == "s3://b/prefix/" + k


def test_file_storage_roundtrip(tmp_path):
    url = f"file://{tmp_path}/a/b.jpg"
    assert not exists(url)
    put(url, b"x", "image/jpeg")
    assert open_read(url) == b"x" and exists(url)
    with pytest.raises(FileNotFoundError):
        open_read(f"file://{tmp_path}/missing.jpg")


class _NoSuchKey(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class FakeS3:
    def __init__(self):
        self.objects: dict[tuple[str, str], tuple[bytes, str]] = {}

    def put_object(self, Bucket, Key, Body, ContentType):  # noqa: N803 - boto3's spelling
        self.objects[(Bucket, Key)] = (Body, ContentType)

    def get_object(self, Bucket, Key):  # noqa: N803
        if (Bucket, Key) not in self.objects:
            raise _NoSuchKey()
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)][0])}

    def head_object(self, Bucket, Key):  # noqa: N803
        if (Bucket, Key) not in self.objects:
            raise _NoSuchKey()
        return {}


def test_s3_storage_uses_the_client_with_content_type(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(storage, "_s3_client", lambda: fake)
    put("s3://bucket/wahoobay/x.jpg", b"jpeg", "image/jpeg")
    assert fake.objects[("bucket", "wahoobay/x.jpg")] == (b"jpeg", "image/jpeg")
    assert open_read("s3://bucket/wahoobay/x.jpg") == b"jpeg"
    assert exists("s3://bucket/wahoobay/x.jpg") and not exists("s3://bucket/nope.jpg")
    with pytest.raises(FileNotFoundError):
        open_read("s3://bucket/nope.jpg")


def test_unknown_schemes_are_refused():
    with pytest.raises(ValueError):
        open_read("ftp://host/x")
    with pytest.raises(ValueError):
        put("/plain/path", b"x", "image/jpeg")
