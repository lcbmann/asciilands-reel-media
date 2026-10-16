"""Build project/review-v2.html from the v2 render data. Run after edit_reel_v2.py."""
from pathlib import Path
import json, html

R = Path(__file__).resolve().parent
O = R / 'edit-v2'
E = html.escape


def main():
    tl = json.loads((O / 'timeline.json').read_text())
    caps = json.loads((O / 'captions.json').read_text())
    audio = json.loads((O / 'audio-report.json').read_text())
    cuts = json.loads((O / 'audio-cuts.json').read_text())
    checks = json.loads((O / 'checks.json').read_text())
    sheets = sorted(p.name for p in (O / 'qa').glob('sheet-*.jpg'))
    m = audio['master']
    rows = []
    for i, s in enumerate(tl):
        prov = s.get('provisional')
        snd = s.get('sound')
        snd_txt = 'none' if not snd else (f'live game sound {snd[1]:+g} dB' if snd[0] == 'live' else f'gated real strikes {snd[1]:+g} dB')
        rows.append(f'<tr class="{"prov" if prov else ""}"><td>{i:02d}</td><td>{s["start"]:.2f} to {s["end"]:.2f}</td>'
                    f'<td>{E(s["at"])}</td><td>{E(Path(s["file"]).name)}</td><td>{s["t"]:.2f} to {s["t_out"]:.2f}</td>'
                    f'<td>{s["speed"]:.3g}x</td><td>{E(snd_txt)}</td><td>{E(s.get("note", ""))}'
                    f'{"<br><b>Needs " + E(prov) + "</b>" if prov else ""}</td></tr>')
    pauses = ''.join(f'<li>after "{E(p["after"])}": {p.get("original", p["kept"]):.2f}s to {p["kept"]:.2f}s</li>'
                     for p in cuts['pauses'] if p.get('original'))
    prov = [s for s in tl if s.get('provisional')]
    page = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Smithing reel v2</title>
<style>
:root{{--bg:#0f1712;--card:#18241c;--ink:#ece9da;--muted:#b9c6b8;--gold:#e6c675;--red:#e88;--line:#3a4b3e}}
body{{background:var(--bg);color:var(--ink);font:16px/1.55 system-ui,sans-serif;margin:0;padding:16px;max-width:1150px;margin:auto}}
h1,h2{{color:var(--gold)}} a{{color:var(--gold)}} .card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:16px 0}}
.prov{{background:#3a1d1d}} table{{border-collapse:collapse;width:100%;font-size:14px}} td,th{{border-bottom:1px solid var(--line);padding:6px;text-align:left;vertical-align:top}}
.wrap{{overflow-x:auto}} video{{width:100%;max-width:420px;display:block}} img{{max-width:100%}} .bad{{color:var(--red)}} small{{color:var(--muted)}}
.grid{{display:flex;gap:16px;flex-wrap:wrap}} .grid>div{{flex:1 1 320px}}
</style>
<h1>Asciilands smithing reel v2</h1>
<p>Rebuilt from the original recordings and the original narration, using the corrections brief. v1 is kept unchanged in reference/.</p>
<div class="grid"><div class="card"><h2>Review copy</h2><video controls preload="metadata" src="smithing-v2-review.mp4"></video>
<p><small>The review copy shows a red PROVISIONAL tag over the {len(prov)} sections still waiting for pickups. The final <code>smithing-v2.mp4</code> has no tags.</small></p></div>
<div class="card"><h2>Still missing</h2><ul>{''.join(f'<li><b>{E(s["provisional"])}</b> for "{E(s["at"])}" at {s["start"]:.2f} to {s["end"]:.2f}s. Currently: {E(s.get("note", ""))}</li>' for s in prov)}</ul>
<p>Pickup instructions are in <a href="../library/index.html">the footage library</a> and in HANDOFF-v2.txt.</p></div></div>
<div class="card"><h2>Numbers</h2><ul>
<li>Length {tl[-1]["end"]:.2f}s, 1080x1920, 30 fps. {len(tl)} shots, {len(caps)} caption groups.</li>
<li>Narration retimed from {cuts["original"]:.2f}s to {cuts["voice"]:.2f}s. Only measured silence was removed.</li>
<li>Audio: fixed gain {audio["fixed_gain_db"]:+.2f} dB, then a limiter. Master {m["input_i"]:.2f} LUFS integrated, {m["input_tp"]:.2f} dBTP, LRA {m["input_lra"]:.1f} LU.</li>
<li>Automated checks: {"none failed" if not checks["problems"] else '<span class="bad">' + E("; ".join(checks["problems"])) + "</span>"}.</li></ul>
<details><summary>Pauses shortened</summary><ul>{pauses}</ul></details></div>
<div class="card"><h2>Shot list</h2><div class="wrap"><table><tr><th>#</th><th>Output s</th><th>First words</th><th>Source</th><th>Source s</th><th>Speed</th><th>Sound</th><th>Notes</th></tr>{''.join(rows)}</table></div></div>
<div class="card"><h2>QA stills at every cut and caption change</h2><p><small>Red lines mark the safe area: x 80 to 1000, y 270 to 1480.</small></p>{''.join(f'<p><img loading="lazy" src="edit-v2/qa/{s}"></p>' for s in sheets)}</div>
</html>'''
    (R / 'review-v2.html').write_text(page, encoding='utf8')
    print('review-v2.html written')


if __name__ == '__main__':
    main()
