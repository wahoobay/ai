"""Where frames and models live: behind URLs, with the same keys everywhere.

    file:///data/frames        local disk (compose, the DGX)
    s3://bucket/prefix         AWS; boto3 is imported only when an s3:// URL is used
    https://host/prefix        read-only (published models)

A frame's key is `<location>/YYYY/MM/DD/HH/<ts>_f<frame_id>_<source>.jpg`
(frame_key); saved_frames.image_path stores the key, and the dashboard reads
join(FRAMES_URL, key). Same key under file:// or s3://, so a database can move
between them without touching a row.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

SCHEMES = ("file", "s3", "http", "https")


def frame_key(location_id: str, ts_utc: datetime, frame_id: int, source: str) -> str:
    ts = ts_utc.astimezone(UTC) if ts_utc.tzinfo else ts_utc.replace(tzinfo=UTC)
    return f"{location_id}/{ts:%Y/%m/%d/%H}/{ts:%Y%m%dT%H%M%SZ}_f{frame_id:010d}_{source}.jpg"


def join(base_url: str, key: str) -> str:
    return f"{base_url.rstrip('/')}/{key.lstrip('/')}"


def _split(url: str):
    parts = urlsplit(url)
    if parts.scheme not in SCHEMES:
        raise ValueError(f"unsupported storage URL {url!r}: use file://, s3:// or https://")
    return parts


def _path(parts) -> Path:
    return Path(unquote(parts.path))


def _missing(exc: Exception) -> bool:
    code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
    return code in ("NoSuchKey", "NotFound", "404")


_client = None


def _s3_client():
    global _client
    if _client is None:
        import boto3  # only an s3:// URL needs it

        _client = boto3.client("s3")
    return _client


def open_read(url: str) -> bytes:
    """The object's bytes; FileNotFoundError when there is none."""
    parts = _split(url)
    if parts.scheme == "file":
        return _path(parts).read_bytes()
    if parts.scheme == "s3":
        try:
            return (
                _s3_client()
                .get_object(Bucket=parts.netloc, Key=parts.path.lstrip("/"))["Body"]
                .read()
            )
        except Exception as e:
            if _missing(e):
                raise FileNotFoundError(url) from e
            raise
    try:
        with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 - https URLs are configuration
            return response.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise FileNotFoundError(url) from e
        raise


def put(url: str, data: bytes, content_type: str) -> None:
    """Write the object (whole, atomically for file://)."""
    parts = _split(url)
    if parts.scheme == "file":
        path = _path(parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.part")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return
    if parts.scheme == "s3":
        _s3_client().put_object(
            Bucket=parts.netloc, Key=parts.path.lstrip("/"), Body=data, ContentType=content_type
        )
        return
    raise ValueError(f"cannot write to {url!r}: https:// storage is read-only")


def exists(url: str) -> bool:
    parts = _split(url)
    if parts.scheme == "file":
        return _path(parts).is_file()
    if parts.scheme == "s3":
        try:
            _s3_client().head_object(Bucket=parts.netloc, Key=parts.path.lstrip("/"))
            return True
        except Exception as e:
            if _missing(e):
                return False
            raise
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=60):  # noqa: S310
            return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise
