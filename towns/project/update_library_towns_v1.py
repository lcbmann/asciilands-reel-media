"""Append the "They built this" recordings, verified selects and usage to the footage library.

python project/update_library_towns_v1.py

Append only: existing recordings, selects, rows and history are never changed or removed.
Writes smithing/library/catalogue.json and smithing/library/shot-log.csv (the shared library).
"""
from pathlib import Path
import csv, json, subprocess

R = Path(__file__).resolve().parent
LIB = R.parent.parent / 'smithing/library'
V = R.parent / 'media/video'
TAG = 'towns-v1'
PROV = 'Claude frame/audio review 2026-10-21 (verified select)'
OWNER = 'Owner timestamp notes 2026-10-21'

# (file, [(start, end, description, provenance)], audio note)
NEW = [
    ('york1', [(19.0, 26.5, 'York now: walk synced to last week (yorkarrival 6.00), pergola town hall ahead', OWNER),
               (26.0, 34.0, 'Walking back into York, wider view', OWNER),
               (55.0, 72.0, 'York orbit over the bridge and tower; used under the end card', OWNER),
               (0.0, 1.0, 'REJECT: settings menu open', PROV)], 'game sound, a few UI clicks'),
    ('york2', [(16.0, 22.5, 'The York walk again with players around', OWNER),
               (0.0, 1.0, 'REJECT: settings menu open', PROV), (23.0, 23.2, 'REJECT: settings menu opens', PROV)],
     'broadband ambience'),
    ('york3', [(3.0, 20.0, 'Walking into the York Lawhouse, each law sign in turn', OWNER),
               (8.3, 10.0, 'First Law: Louie is to be recognized as King of York and its region', PROV),
               (12.0, 17.5, 'Fourth Law: builds near waterways must not block water', PROV),
               (22.0, 22.6, 'REJECT: settings menu open', PROV)], 'footsteps, door'),
    ('york4', [(3.0, 25.0, 'Walking through York, lots of different players around', OWNER),
               (26.0, 31.0, 'Another player crosses in front of the camera', OWNER),
               (0.0, 1.0, 'REJECT: settings menu open', PROV), (34.0, 34.7, 'REJECT: settings menu open', PROV)],
     'broadband ambience'),
    ('world zoom', [(0.0, 2.3, 'Local map of Moss Whitefall Woods (York), closest zoom', PROV),
                    (2.35, 3.05, 'Local map zooming out', PROV),
                    (3.10, 3.45, 'REJECT: dark frames while switching Local to World', PROV),
                    (3.5, 5.0, 'World map zooming out to the whole island', PROV)], 'no sound'),
    ('asterisk', [(9.0, 17.5, 'Asterisk, mayor of Aphelion, walks up and greets: Hello wanderer! Welcome to Aphelion!', PROV),
                  (0.0, 1.0, 'REJECT: settings menu open', PROV), (17.6, 18.3, 'REJECT: Pet panel opens', PROV)],
     'ambience'),
    ('aphelionorbit', [(23.0, 48.3, 'Aphelion from afar, orbit', OWNER),
                       (36.5, 40.0, 'Best panorama: town across the top, one pine at the left', PROV)], 'ambience'),
    ('wandermere', [(7.0, 18.7, 'Wandermere orbit, newer town', OWNER)], 'ambience'),
    ('northmost', [(6.0, 12.4, 'Castle Northmost orbit, stone walls and stall', OWNER)], 'near silent'),
    ('fortredleaf', [(31.0, 43.1, 'Fort Redleaf orbit, gatehouse visible (used mirrored in towns-v1)', OWNER)], 'quiet ambience'),
    ('nordheim', [(8.0, 26.3, 'Nordheim coming into view from the mountain', OWNER),
                  (0.0, 26.3, 'NOTE: Chrome tab and bookmark bars at the top (keep crops below y 212)', PROV)], 'ambience'),
    ('driftwoodmarket', [(42.0, 56.0, 'Walking down the Driftwood market street at night', OWNER),
                         (48.0, 53.0, 'Stall rows fill the upper frame', PROV),
                         (16.0, 21.5, 'REJECT for picture: stall menu open', PROV)], 'footsteps, clicks'),
    ('market', [(18.0, 21.0, 'Regional market (Drift Foxglove Meadows): Buy tab, real listings with sellers', PROV),
                (1.0, 17.5, 'Walking among the market stalls at night before opening the market', PROV)], 'ambience'),
    ('plotforsale', [(12.0, 14.0, 'Walking onto a plot for sale, edges lit, Buy plot prompt', OWNER),
                     (14.08, 14.20, 'REJECT: Town panel jumps from the treasury view to the plots (the flash)', PROV),
                     (14.25, 16.7, 'Plot UI: For sale 50 gold, Anyone can buy, Buy for 50 gold, plot grid', PROV)],
     'ambience'),
    ('awesomesmithing', [(3.0, 6.5, 'Inspect: Masterwork Blacksteel Sword (Master Smith, Masterwork 96)', PROV),
                         (10.0, 13.0, 'Inspect: Rapier Lunge', PROV),
                         (16.0, 18.5, 'Inspect: Needle (Rapier, Etched, Steel, Light Tip-Heavy Long Curved Slack-Quenched)', PROV),
                         (23.0, 26.5, 'Inspect: Quick Dirk (Masterwork 100)', PROV),
                         (42.0, 64.0, 'New Design your blade interface, clicking shape buttons', OWNER),
                         (0.0, 64.0, 'NOTE: local build; Chrome tab and bookmark bars at the top (keep crops below y 212)', PROV)],
     'UI clicks'),
    ('raft', [(3.0, 6.0, 'Bringing the raft up to the dock and stopping; Moor raft prompt', OWNER),
              (0.0, 0.9, 'REJECT: Build menu open', PROV), (6.4, 7.4, 'REJECT: Build menu open', PROV)], 'no sound'),
]
SUPPLIED = [
    dict(file='followup.wav', path='towns/media/audio/followup.wav', kind='voice',
         description='They built this narration, 51.3 s, 48 kHz mono. Whisper hears "exploration won. They\'re" as "exploration 1 that are"; fixed by context.'),
    dict(file='discord-poll-final.png', path='towns/assets/stills/discord-poll-final.png', kind='owner screenshot',
         description='Where should Asciilands go next? poll, 1,506 votes: Smithing 24 %, Exploration and Sailing 19 %.'),
    dict(file='discord-cult-of-akton.png', path='towns/assets/stills/discord-cult-of-akton.png', kind='owner screenshot',
         description="Cleptomane's Cult of Akton forum post (permission given 2026-10-21)."),
    dict(file='wiki-star-iron-order.png', path='towns/assets/stills/wiki-star-iron-order.png', kind='owner screenshot',
         description='awiki.yi5.dev Market row: Star Iron, best buy order 1000g. The live order was gone by filming time.'),
]


