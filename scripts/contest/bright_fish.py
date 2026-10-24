import csv, os, numpy as np, torch, sys, glob
from transformers import CLIPModel, CLIPTokenizer
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); from clip_aes import DS
S=os.path.dirname(os.path.abspath(__file__))
POS=["a close-up photo of a single large colorful parrotfish","a bright orange and blue parrotfish filling the frame","an angelfish facing the camera","one large vividly colored tropical fish, sharp and close to the camera","a bright blue and green fish with iridescent scales"]
NEG=["a school of yellow striped grunts","many small striped yellow fish swimming together","a large school of small fish","distant tiny fish in murky water","an empty reef with no fish"]
EX=sorted(glob.glob('/raid/scratch/dzimmerman2021/wahoobay/exports/vibrant_frames/parrotfish_2026-06-21/20260621T2219*_seahivecam.jpg'))[:0]  # placeholder
EX=['/raid/scratch/dzimmerman2021/wahoobay/exports/vibrant_frames/parrotfish_2026-06-21/'+f for f in ['20260621T221908Z_f0033490354_seahivecam.jpg','20260621T221920Z_f0033490533_seahivecam.jpg','20260621T221927Z_f0033490625_seahivecam.jpg','20260621T221942Z_f0033490853_seahivecam.jpg','20260621T221943Z_f0033490875_seahivecam.jpg','20260621T222326Z_f0033494224_seahivecam.jpg']]
dev='cuda'; m=CLIPModel.from_pretrained('openai/clip-vit-large-patch14', torch_dtype=torch.float16).to(dev).eval(); tok=CLIPTokenizer.from_pretrained('openai/clip-vit-large-patch14')
with torch.no_grad():
    t=tok(POS+NEG, padding=True, return_tensors='pt').to(dev); T=m.get_text_features(**t); T=getattr(T,'pooler_output',T) if not torch.is_tensor(T) else T; T=(T/T.norm(dim=-1,keepdim=True)).float().cpu().numpy()
    ds=DS([dict(image_path=p) for p in EX]); x=torch.stack([ds[i][0] for i in range(len(EX))]).view(-1,3,224,224).to(dev).half()
    E=m.get_image_features(pixel_values=x); E=getattr(E,'pooler_output',E) if not torch.is_tensor(E) else E; E=(E/E.norm(dim=-1,keepdim=True)).view(len(EX),2,-1)[:,1].float().cpu().numpy()
rows=list(csv.DictReader(open(f'{S}/random5.csv')))
emb=np.load(f'{S}/clip_emb_center.npy', mmap_mode='r')
# the angelfish frame (3 July 09:46) is in the sample: add it as an exemplar by id lookup on timestamp
ang=[i for i,r in enumerate(rows) if r['ts'].startswith('2026-07-03 09:46')]
EX_E=np.vstack([E]+([emb[ang].astype(np.float32)] if ang else [])); print('exemplars', EX_E.shape[0])
out=open(f'{S}/bright5.csv','w',newline=''); w=csv.writer(out); w.writerow(['id','bright','grunt','exsim'])
for s in range(0,len(rows),100000):
    X=np.asarray(emb[s:s+100000], dtype=np.float32); sims=100*X@T.T; ex=(X@EX_E.T).max(1)
    pos=sims[:,:len(POS)]; neg=sims[:,len(POS):]
    bright=pos.max(1)-neg.max(1); grunt=neg[:,:3].max(1)-pos.max(1)
    for k in range(X.shape[0]): w.writerow([rows[s+k]['id'], round(float(bright[k]),3), round(float(grunt[k]),3), round(float(ex[k]),4)])
    print(s+X.shape[0], flush=True)
print('done')
