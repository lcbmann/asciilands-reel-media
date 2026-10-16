"""Append the smithing v3 review to the footage library. Append-only:
existing descriptions, owner notes, selects, provenance and used_in.smithing-v1 are kept.
Run after edit_reel_v3.py:  python project/update_library_v3.py
"""
from pathlib import Path
import json, csv, html

R = Path(__file__).resolve().parent
B = R.parent
L = B / 'library'
PROV = 'Claude frame/audio review 2026-10-16 (verified select)'
EDIT = 'smithing-v3'
TODAY = '2026-10-16'

# Verified selects found while cutting v2. Times are source seconds.
VERIFIED = {
    '2026-10-04 17-31-45.mkv': [
        (12.0, 16.0, 'Night campfire by a lakeside cabin; dark-haired player (blue-grey shirt, brown trousers) at the fire'),
        (16.5, 21.0, 'Players arrive: Ash, Moss, Kestrel, Juniper join the campfire'),
        (17.5, 20.3, 'Kestrel walks up from below: white hair, red jacket, backpack (used in v3)'),
        (21.0, 29.0, 'Six characters around the campfire: Juniper, Kestrel, dark-haired player, Moss, Ash and one more'),
        (46.6, 50.2, 'Dark-haired player close by the fire, cabin wall behind (used in v3, 49.0 to 51.8)'),
        (50.9, 53.7, 'Ash walks in from the right: red hair, blue tunic; Moss follows (used in v3)'),
        (60.0, 168.0, 'Group by the cabin from several orbit angles; same characters'),
    ],
    'mapzoomout.mkv': [
        (0.0, 1.9, 'Local map, region hex Bracken Moss Meadows'),
        (2.0, 5.9, 'Local map zoom levels with the player arrow'),
        (6.0, 6.4, 'REJECT: blank frame during the Local to World switch'),
        (6.45, 8.85, 'World atlas zooming out to the whole explored world (used in v3)'),
        (8.85, 12.6, 'Widest World view, held'),
    ],
    'oneclickstonepickaxe.mkv': [
        (0.0, 1.5, 'Workbench: Stone Pickaxe recipe, ingredients Stick 50/2, Stone Chunk 52/2, Rope 2/1'),
        (1.55, 1.55, 'First click on Make 1 Stone Pickaxe'),
        (1.8, 3.6, 'Result line: +1 Stone Pickaxe'),
        (3.67, 3.67, 'Second identical click'),
    ],
    'oneclick-stonepickaxe-packfull.mkv': [
        (0.0, 3.7, 'Stone Pickaxe recipe before the click (usable only as a recipe view)'),
        (3.78, 7.2, 'REJECT: Make fails with "Pack full", then the Pack opens. Not a successful craft'),
    ],
    'yorkarrival.mkv': [
        (5.00, 5.93, 'REJECT for openings: character faces camera and turns; walk begins 6.00'),
        (6.00, 12.90, 'Clean walk through York from behind, dog alongside; menu opens at 13.0'),
    ],
    'yorksmith.mkv': [
        (10.5, 13.3, 'Walk toward the two York buildings, smithy on the right'),
        (14.5, 16.5, 'York Public Blacksmith sign readable: "Built by Louie"'),
        (18.0, 20.9, 'Town overview, pergola, half-timbered house, smithy'),
        (21.0, 25.5, 'Walk toward camera past the smithy with the dog; good settled ending shot'),
    ],
    'smithersmith.mkv': [
        (0.0, 1.9, 'REJECT: browser chrome visible at the top'),
        (3.5, 5.40, 'Approach the smithy door'),
        (5.43, 5.43, 'Exact frame where the view cuts inside the smithy'),
        (5.43, 8.2, 'Inside: Smither at the anvil, sign label visible'),
        (8.2, 13.5, 'Smither and player inside before the orbit starts at 14'),
    ],
    'smithbadsteel.mkv': [
        (3.0, 5.6, 'Forge UI open, untouched blank, silent'),
        (5.64, 12.1, 'Real-time first heats and strikes; clean game audio, no music'),
        (12.1, 55.6, 'Full shaping: blank to narrow rapier at Fit 97 percent (timelapse source)'),
        (57.5, 60.6, 'Typing the name Swordy'),
        (61.5, 63.0, 'Finish chosen: Polished then Etched'),
        (63.8, 64.45, 'Forge clicked: Forging...'),
        (64.5, 65.8, 'Finished blade panel: "Forged Swordy"; finished piece preview at x1261-1296 y643-920'),
        (75.45, 79.7, 'Swordy Inspect fully open (75.45 opening frame)'),
    ],
    'smithtwoswords.mkv': [
        (38.0, 43.8, 'Iron arming sword late strikes, real time (strikes 38.69 39.51 40.27 41.59 43.38)'),
        (44.0, 49.0, 'Iron at Fit 100 percent, idle heat; little action'),
        (49.0, 55.0, 'Typing the name Cleaver'),
        (59.0, 62.0, 'Iron finished blade panel'),
        (80.9, 83.4, 'Pack with Cleaver selected'),
        (83.55, 86.5, 'Cleaver Inspect fully open (closes 86.6)'),
        (118.0, 145.0, 'Bronze rapier late shaping, many strikes'),
    ],
    'starironhard.mkv': [
        (4.6, 7.4, 'Heat, surge and strike at 6.49, real time'),
    ],
    'inventorymaterials.mkv': [
        (0.0, 3.9, 'Stone Chunk selected'),
        (17.95, 20.75, 'Star Iron description: "Rare meteor metal for exceptional metalwork"'),
    ],
    'orbitdesertmeteor.mkv': [(12.0, 14.1, 'Meteor orbit used in v2')],
    'orbitsnowmeteor.mkv': [(48.0, 50.9, 'Meteor orbit used in v2')],
}

