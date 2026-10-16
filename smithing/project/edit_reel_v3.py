"""Asciilands smithing reel, revision 3.

Run from anywhere:  python project/edit_reel_v3.py [--prepare] [--qa-only] [--jobs N]

Pipeline: cached Whisper words -> silence-aware narration retime (plus spliced
replacement takes) -> word-anchored shots -> every frame rendered in Python:
gameplay frames or stills are cropped, animated and composited, then captions,
cards and the end card are drawn with Pillow alpha_composite -> raw RGB frames
piped into ffmpeg (-f rawvideo -pix_fmt rgb24 -i -) and encoded with libx264 ->
voice + calibrated real game sound + the theme, one fixed gain to -14 LUFS and a
-1 dBTP limiter -> QA stills.

Editorial choices are data near the top: VOICE_REPLACEMENTS, PAUSES, SHOTS,
looks, ACCENTS, END_CARD. Originals are only read. v1 and v2 outputs are untouched.
"""
from pathlib import Path
import subprocess, json, re, math, sys, hashlib, io, os
from concurrent.futures import ThreadPoolExecutor
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance

R = Path(__file__).resolve().parent
B = R.parent
V = B / 'media/video'
VO = B / 'media/audio/smithingshort.wav'
ASSETS = B / 'assets'
O = R / 'edit-v3'
for d in ['', 'shots', 'stills', 'qa', 'audio']:
    (O / d).mkdir(parents=True, exist_ok=True)
FINAL = R / 'smithing-v3.mp4'
REVIEW = R / 'smithing-v3-review.mp4'
W, H, FPS, SR = 1080, 1920, 30, 48000
FONT = str(ASSETS / 'fonts/Bold.ttf')
WHITE, GOLD, INK = '#fff9e9', '#efcc77', '#08110c'
# Theme colours shared with the earlier shorts (short2/short5 end card).
T_GOLD, T_PAPER, T_INK = (242, 207, 120), (236, 232, 214), (10, 16, 12)
SAFE = dict(left=80, right=1000, top=270, bottom=1480)


def run(args, **kw):
    return subprocess.run([str(x) for x in args], check=True, **kw)


def ff(args, **kw):
    return run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', *args], **kw)


def probe_duration(p):
    return float(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                                          '-of', 'csv=p=0', str(p)]).decode())


def norm(t):
    return re.sub(r'[^a-z0-9]', '', t.lower())


def src(name):
    return V / (name + '.mkv')


def ease(u):                      # smooth in and out, for camera moves
    u = min(1.0, max(0.0, u))
    return u * u * (3 - 2 * u)


def ease_out(u):                  # cubic ease-out, as in the earlier shorts' cards
    u = min(1.0, max(0.0, u))
    return 1 - (1 - u) ** 3


# ---------------------------------------------------------------- narration
WORD_FIXES = [(('ram', 'materials'), 0, 'Rare')]      # matched by context, never by index
PAUSE_DEFAULT = {'.': 0.30, '?': 0.32, '!': 0.30, ',': 0.20, '': 0.16}
PAUSES = {
    'you hammered out?': 0.34,
    'paying for their skill.': 0.85,   # the one intentional beat before the question
}
LEAD_IN = 0.06
TAIL_PAD = 0.08
HEAD_PAD = 0.07
# Replacement takes, spliced in at the measured silences around the phrase.
# Word times are in the take's own seconds (measured from its energy envelope).
VOICE_REPLACEMENTS = [
    dict(phrase='Rare materials are one reason.', file=B / 'media/audio/rarematerials.wav',
         speech=(0.62, 2.10),
         words=[('Rare', 0.64, 0.96), ('materials', 1.04, 1.40), ('are', 1.44, 1.55), ('one', 1.58, 1.72),
                ('reason.', 1.76, 2.08)]),
]


def load_words():
    if not (R / 'words-raw.json').exists():
        from faster_whisper import WhisperModel
        model = WhisperModel('small.en', device='cpu', compute_type='int8', download_root=str(R / 'models'))
        raw = np.frombuffer(subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(VO), '-ar', '16000', '-ac', '1',
                                                     '-f', 'f32le', '-']), dtype=np.float32)
        segs, _ = model.transcribe(raw, word_timestamps=True, beam_size=5,
                                   initial_prompt='Asciilands. Smithing. Star Iron. Player-run economy. Masterwork sword.')
        words = [dict(w=w.word.strip(), s=w.start, e=w.end, p=w.probability) for seg in segs for w in seg.words]
        (R / 'words-raw.json').write_text(json.dumps(words, indent=2))
    words = json.loads((R / 'words-raw.json').read_text())
    text = [norm(x['w']) for x in words]
    for pattern, offset, fix in WORD_FIXES:
        for i in range(len(text) - len(pattern) + 1):
            if tuple(text[i:i + len(pattern)]) == pattern:
                words[i + offset]['w'] = fix
    return words


def silences(path, noise='-35dB', d=0.25):
    r = subprocess.run(['ffmpeg', '-hide_banner', '-i', str(path), '-af', f'silencedetect=noise={noise}:d={d}',
                        '-f', 'null', os.devnull], capture_output=True, text=True)
    st = [float(x) for x in re.findall(r'silence_start: ([\d.]+)', r.stderr)]
    en = [float(x) for x in re.findall(r'silence_end: ([\d.]+)', r.stderr)]
    merged = []
    for a, b in zip(st, en):
        if merged and a - merged[-1][1] < 0.02:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return merged


def pause_for(words, i):
    tail = ' '.join(norm(x['w']) for x in words[max(0, i - 4):i + 1])
    for phrase, sec in PAUSES.items():
        if tail.endswith(' '.join(norm(p) for p in phrase.split())):
            return sec, phrase
    m = re.search(r'[.?!,]$', words[i]['w'])
    return PAUSE_DEFAULT[m.group(0) if m else ''], None


def read_mono(path):
    return np.frombuffer(subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path), '-ar', str(SR), '-ac', '1',
                                                  '-f', 'f32le', '-']), dtype=np.float32).copy()


def lufs_of(a):
    p = O / 'audio/_measure.wav'
    write_wav(p, a.reshape(-1, 1) if a.ndim == 1 else a)
    return loudness(p)['input_i']


