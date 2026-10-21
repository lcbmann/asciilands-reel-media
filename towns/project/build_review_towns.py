"""Copy a towns draft to the Videos folder with a browser review page.
python project/build_review_towns.py v3 [previous version]"""
import html, json, shutil, sys
from pathlib import Path

R = Path(__file__).resolve().parent
v = sys.argv[1]
prev = sys.argv[2] if len(sys.argv) > 2 else None
E = R / f'edit-{v}'
D = Path('C:/Users/liamb/Videos/asciilands-towns-short') / f'draft-{v}'
D.mkdir(parents=True, exist_ok=True)
for f in [R / f'towns-{v}.mp4', R / f'towns-{v}-review.mp4', E / 'qa/spectrograms.png', *sorted((E / 'qa').glob('sheet-*.jpg'))]:
    shutil.copy2(f, D / f.name)
if prev:
    shutil.copy2(R / f'towns-{prev}-review.mp4', D / f'towns-{prev}-review.mp4')
tl = json.loads((E / 'timeline.json').read_text())
total = tl[-1]['end']
avg = sum(s['duration'] for s in tl) / len(tl)
rows = ''.join(
    f"<tr data-t='{s['start']}'><td><a href='#' onclick='go({s['start']});return false'>{s['start']:.2f}</a></td>"
    f"<td>{s['duration']:.2f} s</td><td>{html.escape(s['at'].replace('[', '').replace(']', ''))}</td>"
    f"<td>{html.escape(s.get('label') or '')}</td><td>{html.escape(s.get('note', ''))}</td></tr>" for s in tl)
sheets = ''.join(f'<img src="{f.name}">' for f in sorted(D.glob('sheet-*.jpg')))
buttons = f"<button onclick=\"src('towns-{v}.mp4')\">{v} full quality</button><button onclick=\"src('towns-{v}-review.mp4')\">{v} review</button>"
if prev:
    buttons += f"<button onclick=\"src('towns-{prev}-review.mp4')\">{prev} (old)</button>"
page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>They Built This {v}</title>
<style>
:root{{--bg:#141a16;--card:#1d2520;--line:#2f3a33;--ink:#e6ece6;--dim:#9fb0a4;--gold:#f0c040}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,Segoe UI,sans-serif}}
.wrap{{display:grid;grid-template-columns:420px 1fr;gap:24px;padding:20px}}
@media(max-width:900px){{.wrap{{grid-template-columns:1fr}}}}
video{{width:100%;border-radius:8px;background:#000;position:sticky;top:16px}}
h1{{margin:0 0 4px;font-size:22px}} .dim{{color:var(--dim)}}
table{{border-collapse:collapse;width:100%;background:var(--card)}} td,th{{border-bottom:1px solid var(--line);padding:5px 8px;text-align:left;vertical-align:top}}
tr.on{{background:#2c3a31}} a{{color:var(--gold)}}
img{{max-width:100%;border:1px solid var(--line);border-radius:6px;margin:8px 0}}
.tabs button{{background:#2c3a31;color:var(--ink);border:0;padding:6px 12px;border-radius:5px;margin-right:6px;cursor:pointer}}
</style></head><body><div class="wrap">
<div><div class="tabs">{buttons}</div>
<video id="v" src="towns-{v}.mp4" controls playsinline></video>
<p class="dim">Click a time to jump. The row for the current shot lights up.</p></div>
<div><h1>They Built This: draft {v}</h1><p class="dim">{total:.1f} s, {len(tl)} shots, average {avg:.1f} s per shot (the smithing reel: 2.96 s).</p>
<table><tr><th>Time</th><th>Length</th><th>Words</th><th>Label</th><th>Footage</th></tr>{rows}</table>
<h2>QA frames</h2>{sheets}
<h2>Sound stems</h2><p class="dim">Top to bottom: voice, game, music, master.</p><img src="spectrograms.png"></div></div>
<script>
const v=document.getElementById('v'),rows=[...document.querySelectorAll('tr[data-t]')];
function go(t){{v.currentTime=t+0.01;v.play()}}
function src(f){{const t=v.currentTime;v.src=f;v.currentTime=t}}
v.addEventListener('timeupdate',()=>{{let k=0;rows.forEach((r,i)=>{{if(+r.dataset.t<=v.currentTime)k=i}});rows.forEach((r,i)=>r.classList.toggle('on',i===k))}});
</script></body></html>"""
(D / f'review-{v}.html').write_text(page, encoding='utf-8')
print(D / f'review-{v}.html')
