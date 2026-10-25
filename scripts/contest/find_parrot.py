import csv
import json
import os
from multiprocessing import Pool

import cv2
import numpy as np

S = os.path.dirname(os.path.abspath(__file__))
MINA = 0.06 * 1280 * 720


def f(r):
    try:
        c = json.load(open(r["coco_path"]))
    except Exception:
        return None
    big = [a["bbox"] for a in c["annotations"] if a["bbox"][2] * a["bbox"][3] >= MINA]
    if not big:
        return None
    im = cv2.imread(r["image_path"], cv2.IMREAD_REDUCED_COLOR_2)
    if im is None:
        return None
    best = None
    for x, y, w, h in big:
        x, y, w, h = (int(v / 2) for v in (x, y, w, h))
        crop = im[max(0, y) : y + h, max(0, x) : x + w]
        if crop.size < 400:
            continue
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        H, Sa, V = hsv[..., 0], hsv[..., 1], hsv[..., 2]
        m = (Sa > 90) & (V > 60)
        n = m.sum()
        if n < 200:
            continue
        orange = float(((H >= 3) & (H <= 22) & m).sum() / crop[..., 0].size)
        blue = float(((H >= 85) & (H <= 125) & m).sum() / crop[..., 0].size)
        sharp = float(cv2.Laplacian(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())
        score = min(orange, blue) * np.sqrt(w * h * 4 / (1280 * 720))
        if best is None or score > best["pscore"]:
            best = dict(
                r,
                bbox=f"{x*2},{y*2},{w*2},{h*2}",
                area=round(w * h * 4 / (1280 * 720), 3),
                orange=round(orange, 3),
                blue=round(blue, 3),
                sharp=round(sharp, 1),
                pscore=round(float(score), 4),
            )
    return best


if __name__ == "__main__":
    rows = list(csv.DictReader(open(f"{S}/sample12.csv")))
    out = []
    n = 0
    with Pool(120) as p:
        for x in p.imap_unordered(f, rows, chunksize=256):
            n += 1
            if n % 200000 == 0:
                print(n, "scanned", len(out), "big-box frames", flush=True)
            if x and x["orange"] >= 0.08 and x["blue"] >= 0.15:
                out.append(x)
    out.sort(key=lambda r: -r["pscore"])
    with open(f"{S}/parrot_hits.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)
    print("done", n, "hits", len(out))