def prepare_audio():
    words = load_words()
    audio = read_mono(VO)
    duration = len(audio) / SR
    remove, report, used = [], [], set()
    for a, b in silences(VO):
        if a < 0.05:
            remove.append((0.0, max(0.0, b - LEAD_IN)))
            continue
        if b > duration - 0.05:
            remove.append((a + 0.25, duration))
            continue
        prev = [i for i, w in enumerate(words) if w['e'] <= a + 0.20]
        i = prev[-1] if prev else 0
        target, phrase = pause_for(words, i)
        if phrase:
            used.add(phrase)
        if b - a <= target + 0.03:
            report.append(dict(after=words[i]['w'], at=round(a, 3), kept=round(b - a, 3), target=target))
            continue
        pre = max(TAIL_PAD, target * 0.55)
        post = max(HEAD_PAD, target - pre)
        remove.append((a + pre, b - post))
        report.append(dict(after=words[i]['w'], at=round(a, 3), original=round(b - a, 3), kept=round(pre + post, 3), target=target))
    assert used == set(PAUSES), ('pause phrase not found', set(PAUSES) - used)
    # Segments of the edited voice: ('orig', a, b) or ('take', k, a, b)
    segs, cursor = [], 0.0
    for a, b in remove:
        if a > cursor:
            segs.append(['orig', cursor, a])
        cursor = max(cursor, b)
    if cursor < duration:
        segs.append(['orig', cursor, duration])
    takes, replaced = [], {}
    for k, rep in enumerate(VOICE_REPLACEMENTS):
        i0 = anchor_index(words, ' '.join(rep['phrase'].split()))
        i1 = i0 + len(rep['phrase'].split()) - 1
        # Join inside the measured silences either side of the phrase, keeping the same padding
        # the retime keeps everywhere else (HEAD_PAD before speech, TAIL_PAD after it).
        gaps = silences(VO)
        before = max(b for a, b in gaps if b <= words[i0]['s'] + 0.6) - HEAD_PAD
        after = min(a for a, b in gaps if a >= words[i1]['e'] - 0.3) + TAIL_PAD
        take = read_mono(rep['file'])
        orig_lufs = lufs_of(audio[int(before * SR):int(after * SR)])
        s0, s1 = rep['speech']
        ta, tb = max(0.0, s0 - HEAD_PAD), min(len(take) / SR, s1 + TAIL_PAD)
        gain = orig_lufs - lufs_of(take[int(ta * SR):int(tb * SR)])
        takes.append(take * 10 ** (gain / 20))
        new = []
        for sg in segs:
            if sg[0] != 'orig' or sg[2] <= before or sg[1] >= after:
                new.append(sg)
                continue
            if sg[1] < before:
                new.append(['orig', sg[1], before])
            if not any(x[0] == 'take' and x[1] == k for x in new):
                new.append(['take', k, ta, tb])
            if sg[2] > after:
                new.append(['orig', after, sg[2]])
        segs = new
        replaced[k] = dict(i0=i0, i1=i1, ta=ta, words=rep['words'], gain=round(gain, 2))

    def seg_len(sg):
        return sg[2] - sg[1] if sg[0] == 'orig' else sg[3] - sg[2]

    def mapped(t):
        out = 0.0
        for sg in segs:
            if sg[0] == 'orig':
                if t >= sg[2]:
                    out += sg[2] - sg[1]
                elif t > sg[1]:
                    return out + t - sg[1]
                else:
                    return out
            else:
                out += seg_len(sg)
        return out

    chunks = []
    for sg in segs:
        c = audio[round(sg[1] * SR):round(sg[2] * SR)].copy() if sg[0] == 'orig' else \
            takes[sg[1]][round(sg[2] * SR):round(sg[3] * SR)].copy()
        n = min(int(0.006 * SR), len(c) // 2)
        c[:n] *= np.linspace(0, 1, n)
        c[-n:] *= np.linspace(1, 0, n)
        chunks.append(c)
    voice = np.concatenate(chunks)
    ff(['-f', 'f32le', '-ar', SR, '-ac', 1, '-i', '-', '-c:a', 'pcm_s24le', O / 'voice.wav'], input=voice.tobytes())
    for w in words:
        w.update(source_start=w['s'], source_end=w['e'], s=round(mapped(w['s']), 4), e=round(mapped(w['e']), 4))
    for k, rp in replaced.items():
        pos = 0.0
        for sg in segs:
            if sg[0] == 'take' and sg[1] == k:
                break
            pos += seg_len(sg)
        for j, (txt, a, b) in zip(range(rp['i0'], rp['i1'] + 1), rp['words']):
            words[j].update(w=txt, s=round(pos + a - rp['ta'], 4), e=round(pos + b - rp['ta'], 4), source='replacement take')
    voice_len = len(voice) / SR
    (O / 'words.json').write_text(json.dumps(words, indent=2))
    (O / 'audio-cuts.json').write_text(json.dumps(dict(segments=segs, removed=remove, pauses=report, replacements=
                                                       {k: dict(v, words=None) for k, v in replaced.items()},
                                                       original=duration, voice=voice_len), indent=2, default=str))
    return words, voice_len


# ---------------------------------------------------------------- stills
# A still is a frame taken from a recording, or a supplied image. Looks address
# sources by index: shot['srcs'][i]. Stills can have prepared variants.
STILL_IMAGES = {
    'swordy-john-smith': ASSETS / 'stills/swordy-john-smith-inspect.png',     # owner-supplied Inspect screenshot
}
# Rows in the John_Smith Inspect screenshot (2000x1224).
JS_ROWS = {'metal': (1185, 520, 380, 64), 'maker': (1185, 595, 380, 64)}


def frame_still(name, t):
    p = O / 'stills' / f'{name}@{t:.2f}.png'
    if not p.exists():
        ff(['-ss', f'{t:.3f}', '-i', src(name), '-frames:v', 1, p])
    return Image.open(p).convert('RGB')


def emphasis(img, row):
    """Dim everything but one stats row, and set that row's value in gold."""
    x, y, w, h = row
    out = ImageEnhance.Brightness(img).enhance(0.45)
    band = img.crop((x - 12, y - 8, x + w + 12, y + h + 8))
    a = np.asarray(band).astype(np.float32)
    luma = a.mean(axis=2)
    value = np.zeros_like(luma, dtype=bool)
    value[int(h * 0.42) + 8:, :] = True                      # lower line of the row is the value
    text = (luma > 110) & value
    gold = np.array(T_GOLD, np.float32)
    a[text] = gold * np.clip(luma[text] / 230, 0.7, 1.15)[:, None]
    a[~text] = a[~text] * 1.25
    out.paste(Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)), (x - 12, y - 8))
    return out


def load_source(spec):
    """spec: dict(still=name, variant=...) or dict(frame=(video, t)) or a video dict."""
    if 'still' in spec:
        img = Image.open(STILL_IMAGES[spec['still']]).convert('RGB')
        if spec.get('variant'):
            img = emphasis(img, JS_ROWS[spec['variant']])
        return img
    if 'frame' in spec:
        img = frame_still(*spec['frame'])
        return emphasis(img, spec['row']) if spec.get('row') else img
    return None


# ---------------------------------------------------------------- looks
def world(cx, cy, h0=1280, h1=None, cx1=None, cy1=None, t0=0, t1='e'):
    """A 9:16 window straight out of the gameplay, with an optional slow push."""
    h1 = h1 or h0
    cx1 = cx if cx1 is None else cx1
    cy1 = cy if cy1 is None else cy1

    def rect(x, y, h):
        w = h * 9 / 16
        return (x - w / 2, y - h / 2, w, h)
    return dict(bg=None, layers=[dict(src=[(t0, rect(cx, cy, h0)), (t1, rect(cx1, cy1, h1))],
                                      dst=[(0, (0, 0, W, H))], ease='linear')])


FORGE_WINDOW = (410, 150, 1740, 1140)
FORGE_FIT = ((1500, 465, 625, 100), (80, 300, 920, 147))
FORGE_CONTROLS = ((448, 548, 1030, 150), (0, 468, 1080, 157))
FORGE_ANVIL = ((640, 700, 830, 465), (0, 645, 1080, 605))


def wide_dst(rect):
    x, y, w, h = rect
    k = W / FORGE_WINDOW[2]
    return ((x - FORGE_WINDOW[0]) * k, 560 + (y - FORGE_WINDOW[1]) * k, w * k, h * k)


def forge(reveal_at=None, reveal_len=0.9):
    parts = [FORGE_FIT, FORGE_CONTROLS, FORGE_ANVIL]
    if reveal_at is None:
        return dict(bg='blur', layers=[dict(src=[(0, s)], dst=[(0, d)]) for s, d in parts])
    layers = [dict(src=[(0, FORGE_WINDOW)], dst=[(0, wide_dst(FORGE_WINDOW))],
                   alpha=[(0, 1), (reveal_at, 1), (f'{reveal_at}+{reveal_len * .6}', 0)])]
    for s, d in parts:
        layers.append(dict(src=[(0, s)], dst=[(0, wide_dst(s)), (reveal_at, wide_dst(s)), (f'{reveal_at}+{reveal_len}', d)],
                           alpha=[(0, 0), (reveal_at, 0), (f'{reveal_at}+0.12', 1)]))
    return dict(bg='blur', layers=layers)


