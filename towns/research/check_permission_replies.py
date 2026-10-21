import os,json,urllib.request,urllib.error,time
t=os.environ["BROWSERLANDS_DISCORD_BOT_TOKEN"]
H={"Authorization":"Bot "+t,"User-Agent":"AsciilandsBot (https://asciilands.io, 1)"}
P={"1562365573734862898":"1562489932923273270","1560401747497844837":"1562489939990679626","1561598265978388510":"1562489946378477580","1561515612562395166":"1562489952586039347","1561188650052616244":"1562489958957187214","1561532575917285396":"1562489965378539682","1561474524094926909":"1562489971817062451","1562138164284497921":"1562489978649317439","1561846647888154695":"1562489985020469291","1562408178807742474":"1562489991186227241","1562242965974417510":"1562489997561434153","1562215591396511744":"1562494571143893003","1562407649440432238":"1562494577829609564","1561958549389844550":"1562494584431575092","1561470551267086456":"1562494591490465803"}
out={}
for tid,mid in P.items():
    for _ in range(4):
        try:
            r=json.load(urllib.request.urlopen(urllib.request.Request(f"https://discord.com/api/v10/channels/{tid}/messages?after={mid}&limit=50",headers=H),timeout=20)); break
        except urllib.error.HTTPError as e:
            if e.code==429: time.sleep(1.5); continue
            r=[{"error":e.code}]; break
    out[tid]=[{"a":m.get("author",{}).get("username"),"ts":m.get("timestamp"),"c":m.get("content","")[:300],"ref":(m.get("message_reference") or {}).get("message_id")} for m in r]
    time.sleep(0.3)
print(json.dumps(out))
