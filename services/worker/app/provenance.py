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
    model_version: str  # "det:v26_n3/cls:v0.10.2" etc
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
    for k in (
        "worker_http_host",
        "worker_http_port",
        "log_level",
        "events_log_dir",
        "frames_dir",
        "database_url",
        "live_stream_max_fps",
        "jpeg_quality",
    ):
        d.pop(k, None)
    payload = json.dumps(d, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _model_version(detector_path: str, classifier_info_path: Path, backend: str) -> str:
    """Best-effort: read info.json next to each checkpoint for a human label.
    For the v2 backend the "info.json" path points at the run dir's
    config.json instead, which holds arch + img_size + split mode."""
    parts = []
    try:
        info = json.loads(Path(detector_path).with_name("info.json").read_text())
        parts.append(f"det:{info.get('model') or '?'}@{info.get('version') or '?'}")
    except Exception:
        parts.append("det:unknown")
    try:
        info = json.loads(classifier_info_path.read_text())
        if backend == "v2":
            arch = info.get("arch") or "?"
            split = info.get("split_mode") or "?"
            parts.append(f"cls:{arch}@{split}")
        else:
            parts.append(f"cls:{info.get('model') or '?'}@{info.get('version') or '?'}")
    except Exception:
        parts.append("cls:unknown")
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

    # Hash whichever classifier the active backend will load — otherwise
    # detection_events.classifier_sha256 lies about which weights produced
    # the row when we're running v2.
    backend = (getattr(cfg, "classifier_backend", "v2") or "v2").strip().lower()
    if backend == "v2":
        cls_ckpt_path = cfg.classifier_v2_checkpoint
        cls_info_path = Path(cfg.classifier_v2_checkpoint).with_name("config.json")
    else:
        cls_ckpt_path = cfg.classifier_path
        cls_info_path = Path(cfg.classifier_inference_module_dir) / "info.json"
    try:
        cls_sha = _sha256_file(cls_ckpt_path)
    except Exception as e:
        log.warning("failed to hash classifier checkpoint (%s): %s", cls_ckpt_path, e)
        cls_sha = "unknown"

    prov = Provenance(
        model_version=_model_version(cfg.detector_path, cls_info_path, backend),
        detector_sha256=det_sha[:16],
        classifier_sha256=cls_sha[:16],
        config_hash=_canonical_config_hash(cfg),
        pipeline_git_sha=_git_sha(),
    )
    log.info(
        "provenance: %s | det=%s | cls=%s | cfg=%s | git=%s",
        prov.model_version,
        prov.detector_sha256,
        prov.classifier_sha256,
        prov.config_hash,
        prov.pipeline_git_sha,
    )
    return prov