INSPECT_SWORD = ((905, 335, 360, 870), (80, 295, 397, 960))
INSPECT_STATS = ((1488, 470, 384, 610), (500, 400, 499, 793))
STATS_MAKER = (1488, 696, 384, 222)
CLEAVER_MAKER_ROW = (1492, 702, 372, 64)          # MAKER Smither in the Cleaver Inspect at full resolution
CLEAVER_MAKER = (1490, 647, 300, 173.5)           # camera centred on that row
STATS_BIG = (80, 420, 920, 532)
SWORD_HERO = (342, 300, 396, 957)        # the kept sword, centred, no stats


def inspect_detail(steps):
    t0 = steps[0][0]
    src_k = [(0, INSPECT_STATS[0]), (t0, INSPECT_STATS[0]), (f'{t0}+0.45', steps[0][1])]
    dst_k = [(0, INSPECT_STATS[1]), (t0, INSPECT_STATS[1]), (f'{t0}+0.45', STATS_BIG)]
    for (_, prev), (t, rect) in zip(steps, steps[1:]):
        src_k += [(f'{t}-0.05', prev), (f'{t}+0.4', rect)]
    return dict(bg='blur', layers=[
        dict(src=[(0, INSPECT_SWORD[0])], dst=[(0, INSPECT_SWORD[1])], alpha=[(0, 1), (t0, 1), (f'{t0}+0.3', 0)]),
        dict(src=src_k, dst=dst_k)])


def maker_focus(t0, emph_at):
    """Inspect, then the camera centres on the MAKER row; a still of the same window with the
    panel dimmed and the maker in gold fades in over the live frame."""
    look = inspect_detail([(t0, CLEAVER_MAKER)])
    stats = look['layers'][1]
    look['layers'].append(dict(i=1, src=stats['src'], dst=stats['dst'], alpha=[(0, 0), (emph_at, 0), (f'{emph_at}+0.4', 1)]))
    return look


def sword_hero():
    return dict(bg='blur', layers=[dict(src=[(0, INSPECT_SWORD[0])], dst=[(0, SWORD_HERO)])])


def shape_match():
    """Push into the finished steel piece so its blade lands where the kept sword appears next."""
    tip, guard_y, cx = 643, 920, 1279          # finished-piece preview in smithbadsteel at 64.5s
    (sx, sy, sw, sh), (dx, dy, dw, dh) = INSPECT_SWORD[0], SWORD_HERO
    ks = dh / sh
    out_tip, out_guard, out_cx = dy + (349 - sy) * ks, dy + (979 - sy) * ks, dx + (1084 - sx) * ks
    k = (out_guard - out_tip) / (guard_y - tip)
    end = (cx - out_cx / k, tip - out_tip / k, W / k, H / k)
    return dict(bg=None, layers=[dict(src=[(0, (893, 60, 771, 1371)), ('e', end)], dst=[(0, (0, 0, W, H))])])


def cam(cx, cy, w):
    """9:16 camera rectangle over a still, centred on (cx, cy), w source pixels wide."""
    h = w * 16 / 9
    return (cx - w / 2, cy - h / 2, w, h)


JS_SWORD = cam(782, 659, 484)            # the whole rapier, full frame
JS_METAL = cam(1370, 551, 380)
JS_MAKER = cam(1345, 626, 330)


def maker_story(steps, emphasis_at):
    """Camera over the John_Smith Inspect screenshot. steps: [(time, camera)]. The
    emphasised variant (dimmed panel, gold maker) fades in at emphasis_at."""
    keys = [(0, steps[0][1])]
    for (_, prev), (t, rect) in zip(steps, steps[1:]):
        keys += [(f'{t}-0.05', prev), (f'{t}+0.45', rect)]
    layers = [dict(i=0, src=keys, dst=[(0, (0, 0, W, H))])]
    for i, at in emphasis_at:
        layers.append(dict(i=i, src=keys, dst=[(0, (0, 0, W, H))], alpha=[(0, 0), (at, 0), (f'{at}+0.4', 1)]))
    return dict(bg=None, layers=layers)


def follow(points, h=300):
    """Crop keyframes that keep one walking character centred: points are (t, x, y) in source pixels."""
    w = h * 450 / 465
    return [(t, (x - w / 2, y - h / 2, w, h)) for t, x, y in points]


def tiles_pop(tiles, stagger=0.18, start=0.0, length=0.45):
    """Collage: each tile (source index, src rect, dst rect) scales up from 90 % and fades in."""
    layers = []
    for k, (i, s, d) in enumerate(tiles):
        t = start + k * stagger
        x, y, w, h = d
        small = (x + w * 0.05, y + h * 0.05, w * 0.9, h * 0.9)
        layers.append(dict(i=i, src=s if isinstance(s, list) else [(0, s)], dst=[(0, small), (t, small), (t + length, d)],
                           alpha=[(0, 0), (t, 0), (t + length * 0.7, 1)], shadow=True,
                           ease='linear' if isinstance(s, list) else None))   # following crops move at walking pace
    return dict(bg='blur', layers=layers)


def slide_in(items):
    """items: (source index, src rect, dst rect, start time). Each rises 60 px and fades in."""
    layers = []
    for i, s, d, t in items:
        x, y, w, h = d
        low = (x, y + 60, w, h)
        layers.append(dict(i=i, src=[(0, s)], dst=[(0, low), (t, low), (f'{t}+0.45', d)],
                           alpha=[(0, 0), (t, 0), (f'{t}+0.3', 1)], shadow=True))
    return dict(bg='blur', layers=layers)


def materials():
    return dict(bg='blur', layers=[
        dict(src=[(0, (868, 362, 762, 112))], dst=[(0, (80, 300, 920, 135))]),
        dict(src=[(0, (1772, 372, 345, 520))], dst=[(0, (272, 455, 537, 810))])])


def naming():
    return dict(bg='blur', layers=[
        dict(src=[(0, (1503, 590, 620, 120))], dst=[(0, (82, 320, 917, 178))]),
        dict(src=[(0, (1100, 715, 370, 440))], dst=[(0, (244, 540, 592, 704))])])


CRAFT_PANEL = (1606, 590, 520, 730)          # Stone Pickaxe recipe: ingredients and Make button
CRAFT_RESULT = (1629, 655, 470, 660)          # gentle push toward the button and the "+1 Stone Pickaxe" line, inside the panel


def crafting(push_at=None):
    s = [(0, CRAFT_PANEL)] if push_at is None else [(0, CRAFT_PANEL), (push_at, CRAFT_PANEL), (f'{push_at}+0.8', CRAFT_RESULT)]
    return dict(bg='blur', layers=[dict(src=s, dst=[(0, (202, 305, 676, 949))])])


# Weapons and tools, all real item art from the recordings.
SWORD_CROP = (905, 335, 360, 870)
TOOL_TILE = lambda k: (924 + 160 * k, 584, 148, 146)   # Stone Axe, Stone Pickaxe, Stone Sickle, Wooden Hammer


def weapons_and_tools():
    items = [(i, SWORD_CROP, (95 + 316 * i, 300, 259, 626), 0.05 + 0.14 * i) for i in range(3)]
    items += [(3, TOOL_TILE(k), (80 + 240 * k, 985, 200, 197), f'@and tools+{0.1 * k:.2f}') for k in range(4)]
    return slide_in(items)


