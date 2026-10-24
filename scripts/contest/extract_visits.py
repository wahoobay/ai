import glob, json, os, csv, cv2, numpy as np, datetime as dt, sys
from multiprocessing import Pool
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from find_parrot import f as colour_check
R='/raid/scratch/dzimmerman2021/wahoobay'; OUT=f'{R}/exports/vibrant_frames/parrotfish_visits'
# (local date, local hour:minute of best hit) from the sampled scan; window = ±20 min
VISITS=['2026-05-17 17:51','2026-06-04 08:57','2026-06-14 16:52','2026-06-15 12:38','2026-06-20 18:10','2026-07-27 09:02','2026-08-13 11:34']
import zoneinfo; NY=zoneinfo.ZoneInfo('America/New_York')
def cands(v):
    t=dt.datetime.strptime(v,'%Y-%m-%d %H:%M').replace(tzinfo=NY); lo=t-dt.timedelta(minutes=20); hi=t+dt.timedelta(minutes=20)
    out=[]
    for h in {lo.astimezone(dt.timezone.utc), hi.astimezone(dt.timezone.utc)}:
        d=f'{R}/frames/{h:%Y/%m/%d/%H}'
        for j in glob.glob(f'{d}/*seahivecam.coco.json'):
            ts=dt.datetime.strptime(os.path.basename(j)[:16],'%Y%m%dT%H%M%SZ').replace(tzinfo=dt.timezone.utc)
            if lo<=ts<=hi: out.append(dict(image_path=j.replace('.coco.json','.jpg'), coco_path=j, ts=ts.astimezone(NY).isoformat(), visit=v))
    return out
if __name__=='__main__':
    allc=[c for v in VISITS for c in cands(v)]; print(len(allc),'candidate frames across', len(VISITS),'visits')
    with Pool(96) as p: hits=[h for h in p.imap_unordered(colour_check, allc, chunksize=32) if h and h['orange']>=0.10 and h['blue']>=0.15]
    hits.sort(key=lambda h:(h['visit'],h['ts'])); print(len(hits),'hits')
    for v in VISITS:
        hv=[h for h in hits if h['visit']==v]; d=f'{OUT}/{v[:10]}_{v[11:13]}{v[14:]}'; os.makedirs(d, exist_ok=True)
        tiles=[]
        for h in hv:
            base=os.path.basename(h['image_path']); os.system(f'cp "{h["image_path"]}" "{d}/{base}"')
            if len(tiles)<36 and (len(hv)<=36 or hv.index(h)%max(1,len(hv)//36)==0):
                im=cv2.imread(h['image_path']); t=cv2.resize(im,(400,225)); cv2.putText(t,h['ts'][11:19]+f" a{h['area']}",(4,18),cv2.FONT_HERSHEY_SIMPLEX,0.6,(255,255,255),2); tiles.append(t)
        with open(f'{d}/index.csv','w',newline='') as fh:
            if hv: w=csv.DictWriter(fh, fieldnames=list(hv[0])); w.writeheader(); w.writerows(hv)
        if tiles:
            cols=6; rws=(len(tiles)+cols-1)//cols
            while len(tiles)<cols*rws: tiles.append(np.zeros_like(tiles[0]))
            cv2.imwrite(f'{d}/contact_sheet.jpg', np.vstack([np.hstack(tiles[i*cols:(i+1)*cols]) for i in range(rws)]), [cv2.IMWRITE_JPEG_QUALITY,75])
        print(v, len(hv), 'frames', (hv[0]['ts'][11:19]+'–'+hv[-1]['ts'][11:19]) if hv else '')
