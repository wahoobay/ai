import csv, os, json, sys, cv2, numpy as np, datetime as dt
S=os.path.dirname(os.path.abspath(__file__))
N=int(sys.argv[1]) if len(sys.argv)>1 else 20
rows=list(csv.DictReader(open(f'{S}/scores3.csv')))
for r in rows:
    for k in ('colorful','sat','val','hue_ent','contrast','fish_area','big_fish','fish_col','fish_sat','sharp'): r[k]=float(r[k])
    r['num_fish']=int(r['num_fish'])
# drop too-dark / blown-out frames, then rank by rank-average of colorfulness, saturation and hue diversity
rows=[r for r in rows if 60<=r['val']<=215]
def ranks(key):
    order=sorted(range(len(rows)), key=lambda i: rows[i][key]); rk=[0]*len(rows)
    for pos,i in enumerate(order): rk[i]=pos/len(rows)
    return rk
R={k:ranks(k) for k in ('colorful','sat','fish_col','fish_sat','fish_area','contrast','sharp')}
W={'colorful':.22,'sat':.13,'fish_col':.18,'fish_sat':.08,'fish_area':.12,'contrast':.12,'sharp':.15}
for i,r in enumerate(rows): r['score']=sum(W[k]*R[k][i] for k in W)
rows.sort(key=lambda r:-r['score'])
# diversity: at most one frame per camera per 30-minute window
seen=set(); top=[]
for r in rows:
    t=dt.datetime.fromisoformat(r['ts']); key=(r['source'], t.strftime('%Y%m%d%H'), t.minute//30); dkey=t.strftime('%Y%m%d')
    if key in seen or sum(1 for x in top if x['ts'][:10]==r['ts'][:10])>=3: continue
    if key in seen: continue
    seen.add(key); top.append(r)
    if len(top)==N: break
os.makedirs(f'{S}/top3', exist_ok=True)
tiles=[]
with open(f'{S}/top3/top{N}.csv','w',newline='') as f:
    w=csv.DictWriter(f, fieldnames=['rank','id','ts','source','num_fish','colorful','sat','hue_ent','score','image_path','annotated_path']); w.writeheader()
    for k,r in enumerate(top,1):
        im=cv2.imread(r['image_path'])
        ann=im.copy()
        try:
            coco=json.load(open(r['coco_path']))
            for a in coco['annotations']:
                x,y,bw,bh=[int(v) for v in a['bbox']]
                name=(a['topk'][0]['name'] if a.get('topk') else 'fish')
                cv2.rectangle(ann,(x,y),(x+bw,y+bh),(0,255,255),2)
                (tw,th),_=cv2.getTextSize(name,cv2.FONT_HERSHEY_SIMPLEX,0.5,1)
                cv2.rectangle(ann,(x,max(0,y-th-6)),(x+tw+4,y),(0,255,255),-1)
                cv2.putText(ann,name,(x+2,y-3),cv2.FONT_HERSHEY_SIMPLEX,0.5,(0,0,0),1,cv2.LINE_AA)
        except Exception as e: print('coco fail',r['coco_path'],e)
        base=f"{k:02d}_{os.path.basename(r['image_path'])[:-4]}"
        cv2.imwrite(f'{S}/top3/{base}_raw.jpg', im, [cv2.IMWRITE_JPEG_QUALITY,95])
        ap=f'{S}/top3/{base}_annotated.jpg'; cv2.imwrite(ap, ann, [cv2.IMWRITE_JPEG_QUALITY,95])
        r2={kk:r[kk] for kk in ['id','ts','source','num_fish','colorful','sat','hue_ent','image_path']}; r2.update(rank=k,score=round(r['score'],4),annotated_path=ap); w.writerow(r2)
        t=cv2.resize(ann,(640,360)); cv2.putText(t,f"#{k} {r['source']} {r['ts'][:16]} fish={r['num_fish']}",(8,22),cv2.FONT_HERSHEY_SIMPLEX,0.6,(255,255,255),2,cv2.LINE_AA)
        tiles.append(t)
cols=4; rws=(len(tiles)+cols-1)//cols
while len(tiles)<cols*rws: tiles.append(np.zeros_like(tiles[0]))
sheet=np.vstack([np.hstack(tiles[i*cols:(i+1)*cols]) for i in range(rws)])
cv2.imwrite(f'{S}/top3/contact_sheet.jpg', sheet, [cv2.IMWRITE_JPEG_QUALITY,85])
print('wrote', len(top))