# ---------------------------------------------------------------- shots
# at: spoken words that open the shot; a [bracketed] word marks the exact cut word.
# srcs: sources (video dict(v=, t=, speed=) or still dict). Single video shots use v/t/speed.
# speed 'fit:<source out>' derives speed from the narration length; t 'cont' continues.
# sound: None | ('live',) real recorded audio | ('hits', min_gap) gated real strikes |
#        ('clicks', min_gap) gated UI sounds only (for clips with in-game music under them).
CUT_LEAD = 0.07
SHOTS = [
    dict(at='I want players', v='yorkarrival', t=6.00, look=world(1280, 750, 1280, 1230),
         note='Walk through York starts at 6.00; turn ends 5.93.'),
    dict(at='cross a massive map', v='mapzoomout', t=6.45, speed='fit:8.85', look=world(1120, 830, 1220, 1160),
         note='World atlas zooming out to the whole explored world.'),
    dict(at='buy a [sword.]', srcs=[dict(still='swordy-john-smith'), dict(still='swordy-john-smith', variant='metal'),
                                     dict(still='swordy-john-smith', variant='maker')],
         look=maker_story([(0, JS_SWORD), ("@what it's made", JS_METAL), ('@because of who', JS_MAKER)],
                          [(1, "@what it's made"), (2, '@because of who')]),
         note='Owner-supplied Swordy Inspect: full-screen sword, then METAL Steel, then MAKER John_Smith.'),
    dict(at="I'm building Asciilands", srcs=[dict(v='smithersmith', t=6.3), dict(v='2026-10-04 17-31-45', t=17.5),
                                            dict(v='2026-10-04 17-31-45', t=50.9), dict(v='2026-10-04 17-31-45', t=49.0)],
         look=tiles_pop([
             (0, (960, 350, 581, 600), (80, 300, 450, 465)),                       # Smither: brown apron, moustache
             (1, follow([(0, 1216, 1290), (0.1, 1216, 1290), (0.9, 1200, 1136), (1.7, 1178, 950), (2.5, 1182, 830), (2.83, 1185, 812)]),
              (550, 300, 450, 465)),                                                # Kestrel: white hair, red jacket, backpack
             (2, follow([(0, 2290, 620), (0.3, 2176, 624), (1.1, 1840, 656), (1.9, 1466, 672), (2.83, 1420, 650)]),
              (80, 785, 450, 465)),                                                 # Ash: red hair, blue tunic
             (3, follow([(0, 1270, 600), (2.83, 1270, 600)]), (550, 785, 450, 465))]),  # dark hair, blue-grey shirt
         note='Four different characters, each tile following its own: Smither (smithy), Kestrel, Ash and a '
              'dark-haired player from 2026-10-04 17-31-45.mkv.'),
    dict(at='Weapons and tools', srcs=[dict(frame=('smithtwoswords', 85.0)), dict(frame=('smithtwoswords', 160.8)),
                                      dict(frame=('smithbadsteel', 77.0)), dict(frame=('oneclickstonepickaxe', 1.0))],
         look=weapons_and_tools(), note='Iron, bronze and steel swords, then four real tools.'),
    dict(at='But for that', v='yorksmith', t=14.55, look=world(1290, 700, 1280, 900, 1290, 690),
         note='York Public Blacksmith sign, Built by Louie.'),
    dict(at='people need a', v='smithersmith', t=4.60, look=world(1265, 740, 1280, 1060, 1250, 700),
         note='Walk up to the door, enter at 5.43, Smither is inside.'),
    dict(at='Rare materials', v='orbitdesertmeteor', t=12.0, look=world(1280, 760, 1280, 1150),
         note='Voice: replacement take rarematerials.wav.'),
    dict(at='Better damage and', v='inventorymaterials', t=17.95, speed='fit:20.75', look=materials()),
    dict(at='But if anyone', v='oneclickstonepickaxe', t=0.0, speed='fit:1.25', look=crafting(),
         note='One-click Stone Pickaxe recipe: ingredients first.'),
    dict(at='can click a', v='oneclickstonepickaxe', t='cont', look=crafting(), sound=('clicks', 0.15),
         note='First click at source 1.55 lands on "click"; second identical craft at 3.67.'),
    dict(at="there's not much", v='oneclickstonepickaxe', t='cont', speed='fit:4.80', look=crafting(push_at=0.0),
         note='Hold on "+1 Stone Pickaxe".'),
    dict(at='I wanted being', v='smithbadsteel', t=3.00, look=forge(reveal_at='@So in Asciilands'), sound=('live',),
         note='Real-time steel forging with its own sound; full UI first.'),
    dict(at='Two players can', v='smithbadsteel', t='cont', speed='fit:55.60', look=forge(), sound=('hits', 0.42),
         note='Timelapse of the same steel piece to Fit 97%.'),
    dict(at='And that shape', v='smithbadsteel', t=64.48, speed='fit:65.45', look=shape_match(),
         note='Finished steel piece, matched to the kept sword. No recorded sound: it carries the game jingle.'),
    dict(at="That's the actual", v='smithbadsteel', t=75.55, look=sword_hero(),
         note='The same imperfect blade, centred and large.'),
    dict(at='You can give', v='smithbadsteel', t=57.40, speed='fit:60.75', look=naming(), note='Typing the name Swordy.'),
    dict(at="and your maker's", srcs=[dict(still='swordy-john-smith'), dict(still='swordy-john-smith', variant='maker')],
         look=maker_story([(0, JS_SWORD), ("@maker's mark", JS_MAKER)], [(1, "@maker's mark+0.25")]),
         note='Maker mark: zoom to MAKER John_Smith, panel dims, name turns gold.'),
    dict(at='The basic metals', v='smithtwoswords', t=118.0, speed='fit:145.0', look=forge(), sound=('hits', 0.30)),
    dict(at='The higher -tier', v='starironhard', t=4.60, look=forge(), sound=('hits', 0.25)),
    dict(at='A good smith', v='yorksmith', t=10.5, look=world(1280, 660, 1280, 1180)),
    dict(at='Someone asks who', srcs=[dict(v='smithtwoswords', t=83.62),
                                     dict(frame=('smithtwoswords', 85.0), row=CLEAVER_MAKER_ROW)],
         look=maker_focus(0.3, '@forged your+0.1'), note='Cleaver: zoom to MAKER, centred, panel dims and the name turns gold.'),
    dict(at='you point them', v='smithersmith', t=8.17, look=world(1250, 680, 1100, 860, 1240, 650)),
    dict(at="You're not just", v='orbitsnowmeteor', t=48.0, look=world(1280, 780, 1280, 1150)),
    dict(at="You're paying for their", v='smithtwoswords', t=38.0, look=forge(), sound=('hits', 0.2),
         note='Iron arming sword, real-time strikes carry the beat and the question.'),
    dict(at='Tell me in', v='yorksmith', t=21.0, look=world(1280, 700, 1280, 1200), endcard=True, lead=0.15),
]
END_TAIL = 2.5
# Game sound sits this many LU under the narration's integrated loudness (measured per shot),
# heat whooshes are cut further, and while a word is spoken the game ducks by a fixed amount.
GAME_UNDER_VOICE = {'live': 11.0, 'hits': 11.0, 'clicks': 19.0}
WHOOSH_CUT_DB = 10.0
SPEECH_DUCK_DB = 6.0
MUSIC_DB = -27.0          # one level for the whole reel; only game sound is balanced under the voice
ACCENTS = ['SWORD', 'SWORDS', 'MAP', 'MASSIVE', 'WHO', 'ECONOMY', 'PLAYERS', 'INTERESTING', 'RARE', 'DURABILITY',
           'DAMAGE', 'CLICK', 'BUTTON', 'SAME', 'VALUE', 'GOOD', 'BLADE', 'HEAT', 'STRIKE', 'SHAPE', 'METAL', 'STEEL',
           'DIFFERENT', 'FORGING', 'HARDER', 'LONGER', 'HAMMERED', 'KEEP', 'NAME', "MAKER'S", 'MARK', 'BUYS',
           'FORGIVING', 'SERIOUS', 'SKILL', 'SHOP', 'REPUTATION', 'MASTERWORK', 'CUSTOMER', 'MATERIALS', 'TIME',
           'REWARDING', 'CHORE', 'COMMENTS']


