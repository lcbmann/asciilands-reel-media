import json,re,os,urllib.request,csv,hashlib,sys
from concurrent.futures import ThreadPoolExecutor
G="1549197806739394590"
d=json.load(open('dc_dump.json',encoding='utf-8'))
OUT=sys.argv[1]; os.makedirs(OUT,exist_ok=True)
CH={"screenshots":"1549198722385322064","general":"1549198613425688587","eu_chat":"1561461151923965982","na_chat":"1561461245348024502","announcements":"1549198646942367846","devlog":"1549198677627637771","poll_thread":"1561679219363291218"}
items=[]
def slug(s): return re.sub(r'[^a-z0-9]+','-',s.lower()).strip('-')[:28] or 'x'
def add(src,server,thread,tid,m,cid):
    for i,a in enumerate(m.get('attachments',[])):
        if not (a.get('ct') or '').startswith(('image','video')): continue
        ext=os.path.splitext(a['filename'])[1].lower() or '.png'
        fn=f"{server}_{slug(thread)}_{m['id']}_{i}{ext}"
        items.append(dict(file=fn,server=server,source=src,thread=thread,thread_id=tid or '',message_id=m['id'],
          link=f"https://discord.com/channels/{G}/{tid or cid}/{m['id']}",author=m['author']['username'],author_display=m['author'].get('global_name') or '',
          posted=m['ts'],width=a.get('w'),height=a.get('h'),content_type=a.get('ct'),size=a.get('size'),caption=m['content'][:400].replace('\n',' '),url=a['url']))
for k,v in d['forums'].items():
    for t in v:
        for m in t['msgs']:
            if 'author' in m: add(f"{k}-towns-and-trade",k.upper(),t['name'],t['id'],m,None)
for k,v in d['channels'].items():
    for m in v:
        if 'author' in m: add(k,{'eu_chat':'EU','na_chat':'NA'}.get(k,'ANY'),k,None,m,CH[k])
def fetch(it):
    p=os.path.join(OUT,it['file'])
    if not os.path.exists(p):
        try:
            data=urllib.request.urlopen(urllib.request.Request(it['url'],headers={"User-Agent":"Mozilla/5.0"}),timeout=60).read()
            open(p,'wb').write(data)
        except Exception as e: it['error']=str(e); return it
    it['sha256']=hashlib.sha256(open(p,'rb').read()).hexdigest()
    return it
with ThreadPoolExecutor(12) as ex: res=list(ex.map(fetch,items))
for r in res: r.pop('url',None)
json.dump(res,open(os.path.join(OUT,'SOURCES.json'),'w',encoding='utf-8'),ensure_ascii=False,indent=1)
with open(os.path.join(OUT,'SOURCES.csv'),'w',newline='',encoding='utf-8-sig') as f:
    w=csv.DictWriter(f,fieldnames=[k for k in res[0].keys() if k!='error']+['error']); w.writeheader(); [w.writerow(r) for r in res]
print(len(res),'errors',sum(1 for r in res if 'error' in r))
