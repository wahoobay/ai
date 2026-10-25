import csv
import os
import sys
import time

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from transformers import CLIPModel

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from clip_aes import DS

S = os.path.dirname(os.path.abspath(__file__))


class MLP(nn.Module):
    def __init__(s):
        super().__init__()
        s.layers = nn.Sequential(
            nn.Linear(768, 1024),
            nn.Dropout(0.2),
            nn.Linear(1024, 128),
            nn.Dropout(0.2),
            nn.Linear(128, 64),
            nn.Dropout(0.1),
            nn.Linear(64, 16),
            nn.Linear(16, 1),
        )

    def forward(s, x):
        return s.layers(x)


if __name__ == "__main__":
    rows = list(csv.DictReader(open(f"{S}/random5.csv")))
    print(len(rows), "rows", flush=True)
    dev = "cuda"
    m = (
        CLIPModel.from_pretrained("openai/clip-vit-large-patch14", torch_dtype=torch.float16)
        .to(dev)
        .eval()
    )
    head = MLP()
    head.load_state_dict(
        torch.load(f"{S}/aesthetic_l14.pth", map_location="cpu", weights_only=True)
    )
    head = head.to(dev).eval()
    emb = np.lib.format.open_memmap(
        f"{S}/clip_emb_center.npy", mode="w+", dtype=np.float16, shape=(len(rows), 768)
    )
    dl = DataLoader(DS(rows), batch_size=192, num_workers=64, pin_memory=True)
    out = open(f"{S}/laion5.csv", "w", newline="")
    w = csv.writer(out)
    w.writerow(["id", "laion_center", "laion_letterbox"])
    n = 0
    t0 = time.time()
    with torch.no_grad():
        for x, idx in dl:
            B = x.shape[0]
            x = x.view(-1, 3, 224, 224).to(dev, non_blocking=True).half()
            E = m.get_image_features(pixel_values=x)
            E = getattr(E, "pooler_output", E) if not torch.is_tensor(E) else E
            E = (E / E.norm(dim=-1, keepdim=True)).view(
                B, 2, -1
            )  # [:,0]=letterbox, [:,1]=centre crop
            sc = head(E.float()).squeeze(-1).cpu().numpy()  # (B,2)
            Ec = E[:, 1].cpu().numpy().astype(np.float16)
            for k in range(B):
                i = int(idx[k])
                if i < 0:
                    continue
                emb[i] = Ec[k]
                w.writerow([rows[i]["id"], round(float(sc[k, 1]), 4), round(float(sc[k, 0]), 4)])
            n += B
            if n % 19200 == 0:
                print(n, f"{n/(time.time()-t0):.0f} img/s", flush=True)
    emb.flush()
    print("done", n, flush=True)
