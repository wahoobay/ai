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

    # sliding-window tracker for the live overlay (raw detections still flow to DB)
    tracker_enabled: bool
    tracker_window: int
    tracker_iou_threshold: float
    tracker_max_age: int
    tracker_min_hits: int
    tracker_center_alpha: float      # 0..1, higher = more responsive, less smooth
    tracker_velocity_alpha: float    # 0..1, EMA weight on new velocity measurement
    tracker_velocity_decay: float    # 0..1, per-frame velocity decay while coasting

    # Auto-switch to a fallback source when primary goes dark (sunset/camera off)
    fallback_video_source: str
    autoswitch_dark_threshold: float
    autoswitch_light_threshold: float
    autoswitch_sample_every_n_frames: int
    autoswitch_window_samples: int

    # PTZ poller (off by default; when on, writes pan/tilt/zoom to ptz_states)
    ptz_poll_enabled: bool
    ptz_poll_url: str                # full URL incl. userinfo; empty = idle
    ptz_poll_backend: str            # 'vapix' | 'onvif'
    ptz_source_name: str             # label written to ptz_states.source_name
    ptz_poll_interval_s: float
    ptz_poll_timeout_s: float

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
            tracker_enabled=_bool("TRACKER_ENABLED", True),
            tracker_window=_int("TRACKER_WINDOW_FRAMES", 5),
            tracker_iou_threshold=_float("TRACKER_IOU_THRESHOLD", 0.3),
            tracker_max_age=_int("TRACKER_MAX_AGE", 1),
            tracker_min_hits=_int("TRACKER_MIN_HITS", 1),
            tracker_center_alpha=_float("TRACKER_CENTER_ALPHA", 0.6),
            tracker_velocity_alpha=_float("TRACKER_VELOCITY_ALPHA", 0.5),
            tracker_velocity_decay=_float("TRACKER_VELOCITY_DECAY", 0.8),
            fallback_video_source=_str("FALLBACK_VIDEO_SOURCE", ""),
            autoswitch_dark_threshold=_float("AUTOSWITCH_DARK_THRESHOLD", 25.0),
            autoswitch_light_threshold=_float("AUTOSWITCH_LIGHT_THRESHOLD", 50.0),
            autoswitch_sample_every_n_frames=_int("AUTOSWITCH_SAMPLE_EVERY_N_FRAMES", 15),
            autoswitch_window_samples=_int("AUTOSWITCH_WINDOW_SAMPLES", 60),
            ptz_poll_enabled=_bool("PTZ_POLL_ENABLED", False),
            ptz_poll_url=_str("PTZ_POLL_URL", ""),
            ptz_poll_backend=_str("PTZ_POLL_BACKEND", "vapix"),
            ptz_source_name=_str("PTZ_SOURCE_NAME", "camera"),
            ptz_poll_interval_s=_float("PTZ_POLL_INTERVAL_S", 1.0),
            ptz_poll_timeout_s=_float("PTZ_POLL_TIMEOUT_S", 4.0),
        )