def anchor_index(words, phrase):
    text = [norm(x['w']) for x in words]
    raw = phrase.split()
    parts = [norm(x) for x in raw]
    hits = [i for i in range(len(text) - len(parts) + 1) if text[i:i + len(parts)] == parts]
    assert len(hits) == 1, (phrase, hits)
    marked = [k for k, x in enumerate(raw) if x.startswith('[')]
    return hits[0] + (marked[0] if marked else 0)


def frame(t):
    return round(t * FPS) / FPS


def build_timeline(words, voice_len):
    total = frame(voice_len + END_TAIL)
    tl = []
    for s in SHOTS:
        wi = anchor_index(words, s['at'])
        start = 0.0 if wi == 0 else frame(words[wi]['s'] - s.get('lead', CUT_LEAD))
        tl.append(dict(s, word=wi, start=start))
    for i, s in enumerate(tl):
        s['end'] = tl[i + 1]['start'] if i + 1 < len(tl) else total
        s['duration'] = round(s['end'] - s['start'], 4)
        s['frames'] = round(s['end'] * FPS) - round(s['start'] * FPS)
        if 'v' in s:
            if s['t'] == 'cont':
                assert tl[i - 1].get('v') == s['v']
                s['t'] = tl[i - 1]['t_out']
            sp = s.get('speed', 1)
            if isinstance(sp, str):
                sp = (float(sp.split(':')[1]) - s['t']) / s['duration']
            s['speed'] = round(sp, 5)
            s['t_out'] = round(s['t'] + s['duration'] * s['speed'], 4)
            s['srcs'] = [dict(v=s['v'], t=s['t'], speed=s['speed'])]
        for sp in s['srcs']:
            if 'v' in sp:
                sp.setdefault('speed', 1)
                sp['file'] = str(src(sp['v']))
                out = sp['t'] + s['duration'] * sp['speed']
                assert out <= probe_duration(sp['file']) - 0.05, (s['at'], sp['v'], out)
                assert 0.25 < sp['speed'] < 30, (s['at'], sp['speed'])
    (O / 'timeline.json').write_text(json.dumps([{k: v for k, v in s.items() if k != 'look'} for s in tl], indent=2, default=str))
    return tl, total


def resolve_time(expr, dur, words, shot_start):
    if isinstance(expr, (int, float)):
        return float(expr)
    e = str(expr)
    e = re.sub(r"@([^+\-]+[^+\-\s])", lambda m: str(words[anchor_index(words, m.group(1))]['s'] - shot_start), e)
    e = e.replace('e', str(dur)) if re.fullmatch(r'[e0-9.+\- ]+', e) else e
    return float(eval(e, {'__builtins__': {}}))


def keyed(frames_, t, linear=False):
    if t <= frames_[0][0]:
        return frames_[0][1]
    for (t0, v0), (t1, v1) in zip(frames_, frames_[1:]):
        if t0 <= t <= t1:
            u = (t - t0) / (t1 - t0) if t1 > t0 else 1.0
            u = u if linear else ease(u)
            if isinstance(v0, tuple):
                return tuple(a + (b - a) * u for a, b in zip(v0, v1))
            return v0 + (v1 - v0) * u
    return frames_[-1][1]


def compile_look(look, shot, words):
    out = dict(bg=look.get('bg'), layers=[])
    conv = lambda seq: sorted([(resolve_time(t, shot['duration'], words, shot['start']), v) for t, v in seq], key=lambda x: x[0])
    for layer in look['layers']:
        out['layers'].append(dict(i=layer.get('i', 0), src=conv(layer['src']), dst=conv(layer['dst']),
                                  alpha=conv(layer.get('alpha', [(0, 1)])), linear=layer.get('ease') == 'linear',
                                  shadow=layer.get('shadow', False)))
    return out


# ---------------------------------------------------------------- captions (drawn per frame)
CAP_SIZE, CAP_MAX_W, CAP_TOP = 74, 860, 1270
STROKE = 9
WEAK_END = {'a', 'an', 'the', 'to', 'of', 'and', 'in', 'with', 'for', 'from', 'at', 'your', 'their', 'into', 'on',
            'just', 'can', 'be', 'by', 'it', 'its', 'im', 'that', 'this', 'does', 'or', 'but', 'if', 'some'}
STRONG_START = {'but', 'and', 'or', 'just', 'instead', 'when', 'to', 'for', 'into'}
CAPTION_STARTS = ['because of what', 'can click a button', 'with very different', 'when someone else',
                  'mark stays', 'and end up']


def font(size=CAP_SIZE):
    return ImageFont.truetype(FONT, size)


def measure(text, f):
    b = ImageDraw.Draw(Image.new('L', (1, 1))).textbbox((0, 0), text, font=f, stroke_width=STROKE)
    return b[2] - b[0]


def group_captions(words, total):
    f = font()
    tokens = []
    for w in words:
        if w['w'].startswith('-') and tokens:
            tokens[-1] = dict(tokens[-1], w=tokens[-1]['w'] + w['w'], e=w['e'])
        else:
            tokens.append(dict(w))
    phrases, cur = [], []
    for w in tokens:
        cur.append(w)
        if re.search(r'[.,?!]$', w['w']):
            phrases.append(cur)
            cur = []
    if cur:
        phrases.append(cur)

    def splits(k):
        return [[]] if k == 0 else [[c] + rest for c in (2, 3, 4, 5) if c <= k for rest in splits(k - c)]
    groups = []
    for ph in phrases:
        n = len(ph)
        if n <= 4:
            groups.append(ph)
            continue
        best = None
        for sizes in splits(n):
            pos, cost = 0, 0.0
            for size in sizes:
                chunk = ph[pos:pos + size]
                pos += size
                if pos < n and norm(chunk[-1]['w']) in WEAK_END:
                    cost += 3
                if pos < n and norm(ph[pos]['w']) in STRONG_START:
                    cost -= 1
                if measure(' '.join(x['w'] for x in chunk).upper(), f) > CAP_MAX_W:
                    cost += 2
                head = ' '.join(norm(x['w']) for x in chunk)
                if any(head.startswith(' '.join(norm(p) for p in c.split())) for c in CAPTION_STARTS):
                    cost -= 5
                cost += abs(size - 3) * 0.4 + (3 if size == 5 else 0)
                if (ph[pos]['s'] if pos < n else chunk[-1]['e']) - chunk[0]['s'] < 0.4:
                    cost += 6
            cost += len(sizes) * 0.2
            if best is None or cost < best[0]:
                best = (cost, sizes)
        pos = 0
        for size in best[1]:
            groups.append(ph[pos:pos + size])
            pos += size
    caps = []
    for i, g in enumerate(groups):
        nxt = groups[i + 1][0]['s'] if i + 1 < len(groups) else total
        end = nxt if nxt - g[-1]['e'] < 0.45 else g[-1]['e'] + 0.25
        caps.append(dict(text=' '.join(x['w'] for x in g).upper(), start=round(g[0]['s'], 3), end=round(end, 3)))
    caps[-1]['end'] = total
    return caps


