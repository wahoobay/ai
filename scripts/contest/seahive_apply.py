import csv
import pickle
import random

import cv2
import numpy as np

S = "."
clf = pickle.load(open("seahive_probe4.pkl", "rb"))
T = np.load("seahive_text.npy")
rows = list(csv.DictReader(open("random5c.csv")))
E = np.load("clip_emb4_c.npy", mmap_mode="r")
out = open("seahive5c.csv", "w", newline="")
w = csv.writer(out)
w.writerow(["id", "seahive"])
for s in range(0, len(rows), 100000):
    X = np.asarray(E[s : s + 100000], dtype=np.float32)
    sims = 100 * X @ T.T
    marg = sims[..., :4].max(-1) - sims[..., 4:].max(-1)
    p = clf.predict_proba(np.concatenate([X.reshape(len(X), -1), marg], 1))[:, 1]
    for k in range(len(p)):
        w.writerow([rows[s + k]["id"], round(float(p[k]), 4)])
out.close()
p = {r["id"]: float(r["seahive"]) for r in csv.DictReader(open("seahive5c.csv"))}
cam = [r for r in rows if r["source"] == "seahivecam" and 7 <= int(r["ts"][11:13]) <= 19]
print(
    "reef-cam daytime frames with p>0.9: %.0f%%" % (100 * np.mean([p[r["id"]] > 0.9 for r in cam]))
)
random.seed(5)
hi = random.sample([r for r in cam if p[r["id"]] > 0.9], 32)
lo = random.sample([r for r in cam if p[r["id"]] < 0.1], 16)
tiles = []
for grp in (hi, lo):
    for r in grp:
        im = cv2.imread(r["image_path"], cv2.IMREAD_REDUCED_COLOR_2)
        t = cv2.resize(im, (320, 180))
        cv2.putText(
            t, f'{p[r["id"]]:.2f}', (6, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2
        )
        tiles.append(t)
cv2.imwrite(
    "burst/seahive_val.jpg",
    np.vstack([np.hstack(tiles[i * 8 : (i + 1) * 8]) for i in range(6)]),
    [cv2.IMWRITE_JPEG_QUALITY, 80],
)
print("validation sheet: rows 1-4 predicted seahive, rows 5-6 predicted not")