AUDIO = {
    '2026-10-04 17-31-45.mkv': 'Audio not used in v3.',
    'mapzoomout.mkv': 'Audio checked: quiet; not used.',
    'oneclickstonepickaxe.mkv': 'Audio checked: UI clicks at 1.56/1.74 and 3.67/3.87 s; used live under the narration.',
    'smithbadsteel.mkv': 'Audio checked: clean game sound only (heat whoosh, tonal strikes near 690 Hz/2 kHz, finish chime). No music or microphone. Usable live.',
    'smithtwoswords.mkv': 'Audio checked: strikes and heat clear; faint in-game music under parts (tonal bed visible 90-140s). Use gated strikes, not continuous audio.',
    'starironhard.mkv': 'Audio checked: strikes and heat clear; faint in-game music tones under the whole clip. Use gated strikes.',
    'yorkarrival.mkv': 'Audio checked: quiet ambience only (about -45 LUFS).',
    'smithersmith.mkv': 'Audio checked: quiet ambience only (about -45 LUFS).',
    'yorksmith.mkv': 'Audio checked: ambience and footsteps, about -39 LUFS.',
    'inventorymaterials.mkv': 'Audio checked: UI clicks only.',
}

NEW = {   # recordings added after the original 92-entry catalogue
    'mapzoomout.mkv': 'Owner pickup: World map zoom-out, current build.',
    'oneclickstonepickaxe.mkv': 'Owner pickup (uploaded as oneclickarrow.mkv): one-click Stone Pickaxe craft at a Workbench. Shows the Stone Pickaxe recipe, not Arrows.',
    'oneclick-stonepickaxe-packfull.mkv': 'Owner pickup (uploaded as oneclickarrows.mkv): the craft fails with Pack full. Rejected for the one-click beat.',
}
SUPPLIED = [
    dict(file='rarematerials.wav', path='media/audio/rarematerials.wav', kind='voice pickup',
         description='Replacement take of "Rare materials are one reason." Speech 0.62 to 2.10 s; spliced into v3 at +0.9 dB.'),
    dict(file='swordy-john-smith-inspect.png', path='assets/stills/swordy-john-smith-inspect.png', kind='owner screenshot',
         description='Swordy Inspect, maker John_Smith (2000x1224). Used for the sword, METAL and MAKER emphasis in v3.'),
]
PICKUPS_V2 = [
    dict(file='mapzoomout.mkv', status='NOT YET RECORDED', needed_for='smithing-v2 "cross a massive map"',
         brief='Open M on Local for 2 s, scroll out smoothly into World over 3-5 s, hold widest view 3 s, move pointer aside. '
               'Use a character that has visited many regions: the World atlas only draws visited or seen regions. '
               'Reduced motion off. Full 2560x1440, no other menus.'),
    dict(file='oneclickarrows.mkv', status='NOT YET RECORDED', needed_for='smithing-v2 "click a button and make exactly the same sword"',
         brief='At a Workbench, open the Arrows recipe (2 sticks, 1 stone, 1 fibre makes 6 arrows; the recipe panel says '
               '"Crafts immediately from inventory"). Hold 2 s on the recipe, click Craft once, hold 2-3 s on the result, '
               'click once more. Game sound on, no voice. Stone tools are not used: they are multi-step workbench crafts.'),
]


