import csv
import json
import os
from multiprocessing import Pool

import cv2
import numpy as np

S = os.path.dirname(os.path.abspath(__file__))
rows = list(csv.DictReader(open(f"{S}/scores.csv")))
for r in rows:
    for k in ("colorful", "sat", "val", "hue_ent"):
        r[k] = float(r[k])
rows = [r for r in rows if 60 <= r["val"] <= 215]
rows.sort(key=lambda r: -(r["colorful"] + 0.5 * r["sat"]))
rows = rows[:30000]


def feats(r):
    try:
        im = cv2.imread(r["image_path"], cv2.IMREAD_REDUCED_COLOR_2)
        if im is None:
            return None
        H, W = im.shape[:2]
        coco = json.load(open(r["coco_path"]))
        hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
        contrast = float(hsv[..., 2].std())
        mask = np.zeros((H, W), bool)
        areas = []
        for a in coco["annotations"]:
            x, y, bw, bh = (int(v / 2) for v in a["bbox"])
            x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + bw), min(H, y + bh)
            if x1 > x0 and y1 > y0:
                mask[y0:y1, x0:x1] = True
                areas.append((x1 - x0) * (y1 - y0))
        fa = float(mask.mean())
        big = max(areas) / (H * W) if areas else 0.0
        if mask.sum() > 50:
            px = im[mask].astype(np.float32)
            b, g, rr = px[:, 0], px[:, 1], px[:, 2]
            rg = rr - g
            yb = 0.5 * (rr + g) - b
            fcol = float(
                np.sqrt(rg.std() ** 2 + yb.std() ** 2)
                + 0.3 * np.sqrt(rg.mean() ** 2 + yb.mean() ** 2)
            )
            fsat = float(hsv[..., 1][mask].mean())
        else:
            fcol = fsat = 0.0
        return dict(
            r,
            contrast=round(contrast, 1),
            fish_area=round(fa, 4),
            big_fish=round(big, 4),
            fish_col=round(fcol, 2),
            fish_sat=round(fsat, 1),
        )
    except Exception:
        return None


if __name__ == "__main__":
    with Pool(96) as p:
        out = [x for x in p.imap_unordered(feats, rows, chunksize=64) if x]
    with open(f"{S}/scores2.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)
    print("done", len(out))
