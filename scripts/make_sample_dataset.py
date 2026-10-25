#!/usr/bin/env python3
"""Generate data/sample: a small, committed dataset the dashboard can show without a camera.

    python scripts/make_sample_dataset.py [--out data/sample]

Everything is synthetic -- drawn frames, invented detections, the synthetic sonde
and weather generators -- so no real footage enters git. app.seed loads it (and
shifts its timestamps to "now"); tests/test_seed.py pins its size. Deterministic:
the same seed gives the same files.

Rows: detection_events.jsonl, saved_frames.jsonl (frame_file / coco_file point into
frames/), sensor_readings.jsonl, weather_readings.jsonl, alerts.jsonl.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gen_synthetic_sensor_data as synth  # noqa: E402

END = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
SEED = 20261024
SPECIES = [  # (Latin name, species_id) -- the dashboard looks common names up by the Latin name
    ("Abudefduf saxatilis", "abudefduf_saxatilis"),
    ("Stegastes partitus", "stegastes_partitus"),
    ("Holacanthus bermudensis", "holacanthus_bermudensis"),
    ("Pomacanthus paru", "pomacanthus_paru"),
    ("Haemulon flavolineatum", "haemulon_flavolineatum"),
    ("Lutjanus griseus", "lutjanus_griseus"),
    ("Sparisoma viride", "sparisoma_viride"),
    ("Acanthurus coeruleus", "acanthurus_coeruleus"),
]
SOURCES = ("seahivecam", "pier")
FRAME_W, FRAME_H = 320, 180
FRAMES = 40
FISH_PER_FRAME = 8


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat()


def _draw_frame(rng: np.random.Generator, boxes: list[tuple[int, int, int, int]]) -> bytes:
    """A blue-green gradient with an ellipse per box: a frame-shaped picture, not a photo."""
    base = np.zeros((FRAME_H, FRAME_W, 3), dtype=np.uint8)
    for y in range(FRAME_H):
        shade = int(60 + 90 * y / FRAME_H)
        base[y, :, :] = (10, shade, int(120 + 0.5 * shade))
    im = Image.fromarray(base)
    draw = ImageDraw.Draw(im)
    for x1, y1, x2, y2 in boxes:
        colour = tuple(int(c) for c in rng.integers(120, 255, size=3))
        draw.ellipse((x1, y1, x2, y2), fill=colour, outline=(20, 20, 20))
    import io

    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=70, optimize=True)
    return buf.getvalue()


def make(out: Path) -> dict[str, int]:
    rng = np.random.default_rng(SEED)
    frames_dir = out / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    detections: list[dict] = []
    frames: list[dict] = []
    track_id = 1000
    for i in range(FRAMES):
        ts = END - timedelta(minutes=36 * (FRAMES - 1 - i))
        source = SOURCES[i % 2]
        frame_id = 50_000 + i * 7
        boxes = []
        for _ in range(FISH_PER_FRAME):
            w, h = int(rng.integers(24, 70)), int(rng.integers(14, 40))
            x1, y1 = int(rng.integers(0, FRAME_W - w)), int(rng.integers(0, FRAME_H - h))
            boxes.append((x1, y1, x1 + w, y1 + h))
        frame_file, coco_file = f"frame_{frame_id}.jpg", f"frame_{frame_id}.coco.json"
        (frames_dir / frame_file).write_bytes(_draw_frame(rng, boxes))
        annotations = []
        for n, (x1, y1, x2, y2) in enumerate(boxes):
            picks = rng.choice(len(SPECIES), size=3, replace=False)
            scores = sorted(rng.dirichlet(np.ones(3) * 0.6), reverse=True)
            topk = [
                {"name": SPECIES[p][0], "species_id": SPECIES[p][1], "accuracy": round(float(s), 3)}
                for p, s in zip(picks, scores, strict=True)
            ]
            # A fish lingers: the same track id over a couple of frames of the same camera.
            track_id += 1 if rng.random() < 0.6 else 0
            detections.append(
                {
                    "ts": _iso(ts + timedelta(milliseconds=int(n * 40))),
                    "frame_id": frame_id,
                    "source_name": source,
                    "det_conf": round(float(rng.uniform(0.35, 0.98)), 3),
                    "bbox": [x1, y1, x2, y2],
                    "topk": topk,
                    "best_name": topk[0]["name"],
                    "best_species_id": topk[0]["species_id"],
                    "best_accuracy": topk[0]["accuracy"],
                    "track_id": track_id,
                    "model_version": "sample",
                }
            )
            annotations.append(
                {
                    "id": n + 1,
                    "image_id": 1,
                    "bbox": [x1, y1, x2 - x1, y2 - y1],
                    "category": topk[0]["name"],
                }
            )
        (frames_dir / coco_file).write_text(
            json.dumps(
                {
                    "images": [
                        {"id": 1, "file_name": frame_file, "width": FRAME_W, "height": FRAME_H}
                    ],
                    "annotations": annotations,
                },
                indent=1,
            )
        )
        frames.append(
            {
                "ts": _iso(ts),
                "frame_id": frame_id,
                "source_name": source,
                "reason": "timelapse" if i % 5 else "interesting:new-species",
                "frame_file": frame_file,
                "coco_file": coco_file,
                "num_fish": len(boxes),
                "model_version": "sample",
            }
        )

    start = END - timedelta(days=3, hours=12)
    arrays = synth.synthesize(
        start.replace(tzinfo=None), END.replace(tzinfo=None), np.random.default_rng(SEED)
    )
    sensors = [
        {
            "ts": _ts(arrays["ts"][i]),
            **{k: round(float(arrays[k][i]), 3) for k in arrays if k != "ts"},
        }
        for i in range(len(arrays["ts"]))
    ]
    w_arrays = synth.synthesize_weather(
        start.replace(tzinfo=None), END.replace(tzinfo=None), np.random.default_rng(SEED + 1)
    )
    weather = []
    for i in range(0, len(w_arrays["ts"]), 4):  # every 40 minutes: a hundred-odd rows
        row = {"ts": _ts(w_arrays["ts"][i])}
        for k in w_arrays:
            if k == "ts":
                continue
            v = w_arrays[k][i]
            row[k] = int(v) if k == "weather_code" else round(float(v), 3)
        weather.append(row)
    alerts = [
        {
            "name": "drift_luma_delta",
            "severity": "warning",
            "message": "current-hour brightness differs from the 7-day baseline by 31",
            "details": {"luma_1h": 142.0, "luma_7d": 111.0},
            "first_seen": _iso(END - timedelta(hours=3)),
            "last_seen": _iso(END),
            "resolved_at": None,
        },
        {
            "name": "poller_probe_stale",
            "severity": "warning",
            "message": "sensestream: last ok 48.0 min ago; sonde HTTP 503",
            "details": {"stale": {"sensestream": "last ok 48.0 min ago"}},
            "first_seen": _iso(END - timedelta(hours=1)),
            "last_seen": _iso(END),
            "resolved_at": None,
        },
        {
            "name": "pipeline_silence",
            "severity": "critical",
            "message": "no detection_events for 6.2 min",
            "details": {"age_seconds": 372},
            "first_seen": _iso(END - timedelta(hours=20)),
            "last_seen": _iso(END - timedelta(hours=19, minutes=50)),
            "resolved_at": _iso(END - timedelta(hours=19, minutes=50)),
        },
    ]
    tables = {
        "detection_events": detections,
        "saved_frames": frames,
        "sensor_readings": sensors,
        "weather_readings": weather,
        "alerts": alerts,
    }
    for name, rows in tables.items():
        (out / f"{name}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (out / "README.md").write_text(
        "# Sample dataset\n\nSynthetic, generated by `scripts/make_sample_dataset.py` (seed "
        f"{SEED}): drawn frames, invented detections, the synthetic sonde and weather generators. "
        "`python -m app.seed` (or `make seed`) loads it into an empty database, shifting every "
        "timestamp so the newest row is now. Nothing here is real footage or a real reading.\n\n"
        + "".join(f"- `{name}.jsonl`: {len(rows)} rows\n" for name, rows in tables.items())
        + f"- `frames/`: {FRAMES} JPEG frames with COCO sidecars\n"
    )
    return {name: len(rows) for name, rows in tables.items()}


def _ts(value) -> str:
    if isinstance(value, datetime):
        return _iso(value if value.tzinfo else value.replace(tzinfo=UTC))
    seconds = int(np.datetime64(value, "s").astype("int64"))
    return _iso(datetime.fromtimestamp(seconds, UTC))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "data" / "sample"))
    args = ap.parse_args(argv)
    counts = make(Path(args.out))
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
