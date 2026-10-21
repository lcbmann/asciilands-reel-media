import os,json,urllib.request,urllib.error,time,sys
g=os.environ["BROWSERLANDS_DISCORD_GUILD_ID"];t=os.environ["BROWSERLANDS_DISCORD_BOT_TOKEN"]
H={"Authorization":"Bot "+t,"User-Agent":"AsciilandsBot (https://asciilands.io, 1)"}
def get(path):
    for _ in range(6):
        try: return json.load(urllib.request.urlopen(urllib.request.Request("https://discord.com/api/v10"+path,headers=H),timeout=20))
        except urllib.error.HTTPError as e:
            if e.code==429: time.sleep(float(json.loads(e.read()).get("retry_after",1))+0.2); continue
            return {"error":e.code,"path":path}
        except Exception as e: time.sleep(1)
    return {"error":"fail","path":path}
def msgs(cid,limit=3000):
    out=[];before=None
    while len(out)<limit:
        r=get(f"/channels/{cid}/messages?limit=100"+(f"&before={before}" if before else ""))
        if not isinstance(r,list) or not r: 
            if isinstance(r,dict): out.append(r)
            break
        out+=r;before=r[-1]["id"]
        if len(r)<100: break
    return out
def slim(m):
    if "error" in m: return m
    return {"id":m["id"],"ts":m["timestamp"],"author":{"id":m["author"]["id"],"username":m["author"]["username"],"global_name":m["author"].get("global_name")},
      "content":m.get("content",""),"attachments":[{"url":a["url"],"filename":a["filename"],"w":a.get("width"),"h":a.get("height"),"ct":a.get("content_type"),"size":a.get("size")} for a in m.get("attachments",[])],
      "embeds":[{"type":e.get("type"),"url":e.get("url"),"title":e.get("title"),"image":(e.get("image") or e.get("thumbnail") or {}).get("url")} for e in m.get("embeds",[])],
      "poll":m.get("poll"),"ref":(m.get("message_reference") or {}).get("message_id"),"reactions":[(r["emoji"].get("name"),r["count"]) for r in m.get("reactions",[])]}
FORUMS={"eu":"1560390862117015774","na":"1561460993471684719"}
act=get(f"/guilds/{g}/threads/active").get("threads",[])
res={"forums":{},"channels":{}}
for k,fid in FORUMS.items():
    ths={t["id"]:t for t in act if t["parent_id"]==fid}
    before=None
    while True:
        r=get(f"/channels/{fid}/threads/archived/public?limit=100"+(f"&before={before}" if before else ""))
        if "threads" not in r: break
        for t in r["threads"]: ths[t["id"]]=t
        if not r.get("has_more") or not r["threads"]: break
        before=r["threads"][-1]["thread_metadata"]["archive_timestamp"]
    res["forums"][k]=[]
    for tid,t in ths.items():
        res["forums"][k].append({"id":tid,"name":t["name"],"owner_id":t.get("owner_id"),"message_count":t.get("message_count"),"archived":t["thread_metadata"]["archived"],"created":t["thread_metadata"].get("create_timestamp"),"msgs":[slim(m) for m in msgs(tid)]})
for name,cid in {"screenshots":"1549198722385322064","announcements":"1549198646942367846","devlog":"1549198677627637771","poll_thread":"1561679219363291218","general":"1549198613425688587","eu_chat":"1561461151923965982","na_chat":"1561461245348024502"}.items():
    res["channels"][name]=[slim(m) for m in msgs(cid, 3000 if name in("screenshots","announcements","devlog","poll_thread") else 2000)]
print(json.dumps(res))
