import csv
import os
import sys
import time

import numpy as np
import torch
from PIL import Image
from torch.utils.data import DataLoader, Dataset
from transformers import CLIPModel, CLIPTokenizer

S = os.path.dirname(os.path.abspath(__file__))
IN = sys.argv[1]
TAG = sys.argv[2]
MEAN = np.array([0.48145466, 0.4578275, 0.40821073], np.float32)
STD = np.array([0.26862954, 0.26130258, 0.27577711], np.float32)
from clip_aes import NEG, POS  # noqa: E402  (after sys.argv handling)


class DS(Dataset):
    def __init__(s, rows):
        s.rows = rows

    def __len__(s):
        return len(s.rows)

    def __getitem__(s, i):
        r = s.rows[i]
        try:
            img = Image.open(r["image_path"])
            img.draft("RGB", (640, 360))
            img = img.convert("RGB")
        except Exception:
            return torch.zeros(4, 3, 224, 224), -1
        w, h = img.size
        lb = np.full((224, 224, 3), 114, np.uint8)
        r1 = np.asarray(img.resize((224, int(224 * h / w)), Image.BILINEAR))
        o = (224 - r1.shape[0]) // 2
        lb[o : o + r1.shape[0]] = r1
        wide = np.asarray(img.resize((int(224 * w / h), 224), Image.BILINEAR))
        W = wide.shape[1]
        c = (W - 224) // 2
        views = [lb, wide[:, 0:224], wide[:, c : c + 224], wide[:, W - 224 : W]]
        arr = np.stack(views).astype(np.float32) / 255.0
        arr = (arr - MEAN) / STD
        return torch.from_numpy(arr).permute(0, 3, 1, 2), i


if __name__ == "__main__":
    rows = list(csv.DictReader(open(IN)))
    print(len(rows), "rows", flush=True)
    dev = "cuda"
    m = (
        CLIPModel.from_pretrained("openai/clip-vit-large-patch14", torch_dtype=torch.float16)
        .to(dev)
        .eval()
    )
    tok = CLIPTokenizer.from_pretrained("openai/clip-vit-large-patch14")
    with torch.no_grad():
        t = tok(POS + NEG, padding=True, return_tensors="pt").to(dev)
        T = m.get_text_features(**t)
        T = getattr(T, "pooler_output", T) if not torch.is_tensor(T) else T
        T = T / T.norm(dim=-1, keepdim=True)
    emb = np.lib.format.open_memmap(
        f"{S}/clip_emb4_{TAG}.npy", mode="w+", dtype=np.float16, shape=(len(rows), 4, 768)
    )
    dl = DataLoader(DS(rows), batch_size=128, num_workers=64, pin_memory=True)
    out = open(f"{S}/clip5{TAG}.csv", "w", newline="")
    w = csv.writer(out)
    w.writerow(["id", "aes", "pos_max"])
    n = 0
    t0 = time.time()
    with torch.no_grad():
        for x, idx in dl:
            B = x.shape[0]
            x = x.view(-1, 3, 224, 224).to(dev, non_blocking=True).half()
            E = m.get_image_features(pixel_values=x)
            E = getattr(E, "pooler_output", E) if not torch.is_tensor(E) else E
            E = (E / E.norm(dim=-1, keepdim=True)).view(B, 4, -1)
            Ea = E[:, :2].mean(1)
            Ea = Ea / Ea.norm(dim=-1, keepdim=True)
            sims = (100 * Ea @ T.T).float().cpu().numpy()
            En = E.cpu().numpy().astype(np.float16)
            for k in range(B):
                i = int(idx[k])
                if i < 0:
                    continue
                emb[i] = En[k]
                s = sims[k]
                w.writerow(
                    [
                        rows[i]["id"],
                        round(float(s[: len(POS)].mean() - s[len(POS) :].mean()), 3),
                        round(float(s[: len(POS)].max()), 2),
                    ]
                )
            n += B
            if n % 25600 == 0:
                print(n, f"{n/(time.time()-t0):.0f} img/s", flush=True)
    emb.flush()
    print("done", n, flush=True)
