import csv, os, sys, numpy as np, torch, time
from PIL import Image
from torch.utils.data import Dataset, DataLoader
from transformers import CLIPModel, CLIPTokenizer
S=os.path.dirname(os.path.abspath(__file__))
MEAN=np.array([0.48145466,0.4578275,0.40821073],np.float32); STD=np.array([0.26862954,0.26130258,0.27577711],np.float32)
POS=["an award-winning underwater photograph of a colorful tropical fish on a reef",
     "a stunning, sharp, vibrant close-up photo of a fish, beautiful underwater photography",
     "a dramatic photo of a large school of fish swimming over a reef in clear blue water",
     "a breathtaking nature photograph, National Geographic underwater wildlife photo of the year"]
NEG=["a blurry, murky, hazy underwater image",
     "an empty underwater frame of sand and concrete with no fish",
     "a low quality, noisy, dark security camera frame",
     "a boring snapshot of tiny distant fish"]
class DS(Dataset):
    def __init__(s, rows): s.rows=rows
    def __len__(s): return len(s.rows)
    def __getitem__(s,i):
        r=s.rows[i]
        try:
            img=Image.open(r['image_path']); img.draft('RGB',(640,360)); img=img.convert('RGB')
        except Exception: return torch.zeros(2,3,224,224), -1
        w,h=img.size
        lb=np.full((224,224,3),114,np.uint8); r1=np.asarray(img.resize((224,int(224*h/w)), Image.BILINEAR)); o=(224-r1.shape[0])//2; lb[o:o+r1.shape[0]]=r1
        cc=np.asarray(img.resize((int(224*w/h),224), Image.BILINEAR)); x0=(cc.shape[1]-224)//2; cc=cc[:,x0:x0+224]
        arr=np.stack([lb,cc]).astype(np.float32)/255.; arr=(arr-MEAN)/STD
        return torch.from_numpy(arr).permute(0,3,1,2), i
if __name__=='__main__':
    rows=list(csv.DictReader(open(f'{S}/random5b.csv'))); print(len(rows),'rows', flush=True)
    dev='cuda'; m=CLIPModel.from_pretrained('openai/clip-vit-large-patch14', torch_dtype=torch.float16).to(dev).eval()
    tok=CLIPTokenizer.from_pretrained('openai/clip-vit-large-patch14')
    with torch.no_grad():
        t=tok(POS+NEG, padding=True, return_tensors='pt').to(dev); T=m.get_text_features(**t); T=getattr(T,'pooler_output',T) if not torch.is_tensor(T) else T; T=T/T.norm(dim=-1,keepdim=True)
    dl=DataLoader(DS(rows), batch_size=192, num_workers=48, pin_memory=True)
    out=open(f'{S}/clip5b.csv','w',newline=''); w=csv.writer(out); w.writerow(['id','aes','pos_max']+[f'p{i}' for i in range(len(POS))]+[f'n{i}' for i in range(len(NEG))])
    emb=np.lib.format.open_memmap(f'{S}/clip_emb_center_b.npy', mode='w+', dtype=np.float16, shape=(len(rows),768))
    n=0; t0=time.time()
    with torch.no_grad():
        for x,idx in dl:
            B=x.shape[0]; x=x.view(-1,3,224,224).to(dev, non_blocking=True).half()
            E=m.get_image_features(pixel_values=x); E=getattr(E,'pooler_output',E) if not torch.is_tensor(E) else E; E=E/E.norm(dim=-1,keepdim=True); E=E.view(B,2,-1); Ec=E[:,1].cpu().numpy().astype(np.float16); E=E.mean(1); E=E/E.norm(dim=-1,keepdim=True)
            sims=(100*E@T.T).float().cpu().numpy()
            for k in range(B):
                i=int(idx[k]); 
                if i<0: continue
                emb[i]=Ec[k]; s=sims[k]; aes=float(s[:len(POS)].mean()-s[len(POS):].mean())
                w.writerow([rows[i]['id'], round(aes,3), round(float(s[:len(POS)].max()),2)]+[round(float(v),2) for v in s])
            n+=B
            if n%19200==0: print(n, f'{n/(time.time()-t0):.0f} img/s', flush=True)
    emb.flush(); print('done', n, flush=True)
