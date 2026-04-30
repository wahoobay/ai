"""Video source abstraction.

Two kinds:
- PlaylistSource: iterate files in a directory (with optional loop + realtime pacing)
- RTSPSource: read a live stream

Both yield (frame_id, frame_bgr, source_name). frame_id is a monotonic counter
global to the source's lifetime.
"""
from __future__ import annotations

import logging
import os
import random
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterator, Tuple
from urllib.parse import urlparse

import cv2

log = logging.getLogger(__name__)

Frame = Tuple[int, "cv2.Mat", str]  # (frame_id, bgr, source_name)


class VideoSource(ABC):
    @abstractmethod
    def frames(self) -> Iterator[Frame]: ...

    @abstractmethod
    def close(self) -> None: ...


class PlaylistSource(VideoSource):
    """Loop through a directory of video files, pacing at true FPS if requested."""

    VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}

    def __init__(
        self,
        directory: str | Path,
        loop: bool = True,
        shuffle: bool = False,
        realtime_pacing: bool = True,
    ):
        self.directory = Path(directory)
        self.loop = loop
        self.shuffle = shuffle
        self.realtime_pacing = realtime_pacing
        self._stop = threading.Event()
        self._frame_id = 0

    def _list_videos(self) -> list[Path]:
        if not self.directory.exists():
            raise FileNotFoundError(f"Video directory not found: {self.directory}")
        files = sorted(
            p for p in self.directory.iterdir()
            if p.suffix.lower() in self.VIDEO_EXTS
        )
        if not files:
            raise FileNotFoundError(f"No videos in {self.directory}")
        if self.shuffle:
            random.shuffle(files)
        return files

    def frames(self) -> Iterator[Frame]:
        while not self._stop.is_set():
            files = self._list_videos()
            log.info("PlaylistSource cycling %d file(s) from %s", len(files), self.directory)
            for path in files:
                if self._stop.is_set():
                    return
                yield from self._iter_file(path)
            if not self.loop:
                return

    def _iter_file(self, path: Path) -> Iterator[Frame]:
        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            log.warning("Failed to open %s; skipping", path)
            return
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        period = 1.0 / fps
        source_name = path.stem
        log.info("PlaylistSource: %s (fps=%.2f)", source_name, fps)
        try:
            next_due = time.monotonic()
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    break
                self._frame_id += 1
                yield (self._frame_id, frame, source_name)
                if self.realtime_pacing:
                    next_due += period
                    delay = next_due - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
                    else:
                        # falling behind — reset schedule rather than sprint
                        next_due = time.monotonic()
        finally:
            cap.release()

    def close(self) -> None:
        self._stop.set()


class RTSPSource(VideoSource):
    """Single RTSP stream via OpenCV/FFmpeg. Reconnects on failure."""

    def __init__(self, url: str, source_name: str | None = None, reconnect_delay: float = 2.0):
        self.url = url
        self.source_name = source_name or urlparse(url).hostname or "rtsp"
        self.reconnect_delay = reconnect_delay
        self._stop = threading.Event()
        self._frame_id = 0

    def frames(self) -> Iterator[Frame]:
        # Encourage FFmpeg backend for RTSP and TCP transport (more reliable than UDP).
        os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
        while not self._stop.is_set():
            cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                log.warning("RTSP open failed, retry in %.1fs", self.reconnect_delay)
                self._stop.wait(self.reconnect_delay)
                continue
            log.info("RTSPSource connected: %s", self.url)
            try:
                while not self._stop.is_set():
                    ok, frame = cap.read()
                    if not ok:
                        log.warning("RTSP read failed, reconnecting")
                        break
                    self._frame_id += 1
                    yield (self._frame_id, frame, self.source_name)
            finally:
                cap.release()
            self._stop.wait(self.reconnect_delay)

    def close(self) -> None:
        self._stop.set()


class HTTPSource(VideoSource):
    """HTTP(S) video source — typically MJPEG (multipart/x-mixed-replace) or
    HLS. Used for cameras where only the HTTP admin port is exposed and the
    native RTSP port isn't reachable (common with NAT'd Axis cameras).

    Self-signed certs are tolerated (camera HTTPS certs almost always are).
    """

    def __init__(self, url: str, source_name: str | None = None, reconnect_delay: float = 2.0):
        self.url = url
        self.source_name = source_name or urlparse(url).hostname or "http"
        self.reconnect_delay = reconnect_delay
        self._stop = threading.Event()
        self._frame_id = 0

    def frames(self) -> Iterator[Frame]:
        # Tell ffmpeg not to verify the TLS cert (Axis cameras self-sign).
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "tls_verify;0"
        while not self._stop.is_set():
            cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                log.warning("HTTP source open failed, retry in %.1fs", self.reconnect_delay)
                self._stop.wait(self.reconnect_delay)
                continue
            log.info("HTTPSource connected: %s", _strip_creds(self.url))
            try:
                while not self._stop.is_set():
                    ok, frame = cap.read()
                    if not ok:
                        log.warning("HTTP read failed, reconnecting")
                        break
                    self._frame_id += 1
                    yield (self._frame_id, frame, self.source_name)
            finally:
                cap.release()
            self._stop.wait(self.reconnect_delay)

    def close(self) -> None:
        self._stop.set()


def _strip_creds(url: str) -> str:
    """Hide user:pass@ in logged URLs."""
    p = urlparse(url)
    if not p.username:
        return url
    netloc = p.hostname or ""
    if p.port:
        netloc += f":{p.port}"
    return p._replace(netloc=netloc).geturl()


def source_from_config(cfg) -> VideoSource:
    spec = cfg.video_source
    if spec.startswith("rtsp://"):
        return RTSPSource(spec)
    if spec.startswith("http://") or spec.startswith("https://"):
        return HTTPSource(spec)
    if spec.startswith("file://"):
        directory = spec[len("file://"):]
    else:
        directory = spec
    return PlaylistSource(
        directory,
        loop=cfg.video_loop,
        shuffle=cfg.playlist_shuffle,
        realtime_pacing=cfg.realtime_pacing,
    )
