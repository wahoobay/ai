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


class AutoswitchSource(VideoSource):
    """Switches between a primary and fallback source based on the primary's
    rolling mean-luma. When primary goes dark for sustained period (e.g.,
    sunset, camera off), serve fallback frames; when it brightens up again
    (sunrise), switch back. Hysteresis prevents flapping at the threshold.

    Always reads a frame from primary on every tick to keep the brightness
    measurement current. When in fallback mode, replaces those frames with
    fallback frames in the yielded stream — the primary frame is consumed
    (and decoded) but discarded for output.
    """

    def __init__(
        self,
        primary: VideoSource,
        fallback: VideoSource,
        *,
        dark_threshold: float = 25.0,
        light_threshold: float = 50.0,
        sample_every_n_frames: int = 15,
        window_samples: int = 60,
    ) -> None:
        from collections import deque
        self.primary = primary
        self.fallback = fallback
        self.dark_threshold = dark_threshold
        self.light_threshold = light_threshold
        self.sample_every_n_frames = max(1, sample_every_n_frames)
        self._window: "deque[float]" = deque(maxlen=max(1, window_samples))
        self._stop = threading.Event()
        # exposed state (read by /stats etc.)
        self.is_dark: bool = False
        self.last_luma: float | None = None
        self.last_avg_luma: float | None = None
        self.switches: int = 0

    def frames(self) -> Iterator[Frame]:
        primary_iter = self.primary.frames()
        fallback_iter = self.fallback.frames()
        i = 0
        while not self._stop.is_set():
            try:
                pframe = next(primary_iter)
            except StopIteration:
                log.warning("AutoswitchSource: primary exhausted")
                return
            i += 1

            # sparse brightness sampling
            if i % self.sample_every_n_frames == 0:
                _, img, _ = pframe
                small = cv2.resize(img, (160, 90), interpolation=cv2.INTER_AREA)
                # BT.601 luma on BGR
                luma = (
                    0.114 * small[..., 0]
                    + 0.587 * small[..., 1]
                    + 0.299 * small[..., 2]
                )
                m = float(luma.mean())
                self.last_luma = m
                self._window.append(m)
                if len(self._window) >= max(3, self._window.maxlen // 4):
                    avg = sum(self._window) / len(self._window)
                    self.last_avg_luma = avg
                    if not self.is_dark and avg < self.dark_threshold:
                        self.is_dark = True
                        self.switches += 1
                        log.info("autoswitch: primary went dark (avg luma %.1f), switching to fallback", avg)
                    elif self.is_dark and avg > self.light_threshold:
                        self.is_dark = False
                        self.switches += 1
                        log.info("autoswitch: primary recovered (avg luma %.1f), switching back to primary", avg)

            if self.is_dark:
                try:
                    yield next(fallback_iter)
                except StopIteration:
                    # restart fallback iterator (e.g. PlaylistSource that hit end without loop)
                    fallback_iter = self.fallback.frames()
                    try:
                        yield next(fallback_iter)
                    except StopIteration:
                        # fallback dead too — emit primary's dark frame so pipeline keeps ticking
                        yield pframe
            else:
                yield pframe

    def close(self) -> None:
        self._stop.set()
        try: self.primary.close()
        except Exception: pass
        try: self.fallback.close()
        except Exception: pass


def _build_one(cfg, spec: str) -> VideoSource:
    """Construct a single VideoSource from a URL/path string."""
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


def source_from_config(cfg) -> VideoSource:
    primary = _build_one(cfg, cfg.video_source)
    fallback_spec = (cfg.fallback_video_source or "").strip()
    if not fallback_spec:
        return primary
    fallback = _build_one(cfg, fallback_spec)
    log.info(
        "AutoswitchSource: primary=%s, fallback=%s, dark<%.1f light>%.1f",
        _strip_creds(cfg.video_source),
        _strip_creds(fallback_spec),
        cfg.autoswitch_dark_threshold,
        cfg.autoswitch_light_threshold,
    )
    return AutoswitchSource(
        primary=primary,
        fallback=fallback,
        dark_threshold=cfg.autoswitch_dark_threshold,
        light_threshold=cfg.autoswitch_light_threshold,
        sample_every_n_frames=cfg.autoswitch_sample_every_n_frames,
        window_samples=cfg.autoswitch_window_samples,
    )
