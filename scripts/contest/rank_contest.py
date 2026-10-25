import csv
import datetime as dt
import json
import os
import sys

import cv2
import numpy as np

S = os.path.dirname(os.path.abspath(__file__))
N = int(sys.argv[1]) if len(sys.argv) > 1 else 20
OUT = sys.argv[2] if len(sys.argv) > 2 else f"{S}/contest"
meta = {r["id"]: r for r in csv.DictReader(open(f"{S}/random5.csv"))}
hand = {r["id"]: r for r in csv.DictReader(open(f"{S}/hand5.csv"))}
clip = {r["id"]: r for r in csv.DictReader(open(f"{S}/clip5.csv"))}
ids = [i for i in clip if i in hand]
rows = []
for i in ids:
    h = hand[i]
    c = clip[i]
    r = dict(meta[i])
    r.update({k: float(h[k]) for k in h if k != "id"})
    r.update(aes=float(c["aes"]), pos_max=float(c["pos_max"]))
    rows.append(r)
print(len(rows), "scored frames")
# hard gates: not too dark/blown out, at least one fish box, subject not tiny
rows = [r for r in rows if 50 <= r["val"] <= 220 and r["nbox"] >= 1 and r["big_fish"] >= 0.01]
print(len(rows), "after gates")


def ranks(key):
    order = sorted(range(len(rows)), key=lambda i: rows[i][key])
    rk = [0.0] * len(rows)
    for pos, i in enumerate(order):
        rk[i] = pos / len(rows)
    return rk


R = {k: ranks(k) for k in ("aes", "colorful", "contrast", "big_sharp", "big_fish", "fish_sat")}
W = {
    "aes": 0.45,
    "colorful": 0.12,
    "contrast": 0.08,
    "big_sharp": 0.15,
    "big_fish": 0.12,
    "fish_sat": 0.08,
}
for i, r in enumerate(rows):
    r["score"] = sum(W[k] * R[k][i] for k in W)
rows.sort(key=lambda r: -r["score"])
seen = set()
top = []
for r in rows:
    t = dt.datetime.fromisoformat(r["ts"])
    key = (r["source"], t.strftime("%Y%m%d%H"), t.minute // 30)
    if key in seen or sum(1 for x in top if x["ts"][:10] == r["ts"][:10]) >= 3:
        continue
    seen.add(key)
    top.append(r)
    if len(top) == N:
        break
os.makedirs(OUT, exist_ok=True)
tiles = []
fields = [
    "rank",
    "id",
    "ts",
    "source",
    "num_fish",
    "aes",
    "colorful",
    "big_sharp",
    "big_fish",
    "score",
    "image_path",
    "annotated_path",
]
with open(f"{OUT}/top{N}.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=fields)
    w.writeheader()
    for k, r in enumerate(top, 1):
        im = cv2.imread(r["image_path"])
        ann = im.copy()
        try:
            for a in json.load(open(r["coco_path"]))["annotations"]:
                x, y, bw, bh = (int(v) for v in a["bbox"])
                name = a["topk"][0]["name"] if a.get("topk") else "fish"
                cv2.rectangle(ann, (x, y), (x + bw, y + bh), (0, 255, 255), 2)
                cv2.putText(
                    ann,
                    name,
                    (x + 2, max(12, y - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.5,
                    (0, 255, 255),
                    1,
                    cv2.LINE_AA,
                )
        except Exception as e:
            print("coco fail", e)
        base = f"{k:02d}_{os.path.basename(r['image_path'])[:-4]}"
        cv2.imwrite(f"{OUT}/{base}_raw.jpg", im, [cv2.IMWRITE_JPEG_QUALITY, 95])
        ap = f"{OUT}/{base}_annotated.jpg"
        cv2.imwrite(ap, ann, [cv2.IMWRITE_JPEG_QUALITY, 95])
        w.writerow(
            {
                **{kk: r[kk] for kk in fields if kk in r},
                "rank": k,
                "score": round(r["score"], 4),
                "annotated_path": ap,
            }
        )
        t = cv2.resize(im, (640, 360))
        cv2.putText(
            t,
            f"#{k} {r['source'][:4]} {r['ts'][:16]} aes={r['aes']:.1f}",
            (8, 22),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
        tiles.append(t)
cols = 4
rws = (len(tiles) + cols - 1) // cols
while len(tiles) < cols * rws:
    tiles.append(np.zeros_like(tiles[0]))
cv2.imwrite(
    f"{OUT}/contact_sheet.jpg",
    np.vstack([np.hstack(tiles[i * cols : (i + 1) * cols]) for i in range(rws)]),
    [cv2.IMWRITE_JPEG_QUALITY, 85],
)
print("wrote", len(top))
