"""PTZ position poller (stub-mode by default).

When enabled, polls the camera's pan / tilt / zoom every PTZ_POLL_INTERVAL_S
seconds and writes each sample to the ``ptz_states`` table. Joined to
``detection_events`` on (source_name, nearest ts) at query time to give every
detection an absolute look-direction.

Two backends:

  - **vapix** (Axis CGI) — `GET .../axis-cgi/com/ptz.cgi?query=position`
    returns plain text like ``pan=12.3\\ntilt=-4.5\\nzoom=6789``. Needs an
    Operator-or-higher account on the camera.
  - **onvif** (placeholder) — Profile S/G ``GetStatus`` would return the same
    info via SOAP. Not implemented yet; will land when we have an ONVIF
    client dependency in the worker image.

Disabled by default. To turn on, set:

    PTZ_POLL_ENABLED=true
    PTZ_POLL_URL=https://root:secret@host/axis-cgi/com/ptz.cgi?query=position&camera=1
    PTZ_POLL_BACKEND=vapix
    PTZ_SOURCE_NAME=seahivecam     # label for ptz_states.source_name
    PTZ_POLL_INTERVAL_S=1.0

Until URL is provided the poller logs once and stays idle (no DB writes,
no retries) — safe to leave running.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)


@dataclass
class PTZPollerStats:
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    poll_count:    int = 0
    success_count: int = 0
    last_success_at: Optional[datetime] = None
    last_pan_deg:  Optional[float] = None
    last_tilt_deg: Optional[float] = None
    last_zoom:     Optional[float] = None
    last_error:    str = ""
    enabled:       bool = False


class PTZPoller:
    def __init__(self, cfg, pg) -> None:
        self.cfg = cfg
        self.pg  = pg
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.stats = PTZPollerStats(enabled=cfg.ptz_poll_enabled and bool(cfg.ptz_poll_url))

    def start(self) -> None:
        if not self.cfg.ptz_poll_enabled:
            log.info("PTZ poller disabled by config")
            return
        if not self.cfg.ptz_poll_url:
            log.info("PTZ poller enabled but no PTZ_POLL_URL set; staying idle")
            return
        self._thread = threading.Thread(target=self._run, name="ptz-poller", daemon=True)
        self._thread.start()
        log.info(
            "PTZ poller started (backend=%s, interval=%.1fs, source=%s)",
            self.cfg.ptz_poll_backend, self.cfg.ptz_poll_interval_s,
            self.cfg.ptz_source_name,
        )

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        cfg = self.cfg
        with httpx.Client(timeout=httpx.Timeout(cfg.ptz_poll_timeout_s), verify=False) as client:
            while not self._stop.is_set():
                t0 = time.monotonic()
                self._tick(client)
                elapsed = time.monotonic() - t0
                self._stop.wait(max(0.0, cfg.ptz_poll_interval_s - elapsed))

    def _tick(self, client: httpx.Client) -> None:
        cfg = self.cfg
        self.stats.poll_count += 1
        try:
            if cfg.ptz_poll_backend == "vapix":
                state = self._poll_vapix(client)
            elif cfg.ptz_poll_backend == "onvif":
                self.stats.last_error = "onvif backend not implemented"
                return
            else:
                self.stats.last_error = f"unknown backend: {cfg.ptz_poll_backend}"
                return
        except Exception as e:
            self.stats.last_error = f"poll: {type(e).__name__}: {e}"
            log.warning("PTZ poll failed: %s", self.stats.last_error)
            return

        if state is None:
            return

        ts = datetime.now(timezone.utc)
        try:
            self.pg.record_ptz_state(
                ts=ts,
                source_name=cfg.ptz_source_name,
                pan_deg=state.get("pan_deg"),
                tilt_deg=state.get("tilt_deg"),
                zoom=state.get("zoom"),
                raw=state.get("raw"),
                poll_method=cfg.ptz_poll_backend,
            )
        except Exception:
            log.exception("PTZ db insert failed")
            return

        self.stats.success_count += 1
        self.stats.last_success_at = ts
        self.stats.last_pan_deg  = state.get("pan_deg")
        self.stats.last_tilt_deg = state.get("tilt_deg")
        self.stats.last_zoom     = state.get("zoom")
        self.stats.last_error    = ""

    @staticmethod
    def _poll_vapix(client: httpx.Client) -> Optional[dict]:
        # cfg.ptz_poll_url contains creds + path; httpx handles userinfo natively
        # (the caller passes the full URL string into ptz_poll_url).
        # We need the URL on the instance; read via `self`. Method is static for
        # readability of the parser only — call below uses `self.cfg.ptz_poll_url`.
        raise NotImplementedError  # see _poll_vapix_impl below

    # (keeping _poll_vapix as instance method for actual use)


def _parse_vapix_response(text: str) -> dict:
    """Axis returns plain text k=v lines: pan, tilt, zoom, focus, iris, ..."""
    parsed: dict = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip().lower()
        v = v.strip()
        try:
            parsed[k] = float(v)
        except ValueError:
            parsed[k] = v
    return parsed


def _vapix_state(client: httpx.Client, url: str) -> Optional[dict]:
    r = client.get(url)
    if r.status_code != 200:
        return None
    parsed = _parse_vapix_response(r.text)
    pan = parsed.get("pan")
    tilt = parsed.get("tilt")
    zoom = parsed.get("zoom")
    return {
        "pan_deg":  float(pan)  if isinstance(pan,  (int, float)) else None,
        "tilt_deg": float(tilt) if isinstance(tilt, (int, float)) else None,
        "zoom":     float(zoom) if isinstance(zoom, (int, float)) else None,
        "raw": parsed,
    }


# Wire the static-method placeholder to the actual implementation
def _poll_vapix(self, client: httpx.Client) -> Optional[dict]:
    return _vapix_state(client, self.cfg.ptz_poll_url)


PTZPoller._poll_vapix = _poll_vapix  # type: ignore[attr-defined]