def probe(p):
    out = subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration:stream=width,height,r_frame_rate',
                                   '-select_streams', 'v:0', '-of', 'json', str(p)])
    d = json.loads(out)
    s = d['streams'][0]
    return float(d['format']['duration']), s['width'], s['height'], s['r_frame_rate']


def used_ranges():
    tl = json.loads((R / 'edit-v1/timeline.json').read_text())
    out = {}
    for s in tl:
        for sp in s['srcs']:
            if 'v' in sp:
                out.setdefault(sp['v'], []).append(dict(shot=s['at'], source=[sp['t'], round(sp['t'] + s['duration'] * sp['speed'], 3)],
                                                       output=[s['start'], s['end']], mirrored=bool(sp.get('flip'))))
    return out


def main():
    cat = json.loads((LIB / 'catalogue.json').read_text(encoding='utf-8'))
    used = used_ranges()
    by_path = {r.get('original_windows_path'): r for r in cat['recordings']}
    added = 0
    for name, selects, audio in NEW:
        win = f'C:\\Users\\liamb\\Videos\\{name}.mkv'
        rec = by_path.get(win)
        if rec is None:
            dur, w, h, fps = probe(V / f'{name}.mkv')
            rec = dict(index=len(cat['recordings']), path=None, duration=round(dur, 3), width=w, height=h, fps=fps,
                       review_status='Claude contact sheets at 1 s or 0.5 s plus key frames (2026-10-21)', selects=[], used_in={},
                       original_windows_path=win, source_available_in_bundle=False, audio_review=audio)
            cat['recordings'].append(rec)
            added += 1
        have = {(s['start'], s['end'], s['description']) for s in rec['selects']}
        for a, b, desc, prov in selects:
            if (a, b, desc) not in have:
                rec['selects'].append(dict(start=a, end=b, description=desc, provenance=prov))
        rec.setdefault('used_in', {})[TAG] = used.get(name, [])
    for name in ('yorkarrival', 'orbitdesertmeteor'):
        rec = by_path.get(f'C:\\Users\\liamb\\Videos\\{name}.mkv')
        if rec is not None:
            rec.setdefault('used_in', {})[TAG] = used.get(name, [])
    have = {x['file'] for x in cat['supplied']}
    cat['supplied'] += [x for x in SUPPLIED if x['file'] not in have]
    line = f'2026-10-21: {TAG} recordings ({added} new), verified selects and used_in.{TAG} (Claude). Nothing removed.'
    if line not in cat['history']:
        cat['history'].append(line)
    cat['updated'] = '2026-10-21'
    (LIB / 'catalogue.json').write_text(json.dumps(cat, indent=1), encoding='utf-8')

    with open(LIB / 'shot-log.csv', encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
        fields = list(rows[0].keys())
    seen = {(r['filename'], r['start'], r['end'], r['description']) for r in rows}
    new_rows = []
    for name, selects, audio in NEW:
        for a, b, desc, prov in selects:
            key = (f'{name}.mkv', str(a), str(b), desc)
            if key not in seen:
                new_rows.append(dict(filename=f'{name}.mkv', source_in_bundle='False', path='', start=a, end=b, description=desc,
                                     review_status='verified 2026-10-21', provenance=prov, audio_review=audio))
    with open(LIB / 'shot-log.csv', 'a', encoding='utf-8', newline='') as f:
        csv.DictWriter(f, fieldnames=fields).writerows(new_rows)
    print(f'{added} recordings added, {len(new_rows)} shot-log rows appended')


if __name__ == '__main__':
    main()
