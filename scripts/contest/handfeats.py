import csv, json, os, numpy as np, cv2
from multiprocessing import Pool
S=os.path.dirname(os.path.abspath(__file__))
def feats(r):
    try:
        im=cv2.imread(r['image_path'], cv2.IMREAD_REDUCED_COLOR_2)
        if im is None: return None
        H,W=im.shape[:2]
        b,g,rr=[im[...,i].astype(np.float32) for i in range(3)]
        rg=rr-g; yb=0.5*(rr+g)-b
        colorful=float(np.sqrt(rg.std()**2+yb.std()**2)+0.3*np.sqrt(rg.mean()**2+yb.mean()**2))
        hsv=cv2.cvtColor(im, cv2.COLOR_BGR2HSV); sat=float(hsv[...,1].mean()); val=float(hsv[...,2].mean()); contrast=float(hsv[...,2].std())
        gray=cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
        try: ann=json.load(open(r['coco_path']))['annotations']
        except Exception: ann=[]
        areas=[]; sharp=[]; fcol=[]
        for a in ann:
            x,y,bw,bh=[int(v/2) for v in a['bbox']]; x0,y0,x1,y1=max(0,x),max(0,y),min(W,x+bw),min(H,y+bh)
            if x1-x0<8 or y1-y0<8: continue
            areas.append((x1-x0)*(y1-y0)/(H*W)); crop=gray[y0:y1,x0:x1]; sharp.append(cv2.Laplacian(crop, cv2.CV_64F).var())
            fcol.append(float(hsv[y0:y1,x0:x1,1].mean()))
        big=max(areas) if areas else 0.0
        bigsharp=sharp[int(np.argmax(areas))] if areas else float(cv2.Laplacian(gray, cv2.CV_64F).var())
        return dict(id=r['id'], colorful=round(colorful,2), sat=round(sat,1), val=round(val,1), contrast=round(contrast,1), nbox=len(areas), big_fish=round(big,4), fish_area=round(float(sum(areas)),4), big_sharp=round(float(bigsharp),1), fish_sat=round(float(np.mean(fcol)) if fcol else 0.0,1))
    except Exception: return None
if __name__=='__main__':
    rows=list(csv.DictReader(open(f'{S}/random5.csv')))
    out=open(f'{S}/hand5.csv','w',newline=''); w=None; n=0
    with Pool(100) as p:
        for x in p.imap_unordered(feats, rows, chunksize=128):
            if not x: continue
            if w is None: w=csv.DictWriter(out, fieldnames=list(x)); w.writeheader()
            w.writerow(x); n+=1
            if n%100000==0: print(n, flush=True)
    print('done', n)
