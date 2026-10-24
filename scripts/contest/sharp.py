import csv, os, cv2, numpy as np, json
from multiprocessing import Pool
S=os.path.dirname(os.path.abspath(__file__))
rows=list(csv.DictReader(open(f'{S}/scores2.csv')))
def f(r):
    im=cv2.imread(r['image_path'], cv2.IMREAD_REDUCED_GRAYSCALE_2)
    if im is None: return None
    # sharpness measured inside fish boxes (the subject), fallback to whole frame
    H,W=im.shape; coco=json.load(open(r['coco_path'])); vals=[]
    for a in coco['annotations']:
        x,y,bw,bh=[int(v/2) for v in a['bbox']]; crop=im[max(0,y):y+bh, max(0,x):x+bw]
        if crop.size>400: vals.append(cv2.Laplacian(crop, cv2.CV_64F).var())
    r['sharp']=round(float(np.median(vals)) if vals else float(cv2.Laplacian(im, cv2.CV_64F).var()),1)
    return r
with Pool(96) as p: out=[x for x in p.imap_unordered(f, rows, chunksize=64) if x]
with open(f'{S}/scores3.csv','w',newline='') as fh:
    w=csv.DictWriter(fh, fieldnames=list(out[0])); w.writeheader(); w.writerows(out)
print('done',len(out))
