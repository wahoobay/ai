from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(key: str, default: bool = False) -> bool:
    v = os.environ.get(key)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _int(key: str, default: int) -> int:
    return int(os.environ.get(key, default))


def _float(key: str, default: float) -> float:
    return float(os.environ.get(key, default))


def _str(key: str, default: str) -> str:
    return os.environ.get(key, default)


@dataclass(frozen=True)
class Config:
    # video source
    video_source: str
    video_loop: bool
    playlist_shuffle: bool
    realtime_pacing: bool

    # models
    detector_path: str
    classifier_path: str
    classifier_inference_module_dir: str
    device: str
    det_imgsz: int
    det_conf_threshold: float
    det_iou_threshold: float
    classifier_topk: int
    classifier_method: str
    classifier_input_h: int
    classifier_input_w: int
    min_accept_accuracy: float

    # persistence
    database_url: str
    events_log_dir: str
    frames_dir: str
    save_timelapse_seconds: int
    save_per_detection: bool
    save_interesting_only: bool
    save_interesting_quiet_seconds: int
    save_interesting_min_conf: float

    # http
    worker_http_host: str
    worker_http_port: int
    log_level: str
    jpeg_quality: int
    live_stream_max_fps: int

    # drift monitor cadence (frames between frame_stats samples; ~1 Hz at 30 fps = 30)
    frame_stats_every_n_frames: int

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            video_source=_str("VIDEO_SOURCE", "file:///data/test_videos"),
            video_loop=_bool("VIDEO_LOOP", True),
            playlist_shuffle=_bool("PLAYLIST_SHUFFLE", False),
            realtime_pacing=_bool("REALTIME_PACING", True),
            detector_path=_str("DETECTOR_PATH", "/data/models/detector_v26/model.pt"),
            classifier_path=_str("CLASSIFIER_PATH", "/data/models/classifier_v0_10_2/model.pt"),
            classifier_inference_module_dir=_str(
                "CLASSIFIER_INFERENCE_MODULE_DIR",
                "/data/models/classifier_v0_10_2",
            ),
            device=_str("DEVICE", "cuda:0"),
            det_imgsz=_int("DET_IMGSZ", 640),
            det_conf_threshold=_float("DET_CONF_THRESHOLD", 0.25),
            det_iou_threshold=_float("DET_IOU_THRESHOLD", 0.45),
            classifier_topk=_int("CLASSIFIER_TOPK", 3),
            classifier_method=_str("CLASSIFIER_METHOD", "natural_centroid"),
            classifier_input_h=_int("CLASSIFIER_INPUT_H", 154),
            classifier_input_w=_int("CLASSIFIER_INPUT_W", 434),
            min_accept_accuracy=_float("MIN_ACCEPT_ACCURACY", 0.0),
            database_url=_str(
                "DATABASE_URL",
                "postgresql://wahoobay:wahoobay@localhost:5432/wahoobay",
            ),
            events_log_dir=_str("EVENTS_LOG_DIR", "/data/logs/events"),
            frames_dir=_str("FRAMES_DIR", "/data/frames"),
            save_timelapse_seconds=_int("SAVE_TIMELAPSE_SECONDS", 0),
            save_per_detection=_bool("SAVE_PER_DETECTION", False),
            save_interesting_only=_bool("SAVE_INTERESTING_ONLY", True),
            save_interesting_quiet_seconds=_int("SAVE_INTERESTING_QUIET_SECONDS", 300),
            save_interesting_min_conf=_float("SAVE_INTERESTING_MIN_CONF", 0.5),
            worker_http_host=_str("WORKER_HTTP_HOST", "0.0.0.0"),
            worker_http_port=_int("WORKER_HTTP_PORT", 8081),
            log_level=_str("LOG_LEVEL", "INFO"),
            jpeg_quality=_int("JPEG_QUALITY", 80),
            live_stream_max_fps=_int("LIVE_STREAM_MAX_FPS", 15),
            frame_stats_every_n_frames=_int("FRAME_STATS_EVERY_N_FRAMES", 30),
        )