def caption_image(text):
    f = font()
    words = text.split()
    lines, row = [], []
    for w in words:
        if row and measure(' '.join(row + [w]), f) > CAP_MAX_W:
            lines.append(row)
            row = [w]
        else:
            row.append(w)
    lines.append(row)
    accent = next((w for a in ACCENTS for w in words if norm(w).upper() == norm(a).upper()), None)
    lh = int(CAP_SIZE * 1.10)
    im = Image.new('RGBA', (W, lh * len(lines) + 40))
    shadow = Image.new('RGBA', im.size)
    d, ds = ImageDraw.Draw(im), ImageDraw.Draw(shadow)
    space = d.textlength(' ', font=f)
    for li, line in enumerate(lines):
        width = sum(d.textlength(w, font=f) for w in line) + space * (len(line) - 1)
        x, y = (W - width) / 2, 10 + li * lh
        for w in line:
            ds.text((x + 4, y + 8), w, font=f, fill=(0, 0, 0, 170), stroke_width=STROKE + 3, stroke_fill=(0, 0, 0, 170))
            d.text((x, y), w, font=f, fill=GOLD if w == accent else WHITE, stroke_width=STROKE, stroke_fill=INK)
            x += d.textlength(w, font=f) + space
    shadow = shadow.filter(ImageFilter.GaussianBlur(7))
    shadow.alpha_composite(im)
    box = shadow.getbbox()
    return shadow.crop(box), (box[0], CAP_TOP + box[1]), len(lines)


def build_caption_layers(caps):
    for c in caps:
        im, (x, y), n = caption_image(c['text'])
        assert x >= SAFE['left'] and x + im.width <= SAFE['right'] and y >= SAFE['top'] and y + im.height <= SAFE['bottom'], c
        c.update(image=im, x=x, y=y, w=im.width, h=im.height, lines=n)
    return caps


# ---------------------------------------------------------------- end card (same as the earlier shorts)
ATLAS = Image.open(ASSETS / 'fonts/cp437_24x24.png').convert('L')
ADVANCE = 19


