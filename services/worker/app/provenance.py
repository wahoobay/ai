"""Deterministic provenance fingerprint for every run.

Computed once at worker startup from:
  - checkpoint file SHA256s
  - a canonical hash of the effective Config
  - the pipeline's git SHA (best-effort)
  - human-readable model_version pulled from the Fishial info.json files

Threaded through onto every detection_events / saved_frames / frame_stats row,
and included in the JSONL event log. Makes every record reproducible: given
a row, you can identify exactly which weights + code + settings produced it.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Provenance:
    model_version: str          # "det:v26_n3/cls:v0.10.2" etc
    detector_sha256: str
    classifier_sha256: str
    config_hash: str
    pipeline_git_sha: str

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def _sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _canonical_config_hash(cfg) -> str:
    """Hash the dataclass Config deterministically.

    Order-independent (sorted keys) and stable across equal configs.
    """
    d = dataclasses.asdict(cfg)
    # drop fields that shouldn't invalidate the hash (ports, hosts, log-level)
    for k in ("worker_http_host", "worker_http_port", "log_level",
              "events_log_dir", "frames_dir", "database_url",
              "live_stream_max_fps", "jpeg_quality"):
        d.pop(k, None)
    payload = json.dumps(d, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _model_version(detector_path: str, classifier_module_dir: str) -> str:
    """Best-effort: read info.json next to each checkpoint for a human label."""
    parts = []
    for label, pth in (
        ("det", Path(detector_path).with_name("info.json")),
        ("cls", Path(classifier_module_dir) / "info.json"),
    ):
        try:
            info = json.loads(pth.read_text())
            version = info.get("version")
            model = info.get("model")
            parts.append(f"{label}:{model or '?'}@{version or '?'}")
        except Exception:
            parts.append(f"{label}:unknown")
    return " | ".join(parts)


def _git_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if out:
            return out
    except Exception:
        pass
    return "unknown"


def compute(cfg) -> Provenance:
    try:
        det_sha = _sha256_file(cfg.detector_path)
    except Exception as e:
        log.warning("failed to hash detector checkpoint: %s", e)
        det_sha = "unknown"
    try:
        cls_sha = _sha256_file(cfg.classifier_path)
    except Exception as e:
        log.warning("failed to hash classifier checkpoint: %s", e)
        cls_sha = "unknown"

    prov = Provenance(
        model_version=_model_version(cfg.detector_path, cfg.classifier_inference_module_dir),
        detector_sha256=det_sha[:16],
        classifier_sha256=cls_sha[:16],
        config_hash=_canonical_config_hash(cfg),
        pipeline_git_sha=_git_sha(),
    )
    log.info(
        "provenance: %s | det=%s | cls=%s | cfg=%s | git=%s",
        prov.model_version, prov.detector_sha256, prov.classifier_sha256,
        prov.config_hash, prov.pipeline_git_sha,
    )
    return prov
