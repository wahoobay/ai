import csv, sys, os, numpy as np, cv2
from multiprocessing import Pool
S=os.path.dirname(os.path.abspath(__file__))
def score(row):
    p=row['image_path']
    try:
        im=cv2.imread(p, cv2.IMREAD_REDUCED_COLOR_4)
        if im is None: return None
        b,g,r=[im[...,i].astype(np.float32) for i in range(3)]
        rg=r-g; yb=0.5*(r+g)-b
        colorful=float(np.sqrt(rg.std()**2+yb.std()**2)+0.3*np.sqrt(rg.mean()**2+yb.mean()**2))  # Hasler & Süsstrunk
        hsv=cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
        sat=float(hsv[...,1].mean()); val=float(hsv[...,2].mean())
        # hue diversity: entropy of hue histogram over reasonably saturated pixels
        m=hsv[...,1]>60
        if m.sum()>100:
            h=np.bincount(hsv[...,0][m].ravel(), minlength=180).astype(np.float64); h/=h.sum()
            hue_ent=float(-(h[h>0]*np.log(h[h>0])).sum())
        else: hue_ent=0.0
        return dict(row, colorful=round(colorful,2), sat=round(sat,1), val=round(val,1), hue_ent=round(hue_ent,3))
    except Exception as e:
        return None
if __name__=='__main__':
    rows=list(csv.DictReader(open(f'{S}/candidates.csv')))
    out=open(f'{S}/scores.csv','w',newline=''); w=None; n=0
    with Pool(96) as pool:
        for r in pool.imap_unordered(score, rows, chunksize=64):
            if r is None: continue
            if w is None: w=csv.DictWriter(out, fieldnames=list(r)); w.writeheader()
            w.writerow(r); n+=1
            if n%20000==0: print(n, flush=True)
    print('done', n)