def bitmap_text(text, scale, colour):
    """The game's own CP437 bitmap font, as used by the earlier shorts' end cards."""
    img = Image.new('RGBA', (max(1, ((len(text) - 1) * ADVANCE + 24) * scale), 24 * scale), (0, 0, 0, 0))
    for i, ch in enumerate(text):
        code = ord(ch) if ord(ch) < 256 else 63
        g = ATLAS.crop(((code % 16) * 24, (code // 16) * 24, (code % 16 + 1) * 24, (code // 16 + 1) * 24))
        g = g.point(lambda v: 255 if v else 0).resize((24 * scale, 24 * scale), Image.NEAREST)
        img.paste(colour + (255,), (i * ADVANCE * scale, 0), g)
    return img


def faded(img, a):
    if a >= 1:
        return img
    img = img.copy()
    img.putalpha(img.getchannel('A').point(lambda v: int(v * max(0, a))))
    return img


_MARK = None


def end_card(bg, t):
    """Blur 14, 74 % toward ink, then wordmark, FREE IN YOUR BROWSER and the URL, each
    fading in over 0.45 s and rising 22 px, staggered 0 / 0.3 / 0.55 s."""
    global _MARK
    if _MARK is None:
        mark = Image.open(ASSETS / 'brand/asciilands-wordmark-cut.webp').convert('RGBA')
        _MARK = mark.resize((900, round(mark.height * 900 / mark.width)), Image.LANCZOS)
    img = Image.blend(bg.filter(ImageFilter.GaussianBlur(14)), Image.new('RGB', (W, H), T_INK), .74).convert('RGBA')

    def put(im, y, start):
        a = ease_out((t - start) / .45)
        if a > 0:
            img.alpha_composite(faded(im, a), ((W - im.width) // 2, int(y + 22 * (1 - a))))
    put(_MARK, H * .36, 0)
    put(bitmap_text('FREE IN YOUR BROWSER', 2, T_GOLD), H * .53, .3)
    put(bitmap_text('asciilands.io', 2, T_PAPER), H * .58, .55)
    return img.convert('RGB')


# ---------------------------------------------------------------- picture
def background(img):
    small = img.reduce(10) if img.width >= 2000 else img.resize((256, round(256 * img.height / img.width)))
    w = small.height * 9 / 16
    x0 = (small.width - w) / 2
    bg = small.resize((108, 192), Image.BILINEAR, box=(x0, 0, x0 + w, small.height)).filter(ImageFilter.GaussianBlur(5))
    bg = bg.resize((W, H), Image.BILINEAR)
    return Image.blend(bg, Image.new('RGB', (W, H), (6, 14, 9)), 0.55)


class VideoReader:
    def __init__(self, sp, n):
        self.size = 2560 * 1440 * 3
        self.p = subprocess.Popen(['ffmpeg', '-v', 'error', '-ss', f"{sp['t']:.4f}", '-i', sp['file'],
                                   '-vf', f"setpts=(PTS-STARTPTS)/{sp['speed']:.6f},fps={FPS}",
                                   '-frames:v', str(n), '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], stdout=subprocess.PIPE)
        self.last = None

    def next(self):
        buf = self.p.stdout.read(self.size)
        if len(buf) < self.size:                   # only ever the final frame or two
            return self.last
        self.last = Image.frombuffer('RGB', (2560, 1440), buf)
        return self.last

    def close(self):
        self.p.stdout.close()
        self.p.wait()


_SHADOW = {}


def drop_shadow(w, h):
    key = (w, h)
    if key not in _SHADOW:
        pad = 30
        im = Image.new('RGBA', (w + pad * 2, h + pad * 2))
        ImageDraw.Draw(im).rectangle((pad, pad + 8, pad + w, pad + h + 8), fill=(0, 0, 0, 170))
        _SHADOW[key] = (im.filter(ImageFilter.GaussianBlur(14)), pad)
    return _SHADOW[key]


def compose(images, look, t):
    canvas = background(images[0]) if look['bg'] else Image.new('RGB', (W, H))
    for layer in look['layers']:
        a = keyed(layer['alpha'], t)
        if a <= 0.01:
            continue
        img = images[layer['i']]
        sx, sy, sw, sh = keyed(layer['src'], t, layer['linear'])
        dx, dy, dw, dh = keyed(layer['dst'], t, layer['linear'])
        cx, sw = sx + sw / 2, sh * dw / dh              # crop aspect always follows the destination
        sx = min(max(cx - sw / 2, 0), img.width - sw)
        sy = min(max(sy, 0), img.height - sh)
        piece = img.resize((max(1, round(dw)), max(1, round(dh))), Image.LANCZOS, box=(sx, sy, sx + sw, sy + sh))
        px, py = round(dx), round(dy)
        if layer['shadow']:
            sh_img, pad = drop_shadow(piece.width, piece.height)
            canvas = canvas.convert('RGBA')
            canvas.alpha_composite(faded(sh_img, a), (px - pad, py - pad))
            canvas = canvas.convert('RGB')
        if a < 0.99:
            region = canvas.crop((px, py, px + piece.width, py + piece.height))
            canvas.paste(Image.blend(region, piece, a), (px, py))
        else:
            canvas.paste(piece, (px, py))
    return canvas


def overlay_captions(frame_img, now, caps):
    out = frame_img.convert('RGBA')
    for c in caps:
        if c['start'] <= now < c['end']:
            out.alpha_composite(c['image'], (c['x'], c['y']))
    return out.convert('RGB')


def shot_signature(i, s, look, caps):
    shown = [(c['text'], c['start'], c['end']) for c in caps if c['end'] > s['start'] and c['start'] < s['end']]
    key = dict(look=look, srcs=s['srcs'], frames=s['frames'], start=s['start'], caps=shown, endcard=s.get('endcard'),
               total=s.get('total'), v=4)
    return hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()


def render_shot(i, s, words, caps, prev_last=None):
    dest = O / 'shots' / f'{i:02d}.mp4'
    last_png = O / 'shots' / f'{i:02d}-last.png'
    look = compile_look(s['look'], s, words)
    sig = shot_signature(i, s, look, caps)
    stamp = dest.with_suffix('.sha')
    if dest.exists() and stamp.exists() and last_png.exists() and stamp.read_text() == sig:
        return dest
    n = s['frames']
    readers, stills = [], []
    for sp in s['srcs']:
        if 'v' in sp:
            readers.append(VideoReader(sp, n))
            stills.append(None)
        else:
            readers.append(None)
            stills.append(load_source(sp))
    enc = subprocess.Popen(['ffmpeg', '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{W}x{H}',
                            '-r', str(FPS), '-i', '-', '-c:v', 'libx264', '-preset', 'medium', '-crf', '16',
                            '-pix_fmt', 'yuv420p', '-threads', '2', str(dest)], stdin=subprocess.PIPE)
    raw = None
    first = round(s['start'] * FPS)
    for f in range(n):
        t = f / FPS
        now = (first + f) / FPS
        images = [rd.next() if rd else st for rd, st in zip(readers, stills)]
        raw = compose(images, look, t)
        out = raw
        if s.get('endcard'):
            # Gameplay keeps running underneath the card, which dissolves in from the previous shot.
            out = end_card(raw, t)
            if t < .6 and prev_last is not None:
                out = Image.blend(prev_last, out, ease_out(t / .6))
            if now > s['total'] - .5:
                out = Image.blend(out, Image.new('RGB', (W, H)), min(1, (now - (s['total'] - .5)) / .5))
        enc.stdin.write(overlay_captions(out, now, caps).tobytes())
    for rd in readers:
        if rd:
            rd.close()
    enc.stdin.close()
    assert enc.wait() == 0
    raw.save(last_png)
    stamp.write_text(sig)
    print(f'shot {i:02d} {s["at"]!r} {s["duration"]:.2f}s', flush=True)
    return dest


def render_picture(tl, words, caps, total, jobs):
    for s in tl:
        s['total'] = total
    body = [(i, s) for i, s in enumerate(tl) if not s.get('endcard')]
    with ThreadPoolExecutor(jobs) as ex:
        list(ex.map(lambda a: render_shot(a[0], a[1], words, caps), body))
    for i, s in enumerate(tl):
        if s.get('endcard'):
            prev = Image.open(O / 'shots' / f'{i - 1:02d}-last.png').convert('RGB')
            render_shot(i, s, words, caps, prev_last=prev)
    (O / 'concat.txt').write_text('\n'.join(f"file 'shots/{i:02d}.mp4'" for i in range(len(tl))))
    ff(['-f', 'concat', '-safe', 0, '-i', O / 'concat.txt', '-c', 'copy', O / 'picture.mp4'])


# ---------------------------------------------------------------- sound
def read_audio(path, ss=0.0, t=None, ch=2):
    a = ['ffmpeg', '-v', 'error', '-ss', f'{ss:.4f}'] + (['-t', f'{t:.4f}'] if t else []) + \
        ['-i', str(path), '-vn', '-ac', str(ch), '-ar', str(SR), '-f', 'f32le', '-']
    return np.frombuffer(subprocess.check_output(a), dtype=np.float32).reshape(-1, ch).copy()


def onsets(path, a, b):
    """Sharp onsets in [a, b]: (time, is_strike). Strikes ring at a pitch; the heat whoosh is
    noise. Spectral flatness separates them (measured: strikes 0.08-0.41, whooshes 0.71-0.80)."""
    x = read_audio(path, a, b - a, 1)[:, 0]
    hp = np.diff(x, prepend=0)
    hop = 240
    n = len(hp) // hop
    db = 10 * np.log10((hp[:n * hop].reshape(n, hop) ** 2).mean(1) + 1e-12)
    out = []
    for i in range(12, n - 8):
        if db[i] - db[i - 12:i - 2].mean() > 14 and db[i] > db.max() - 45 and db[i] >= db[i - 1] and (not out or i - out[-1][2] > 30):
            seg = x[i * hop + 240:i * hop + 240 + 2048]
            if len(seg) < 2048:
                continue
            spec = np.abs(np.fft.rfft(seg * np.hanning(2048))) + 1e-9
            fr = np.fft.rfftfreq(2048, 1 / SR)
            band = spec[(fr > 200) & (fr < 4000)]
            out.append((a + i * hop / SR, np.exp(np.log(band).mean()) / band.mean() < 0.55, i))
    return [(t, strike) for t, strike, _ in out]


def gain_env(n, regions, db, ramp=0.03):
    """Fixed gain curve: db applied inside each (a, b) region (seconds), with short ramps."""
    env = np.ones(n, np.float32)
    g = 10 ** (-db / 20)
    r = int(ramp * SR)
    for a, b in regions:
        i, j = max(0, int(a * SR)), min(n, int(b * SR))
        if j <= i:
            continue
        env[i:j] = np.minimum(env[i:j], g)
        k = min(r, i)
        env[i - k:i] = np.minimum(env[i - k:i], np.linspace(1, g, k))
        k = min(r, n - j)
        env[j:j + k] = np.minimum(env[j:j + k], np.linspace(g, 1, k))
    return env


def mix(tl, total, words):
    n = int(round(total * SR))
    voice = read_audio(O / 'voice.wav')[:n]
    voice_lufs = lufs_of(voice[:, 0])
    out = np.zeros((n, 2), np.float32)
    out[:len(voice)] += voice
    game = np.zeros((n, 2), np.float32)
    events = []
    for s in tl:
        snd = s.get('sound')
        if not snd:
            continue
        sp = s['srcs'][0]
        i0 = int(s['start'] * SR)
        part = np.zeros((n, 2), np.float32)
        if snd[0] == 'live':
            assert abs(sp['speed'] - 1) < 0.3, s['at']
            tail = 0.35
            a = read_audio(sp['file'], sp['t'], s['duration'] + tail)
            r = int(0.012 * SR)
            a[:r] *= np.linspace(0, 1, r)[:, None]
            k = int(tail * SR)
            a[-k:] *= np.linspace(1, 0, k)[:, None]
            # heat whooshes are long broadband swells: take them down well under the strikes
            whoosh = [(s['start'] + (t - sp['t']) - 0.01, s['start'] + (t - sp['t']) + 0.65)
                      for t, strike in onsets(sp['file'], sp['t'], sp['t'] + s['duration']) if not strike]
            m = min(len(a), n - i0)
            part[i0:i0 + m] += a[:m]
            part *= gain_env(n, whoosh, WHOOSH_CUT_DB)[:, None]
            events.append(dict(shot=s['at'], mode='live', source=[sp['t'], sp['t'] + s['duration']], whooshes_cut=len(whoosh)))
        else:
            gap = snd[1]
            found = onsets(sp['file'], sp['t'], s['t_out'])
            hits = [t for t, strike in found if strike or snd[0] == 'clicks']
            placed, last = [], -9
            for o in hits:
                t_out = s['start'] + (o - sp['t']) / sp['speed']
                if t_out - last < gap or t_out > s['end'] - 0.05:
                    continue
                hit = read_audio(sp['file'], max(0, o - 0.012), 0.42 if snd[0] == 'hits' else 0.36)
                hit[:int(0.004 * SR)] *= np.linspace(0, 1, int(0.004 * SR))[:, None]
                hit *= np.exp(-np.linspace(0, 4.5 if snd[0] == 'hits' else 6.0, len(hit)))[:, None]
                j = int((t_out - 0.012) * SR)
                m = min(len(hit), n - j)
                part[j:j + m] += hit[:m]
                placed.append(round(t_out, 3))
                last = t_out
            events.append(dict(shot=s['at'], mode=snd[0], placed_output=placed))
        a0, a1 = i0, min(n, int((s['end'] + 0.35) * SR))
        level = lufs_of(part[a0:a1, 0]) if np.abs(part[a0:a1]).max() > 0 else None
        if level is None or not np.isfinite(level):
            continue
        g = voice_lufs - GAME_UNDER_VOICE[snd[0]] - level
        events[-1].update(measured_lufs=round(level, 2), gain_db=round(g, 2))
        game += part * 10 ** (g / 20)
    speaking = [(w['s'] - 0.05, w['e'] + 0.12) for w in words]
    game *= gain_env(n, speaking, SPEECH_DUCK_DB, ramp=0.08)[:, None]
    out += game
    # Theme from its first note at one fixed level for the whole reel: no fade-in, no dips.
    music = read_audio(ASSETS / 'music/asciilands-theme-loop.ogg')
    music = np.tile(music, (int(np.ceil(n / len(music))), 1))[:n]
    env = np.full(n, 10 ** (MUSIC_DB / 20), np.float32)
    k = int(0.4 * SR)
    c = np.concatenate([[0], np.cumsum(env, dtype=np.float64)])
    sm = ((c[k:] - c[:-k]) / k).astype(np.float32)
    env = np.concatenate([np.full(k // 2, env[0]), sm, np.full(n - len(sm) - k // 2, env[-1])])
    env[:k] = 10 ** (MUSIC_DB / 20)                    # the first note at full level
    fo = int(1.0 * SR)
    env[-fo:] *= np.linspace(1, 0, fo)
    out += music * env[:, None]
    write_wav(O / 'audio/mix.wav', out)
    write_wav(O / 'audio/game.wav', game)
    (O / 'audio/game-events.json').write_text(json.dumps(events, indent=2))
    before = loudness(O / 'audio/mix.wav')
    gain = -14.0 - before['input_i']
    for _ in range(3):
        ff(['-i', O / 'audio/mix.wav', '-af', f'volume={gain:.3f}dB,aresample=192000,alimiter=limit=0.84:level=false:latency=true,aresample={SR}',
            '-c:a', 'pcm_s24le', O / 'audio/master.wav'])
        m = loudness(O / 'audio/master.wav')
        if abs(-14 - m['input_i']) < 0.12:
            break
        gain += -14 - m['input_i']
    report = dict(before=before, fixed_gain_db=round(gain, 3), master=m, voice_lufs=voice_lufs)
    (O / 'audio-report.json').write_text(json.dumps(report, indent=2))
    return report


def write_wav(path, a):
    ff(['-f', 'f32le', '-ar', SR, '-ac', a.shape[1], '-i', '-', '-c:a', 'pcm_f32le', path], input=a.astype(np.float32).tobytes())


def loudness(p):
    r = subprocess.run(['ffmpeg', '-hide_banner', '-i', str(p), '-af', 'loudnorm=I=-14:TP=-1:LRA=11:print_format=json',
                        '-f', 'null', os.devnull], capture_output=True, text=True)
    d = json.loads(re.findall(r'\{[^{}]*"input_i"[^{}]*\}', r.stderr, re.S)[-1])
    return {k: float(v) if re.fullmatch(r'-?[\d.]+|-inf', v) else v for k, v in d.items()}


def mux(video, out, total):
    ff(['-i', video, '-i', O / 'audio/master.wav', '-map', '0:v', '-map', '1:a', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
        '-t', total, '-movflags', '+faststart', out])


def review_copy(final, out, total):
    kbps = int(26 * 8 * 1024 / total) - 160            # stay under about 26 MB
    ff(['-i', final, '-c:v', 'libx264', '-preset', 'slow', '-b:v', f'{kbps}k', '-maxrate', f'{int(kbps * 1.5)}k',
        '-bufsize', f'{kbps * 2}k', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', out])


# ---------------------------------------------------------------- QA
def qa(tl, caps, total, path):
    q = O / 'qa'
    for old in q.glob('*.jpg'):
        old.unlink()
    moments = sorted(set([round(s['start'] + 0.05, 3) for s in tl] + [round(s['end'] - 0.06, 3) for s in tl] +
                         [round(c['start'] + 0.07, 3) for c in caps] + [round(total - 0.1, 3)]))
    moments = [m for m in moments if m < total]
    f = ImageFont.truetype(FONT, 16)
    recs = []
    for i, t in enumerate(moments):
        data = subprocess.check_output(['ffmpeg', '-v', 'error', '-ss', f'{t:.3f}', '-i', str(path), '-frames:v', '1',
                                        '-vf', 'scale=270:480', '-f', 'image2pipe', '-c:v', 'mjpeg', '-'])
        im = Image.open(io.BytesIO(data)).convert('RGB')
        d = ImageDraw.Draw(im)
        for y in (SAFE['top'] / 4, SAFE['bottom'] / 4):
            d.line((0, y, 270, y), fill=(255, 60, 60), width=1)
        for x in (SAFE['left'] / 4, SAFE['right'] / 4):
            d.line((x, 0, x, 480), fill=(255, 60, 60), width=1)
        im.save(q / f'{i:03d}.jpg')
        shot = next((j for j, s in enumerate(tl) if s['start'] <= t < s['end']), len(tl) - 1)
        recs.append(dict(time=t, file=f'{i:03d}.jpg', shot=shot, at=tl[shot]['at']))
    for k in range(0, len(recs), 24):
        chunk = recs[k:k + 24]
        sheet = Image.new('RGB', (270 * 8, 510 * math.ceil(len(chunk) / 8)), '#101b14')
        d = ImageDraw.Draw(sheet)
        for j, r in enumerate(chunk):
            x, y = (j % 8) * 270, (j // 8) * 510
            sheet.paste(Image.open(q / r['file']), (x, y))
            d.text((x + 6, y + 484), f"{r['file'][:3]} {r['time']:.2f}s s{r['shot']:02d}", font=f, fill='white')
        sheet.save(q / f'sheet-{k // 24:02d}.jpg', quality=88)
    (q / 'index.json').write_text(json.dumps(recs, indent=2))
    return recs


def checks(tl, caps, total, report):
    problems = []
    for a, b in zip(caps, caps[1:]):
        if a['end'] > b['start'] + 1e-6:
            problems.append(f"caption overlap {a['text']} / {b['text']}")
    for c in caps:
        if c['end'] - c['start'] < 0.25:
            problems.append(f"caption too brief: {c['text']}")
    if sum(s['frames'] for s in tl) != round(total * FPS):
        problems.append('shot frames do not add up to total')
    if report['master']['input_tp'] > -1.0:
        problems.append(f"true peak {report['master']['input_tp']}")
    if abs(report['master']['input_i'] + 14) > 0.3:
        problems.append(f"loudness {report['master']['input_i']}")
    (O / 'checks.json').write_text(json.dumps(dict(problems=problems), indent=2))
    return problems


if __name__ == '__main__':
    jobs = int(sys.argv[sys.argv.index('--jobs') + 1]) if '--jobs' in sys.argv else 3
    words, voice_len = prepare_audio()
    tl, total = build_timeline(words, voice_len)
    caps = build_caption_layers(group_captions(words, total))
    (O / 'captions.json').write_text(json.dumps([{k: v for k, v in c.items() if k != 'image'} for c in caps], indent=2))
    print(f'{len(tl)} shots, {len(caps)} captions, {total:.2f}s', flush=True)
    if '--prepare' in sys.argv:
        sys.exit()
    if '--qa-only' not in sys.argv:
        render_picture(tl, words, caps, total, jobs)
        report = mix(tl, total, words)
        mux(O / 'picture.mp4', FINAL, total)
        review_copy(FINAL, REVIEW, total)
        print('audio', report['master'], flush=True)
    else:
        report = json.loads((O / 'audio-report.json').read_text())
    recs = qa(tl, caps, total, FINAL)
    problems = checks(tl, caps, total, report)
    print('QA frames', len(recs), 'problems', problems, flush=True)