PICKUPS = [
    dict(file='mapzoomout.mkv', status='RECORDED 2026-10-16', needed_for='smithing "cross a massive map"', brief='Used 6.45 to 8.85 s.'),
    dict(file='oneclickstonepickaxe.mkv', status='RECORDED 2026-10-16', needed_for='smithing "click a button and make exactly the same sword"',
         brief='A real one-click Stone Pickaxe craft. The earlier take (oneclick-stonepickaxe-packfull.mkv) failed with Pack full.'),
]


def probe(path):
    import subprocess
    out = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration:stream=width,height,r_frame_rate,codec_type',
                                              '-of', 'json', str(path)]))
    v = next(st for st in out['streams'] if st['codec_type'] == 'video')
    return dict(duration=float(out['format']['duration']), width=v['width'], height=v['height'], fps=v['r_frame_rate'],
                audio=['aac'] if any(st['codec_type'] == 'audio' for st in out['streams']) else [])


def main():
    cat = json.loads((L / 'catalogue.json').read_text(encoding='utf8'))
    def name(r):   # newer entries have 'file'; older ones only the original Windows path
        return r.get('file') or (r.get('original_windows_path') or '').replace('\\', '/').split('/')[-1]
    entries = {name(r): r for r in cat['recordings']}
    for f, desc in NEW.items():
        if f not in entries:
            rec = dict(file=f, path=f'media/video/{f}', **probe(B / 'media/video' / f), description=desc, selects=[],
                       review_status='', tags=['smithing', 'pickup', '2026-10-16'], source_available_in_bundle=True, used_in={})
            cat['recordings'].append(rec)
            entries[f] = rec
    have_sup = {x['file'] for x in cat.get('supplied', [])}
    cat.setdefault('supplied', []).extend(x for x in SUPPLIED if x['file'] not in have_sup)
    for name, sels in VERIFIED.items():
        e = entries[name]
        have = {(s['start'], s['end'], s['description']) for s in e.get('selects', [])}
        for a, b, d in sels:
            if (a, b, d) not in have:
                e.setdefault('selects', []).append(dict(start=a, end=b, description=d, provenance=PROV))
        e['review_status'] = 'Owner selects plus frame-level review of used ranges (2026-10-16 v2/v3); motion of unused ranges sampled only'
    for name, note in AUDIO.items():
        entries[name]['audio_review'] = note
    camp = entries['2026-10-04 17-31-45.mkv']
    camp.setdefault('description', 'Players together at a night campfire by a lakeside cabin: Ash, Moss, Kestrel, Juniper and a dark-haired player.')
    camp['path'] = 'media/video/2026-10-04 17-31-45.mkv'
    camp['source_available_in_bundle'] = True
    camp['release'] = 'players-pickup-1'
    tl = json.loads((R / 'edit-v3/timeline.json').read_text())
    for e in entries.values():
        if 'used_in' in e:
            e['used_in'].pop(EDIT, None)
    for s in tl:
        for sp in s['srcs']:
            if 'v' in sp:
                f, a, b = sp['v'] + '.mkv', sp['t'], sp['t'] + s['duration'] * sp['speed']
            elif 'frame' in sp:
                f, a, b = sp['frame'][0] + '.mkv', sp['frame'][1], sp['frame'][1]
            else:
                continue
            entries[f].setdefault('used_in', {}).setdefault(EDIT, []).append(dict(
                source_in=round(a, 3), source_out=round(b, 3), output_in=s['start'], output_out=s['end'],
                speed=sp.get('speed'), phrase=s['at'], sound=s.get('sound'), still='frame' in sp))
    cat['pickups'] = PICKUPS
    cat['updated'] = TODAY
    note = '2026-10-16: smithing v3 pickups received, new selects, rejected take, voice pickup and owner still, used_in.smithing-v3 (Claude). Nothing removed.'
    if note not in cat.setdefault('history', []):
        cat['history'].append(note)
    (L / 'catalogue.json').write_text(json.dumps(cat, indent=1), encoding='utf8')

    # CSV: keep the existing columns, add provenance and audio columns at the end.
    rows = []
    with (L / 'shot-log.csv').open(encoding='utf-8-sig') as f:
        old = list(csv.reader(f))
    header = old[0]
    extra = ['provenance', 'audio_review']
    header = header + [c for c in extra if c not in header]
    for r in old[1:]:
        rows.append(r + [''] * (len(header) - len(r)))
    seen = {tuple(r[:5]) for r in rows}
    for name, e in entries.items():
        for s in e.get('selects', []):
            if s.get('provenance') != PROV:
                continue
            row = [name, str(bool(e.get('source_available_in_bundle'))), e.get('path') or '', f"{s['start']:g}", f"{s['end']:g}",
                   s['description'], e['review_status'], s['provenance'], e.get('audio_review', '')]
            if tuple(row[:5]) not in seen:
                rows.append(row)
    for p in PICKUPS:
        rows.append([p['file'], 'False', '', '', '', f"PICKUP {p['status']}: {p['needed_for']}. {p['brief']}", 'Not recorded', 'Claude v2 pickup list', ''])
    with (L / 'shot-log.csv').open('w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)

    # Searchable page with relative links that work inside this bundle.
    E = html.escape
    out = ['<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
           '<title>Asciilands footage library</title><style>body{background:#101913;color:#ece9da;font:16px/1.5 system-ui;'
           'max-width:1100px;margin:0 auto;padding:16px}h1,h2,a{color:#e6c675}input{box-sizing:border-box;width:100%;padding:14px;'
           'font:inherit;position:sticky;top:0}article{background:#1c2920;border:1px solid #425343;border-radius:12px;padding:16px;margin:16px 0}'
           'img{max-width:100%}small{color:#bcc9bb}.v{color:#9fe0a8}.r{color:#f0a0a0}.p{border-color:#a04040}</style>'
           '<h1>Asciilands footage library</h1><p>Original recordings are never modified. Owner timestamps are approximate; '
           'entries marked <span class="v">verified</span> were checked frame by frame. '
           '<a href="shot-log.csv">CSV shot log</a>. <a href="catalogue.json">JSON catalogue</a>.</p>'
           '<input placeholder="Search: steel, York, Louie, meteor, inspect, pickup"><p id="count"></p>']
    for p in PICKUPS:
        out.append(f'<article class="p" data-search="{E((p["file"] + " pickup " + p["brief"]).lower(), quote=True)}"><h2>{E(p["file"])} '
                   f'<small class="r">{E(p["status"])}</small></h2><p>{E(p["needed_for"])}</p><p>{E(p["brief"])}</p></article>')
    order = sorted(entries.items(), key=lambda kv: (0 if EDIT in kv[1].get('used_in', {}) else 1, kv[0]))
    for name, e in order:
        txt = ' '.join([name, e.get('description', ''), ' '.join(e.get('tags', [])), json.dumps(e.get('selects', [])), e.get('audio_review', '')])
        out.append(f'<article data-search="{E(txt.lower(), quote=True)}"><h2>{E(name)}</h2>'
                   f'<p>{e.get("duration", 0):.1f}s. {e.get("width")}x{e.get("height")}. {E(str(e.get("fps")))} fps.</p>'
                   f'<p>{E(e.get("description", ""))}</p><small>{E(e.get("review_status", ""))}</small>')
        if e.get('audio_review'):
            out.append(f'<p><small>{E(e["audio_review"])}</small></p>')
        out.append('<ul>')
        for s in e.get('selects', []):
            cls = 'v' if 'verified' in s.get('provenance', '') else ''
            out.append(f'<li class="{cls}">{s["start"]:g} to {s["end"]:g}s. {E(s["description"])} <small>{E(s.get("provenance", ""))}</small></li>')
        out.append('</ul>')
        for edit, uses in e.get('used_in', {}).items():
            if uses:
                out.append(f'<p><small>{E(edit)}: ' + '; '.join(f'{u["source_in"]:.2f}-{u["source_out"]:.2f}s as "{E(u["phrase"])}"' for u in uses) + '</small></p>')
        if (e.get('path') or '').startswith('media/'):
            out.append(f'<p><a href="../{E(e["path"])}">Open original</a></p>')
        if e.get('contact_sheet'):
            out.append(f'<details><summary>Contact sheet</summary><img loading="lazy" src="../{E(e["contact_sheet"])}"></details>')
        out.append('</article>')
    out.append('<script>const i=document.querySelector("input"),c=[...document.querySelectorAll("article")];'
               'function f(){let n=0;for(const a of c){a.hidden=!a.dataset.search.includes(i.value.toLowerCase());if(!a.hidden)n++}'
               'document.querySelector("#count").textContent=n+" entries"}i.oninput=f;f()</script>')
    (L / 'index.html').write_text('\n'.join(out), encoding='utf8')
    print('library updated:', len(entries), 'recordings,', len(PICKUPS), 'pickups')


if __name__ == '__main__':
    main()
