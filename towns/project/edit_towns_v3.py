"""Asciilands "They built this" short, revision 3. Built on the smithing reel v3 renderer.

Run from anywhere:  python project/edit_towns_v3.py [--prepare] [--qa-only] [--preview] [--jobs N]

Pipeline: cached Whisper words -> silence-aware narration retime (plus spliced
replacement takes) -> word-anchored shots -> every frame rendered in Python:
gameplay frames or stills are cropped, animated and composited, then captions,
cards and the end card are drawn with Pillow alpha_composite -> raw RGB frames
piped into ffmpeg (-f rawvideo -pix_fmt rgb24 -i -) and encoded with libx264 ->
voice + calibrated real game sound + the theme, one fixed gain to -14 LUFS and a
-1 dBTP limiter -> QA stills.

Editorial choices are data near the top: WORD_FIXES, PAUSES, CLEAN, SHOTS, looks,
ACCENTS, MUSIC. Originals are only read. --preview writes first/middle/last stills of
every shot without encoding, for framing checks.
"""
from pathlib import Path
import subprocess, json, re, math, sys, hashlib, io, os
from concurrent.futures import ThreadPoolExecutor
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance, ImageOps

R = Path(__file__).resolve().parent
B = R.parent
V = B / 'media/video'
VO = B / 'media/audio/followup2.wav'
ASSETS = B / 'assets'
O = R / 'edit-v3'
for d in ['', 'shots', 'stills', 'qa', 'audio', 'preview']:
    (O / d).mkdir(parents=True, exist_ok=True)
FINAL = R / 'towns-v3.mp4'
REVIEW = R / 'towns-v3-review.mp4'
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


DERIVED = B / 'media/derived'          # smoothed orbits, made by smooth_orbits.py


def src(name):
    return (DERIVED if name.endswith('-smooth') else V) / (name + '.mkv')


def ease(u):                      # smooth in and out, for camera moves
    u = min(1.0, max(0.0, u))
    return u * u * (3 - 2 * u)


def ease_out(u):                  # cubic ease-out, as in the earlier shorts' cards
    u = min(1.0, max(0.0, u))
    return 1 - (1 - u) ** 3


# ---------------------------------------------------------------- narration
# Whisper heard "exploration won. They're" as "exploration 1 that are" (checked on the
# spectrogram: a 0.2 s gap after "won"). Matched by context, never by index. A shorter
# replacement merges the remaining words' time into its last word.
WORD_FIXES = [(('exploration', '1', 'that', 'are'), ['exploration', 'won.', "They're"]),
              (('the', 'game', 'and', 'improvements'), ['the', 'game,', 'and', 'improvements'])]
PAUSE_DEFAULT = {'.': 0.30, '?': 0.32, '!': 0.30, ',': 0.20, '': 0.16}
PAUSES = {
    'looks like this.': 0.60,          # a beat on York now
    'left and right.': 0.46,
    'sight to behold.': 0.60,
    'coming along.': 0.70,             # room for the blade designer before sailing (0.76 s as recorded)
    'guilds and religions.': 0.85,     # time to read the Cult of Akton post
}
LEAD_IN = 0.06
TAIL_PAD = 0.08
HEAD_PAD = 0.07
# Replacement takes, spliced in at the measured silences around the phrase.
# Word times are in the take's own seconds (measured from its energy envelope).
VOICE_REPLACEMENTS = []


def load_words():
    if not (R / 'words-raw-v3.json').exists():
        from faster_whisper import WhisperModel
        model = WhisperModel('small.en', device='cpu', compute_type='int8', download_root=str(R / 'models'))
        raw = np.frombuffer(subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(VO), '-ar', '16000', '-ac', '1',
                                                     '-f', 'f32le', '-']), dtype=np.float32)
        segs, _ = model.transcribe(raw, word_timestamps=True, beam_size=5,
                                   initial_prompt='Asciilands. York. Star iron. Guilds and religions. Smithing. Sailing.')
        words = [dict(w=w.word.strip(), s=w.start, e=w.end, p=w.probability) for seg in segs for w in seg.words]
        (R / 'words-raw-v3.json').write_text(json.dumps(words, indent=2))
    words = json.loads((R / 'words-raw-v3.json').read_text())
    text = [norm(x['w']) for x in words]
    for pattern, fix in WORD_FIXES:
        hits = [i for i in range(len(text) - len(pattern) + 1) if tuple(text[i:i + len(pattern)]) == pattern]
        assert len(hits) <= 1, pattern
        for i in hits:
            span = words[i:i + len(pattern)]
            new = [dict(span[k], w=fix[k]) for k in range(len(fix))]
            new[-1]['e'] = span[-1]['e']
            words[i:i + len(pattern)] = new
            text = [norm(x['w']) for x in words]
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
# sources by index: shot['srcs'][i].
DISCORD = Path('C:/Users/liamb/Videos/asciilands-towns-short/discord-stills')
STILL_IMAGES = {
    'nordheim-walls': DISCORD / 'NA_kingdom-of-nordheim_1562365573734862898_0.png',   # Bill, Just Bill
    'northmost': DISCORD / 'EU_castle-northmost_1561624618379387001_0.png',           # Dirtmole
    'pyramids': DISCORD / 'ANY_screenshots_1562450670852907008_0.png',                # Truly
    'hellheim': DISCORD / 'EU_hellheim_1561846647888154695_0.png',                    # Monsterrubberduck
    'york-sea': DISCORD / 'EU_york_1562209372308897802_0.png',                        # Craeb (York: Louie)
    'wefarm': DISCORD / 'NA_want-cheap-food_1562138164284497921_1.png',               # goRogue
    'cult': ASSETS / 'stills/discord-cult-of-akton.png',      # Cleptomane's post, owner screenshot
    'poll': ASSETS / 'stills/discord-poll-final.png',         # the announcement poll, owner screenshot
    'wiki': ASSETS / 'stills/wiki-star-iron-order.png',       # awiki.yi5.dev Market row, owner screenshot
}
# Poll rows in the 590x567 screenshot: label and vote count on one line each.
POLL_ROWS = {'smithing': (84, 236, 481, 54), 'exploration': (84, 428, 481, 54)}


def frame_still(name, t, flip=False):
    p = O / 'stills' / f'{name}@{t:.2f}.png'
    if not p.exists():
        ff(['-ss', f'{t:.3f}', '-i', src(name), '-frames:v', 1, p])
    img = Image.open(p).convert('RGB')
    return ImageOps.mirror(img) if flip else img


def highlight(img, rows):
    """Dim everything but the given rows; bright text inside them turns gold."""
    out = ImageEnhance.Brightness(img).enhance(0.42)
    for x, y, w, h in rows:
        band = np.asarray(img.crop((x, y, x + w, y + h))).astype(np.float32)
        luma = band.mean(axis=2)
        text = luma > 150
        gold = np.array(T_GOLD, np.float32)
        band[text] = gold * np.clip(luma[text] / 230, 0.75, 1.15)[:, None]
        out.paste(Image.fromarray(np.clip(band, 0, 255).astype(np.uint8)), (x, y))
    return out


def load_source(spec):
    """spec: dict(still=name, rows=[...]) or dict(frame=(video, t), flip=) or a video dict."""
    if 'still' in spec:
        img = Image.open(STILL_IMAGES[spec['still']]).convert('RGB')
        if spec.get('rows'):
            img = highlight(img, [POLL_ROWS[r] for r in spec['rows']])
        return img
    if 'frame' in spec:
        img = frame_still(*spec['frame'], flip=spec.get('flip', False))
        return padded_map(img, spec['mapbox']) if spec.get('mapbox') else img
    return None


# Clean source area per recording: everything outside it (the Chrome debugging bar, tab and
# bookmark bars on the local-build clips, the window's bottom edge) can never enter a crop.
CLEAN_DEFAULT = (0, 100, 2560, 1406)    # below the Chrome debugging bar and the Admin tools badge
CLEAN = {'yorkbridge-smooth': (450, 240, 2150, 1406), 'nordheim-smooth': (500, 240, 2100, 1406),
         '2026-10-21 22-37-13': (0, 240, 2560, 1406), 'world zoom': (0, 240, 2560, 1406), 'nordheim': (0, 240, 2560, 1406), 'awesomesmithing': (0, 240, 2560, 1406), 'raft': (0, 240, 2560, 1406),
         'yorkarrival': (0, 0, 2560, 1440), 'orbitdesertmeteor': (0, 0, 2560, 1440)}


def clean_box(spec):
    if 'mapbox' in spec:
        return None                                   # the padded canvas is clean everywhere
    if 'v' in spec:
        x0, y0, x1, y1 = CLEAN.get(spec['v'], CLEAN_DEFAULT)
        return (2560 - x1, y0, 2560 - x0, y1) if spec.get('flip') else (x0, y0, x1, y1)
    if 'frame' in spec:
        return CLEAN.get(spec['frame'][0], CLEAN_DEFAULT)
    return None


# ---------------------------------------------------------------- looks
def world(cx, cy, h0=1280, h1=None, cx1=None, cy1=None, t0=0, t1='e', ease='linear'):
    """A 9:16 window straight out of the gameplay, with an optional slow push or pan."""
    h1 = h1 or h0
    cx1 = cx if cx1 is None else cx1
    cy1 = cy if cy1 is None else cy1

    def rect(x, y, h):
        w = h * 9 / 16
        return (x - w / 2, y - h / 2, w, h)
    return dict(bg=None, layers=[dict(src=[(t0, rect(cx, cy, h0)), (t1, rect(cx1, cy1, h1))],
                                      dst=[(0, (0, 0, W, H))], ease=ease)])


def panel(items, push=1.04):
    """Real UI pieces laid out large on a blurred backdrop. items: (source index, src rect,
    dst rect, start). A piece starting at 0 is there on the cut (no empty frames); later ones
    rise 40 px and fade in. Then everything pushes in gently."""
    layers = []
    for i, s, d, t in items:
        x, y, w, h = d
        big = (x - w * (push - 1) / 2, y - h * (push - 1) / 2, w * push, h * push)
        low = (x, y + 40, w, h)
        if not t:
            layers.append(dict(i=i, src=[(0, s)], dst=[(0, d), ('e', big)], shadow=True))
            continue
        layers.append(dict(i=i, src=[(0, s)], dst=[(0, low), (t, low), (f'{t}+0.4', d), ('e', big)],
                           alpha=[(0, 0), (t, 0), (f'{t}+0.25', 1)], shadow=True))
    return dict(bg='blur', layers=layers)


def cam(cx, cy, w):
    """9:16 camera rectangle centred on (cx, cy), w source pixels wide."""
    h = w * 16 / 9
    return (cx - w / 2, cy - h / 2, w, h)


# Plot UI in plotforsale.mkv after 14.25 s (the Town panel jumps from its treasury view to
# the plots between 14.08 and 14.20; that jump is the flash to avoid).
PLOT_PRICE = (768, 312, 720, 182)      # South-west of the Town Hall / For sale: 50 gold / Buy for 50 gold
PLOT_GRID = (152, 346, 250, 312)       # the plot grid with prices, the town hall and You
PLOT_KEY = (152, 672, 600, 40)         # For sale  Yours  Owned  Town Hall  You


def plots():
    return panel([(0, PLOT_PRICE, (80, 300, 920, 233), 0),
                  (0, PLOT_GRID, (310, 575, 460, 574), 0.15),
                  (0, PLOT_KEY, (110, 1180, 860, 57), 0.3)], push=1.03)


# Regional market in market.mkv at 18.6 s: one column of real player listings.
MARKET_TITLE = (430, 310, 430, 90)
MARKET_LEFT = (442, 623, 579, 507)     # Amber, Berries, Damoclyse with prices and sellers


def market():
    return panel([(0, MARKET_TITLE, (130, 300, 600, 126), 0),
                  (0, MARKET_LEFT, (110, 450, 860, 753), 0.12)], push=1.03)


# Swords from the smithing work in progress (awesomesmithing.mkv inspects).
SMITH_SWORD = (931, 455, 360, 810)


def swords():
    tiles = [(1, (45, 330, 310, 698)), (2, (385, 330, 310, 698)), (3, (725, 330, 310, 698))]
    return panel([(i, SMITH_SWORD, d, 0.12 * k) for k, (i, d) in enumerate(tiles)], push=1.02)


BLADE_DESIGN = (584, 600, 830, 600)    # Design your blade: shape buttons, the blank, length and curve


def blade_designer():
    return dict(bg='blur', layers=[dict(src=[(0, BLADE_DESIGN), ('e', (604, 614, 790, 571))],
                                        dst=[(0, (50, 380, 980, 708))], shadow=True)])


# Wiki market row (922x78): the Star Iron item and the best buy order, laid out large.
WIKI_ITEM = (8, 40, 112, 32)           # Star Iron
WIKI_ORDER = (700, 6, 132, 60)         # BEST BUY ORDER / 1000g


def star_iron_order():
    return dict(bg='blur', layers=[
        dict(i=1, src=[(0, WIKI_ITEM)], dst=[(0, (190, 420, 700, 200))], shadow=True),
        dict(i=1, src=[(0, WIKI_ORDER)], dst=[(0, (160, 700, 760, 345)), ('@thousand', (160, 700, 760, 345)),
                                               ('@thousand+0.45', (100, 672, 880, 400))],
             alpha=[(0, 0), (0.12, 0), (0.4, 1)], shadow=True)])


POLL_BOX = (64, 40, 522, 440)          # question and the six answers with their votes
# The two winning rows, as they read in the screenshot (label, then votes and percentage).
POLL_QUESTION = (80, 50, 290, 24)      # Where should Asciilands go next?
POLL_SMITH = ((98, 252, 66, 24), (446, 252, 104, 24))
POLL_EXPLORE = ((98, 446, 160, 24), (446, 446, 104, 24))


def poll():
    """The whole poll, then it gives way to its question and the two winning rows, each lifted
    out at large size as it is named."""
    d0, d1 = (40, 330, 1000, 843), (22, 315, 1036, 873)
    gone = [(0, 1), ('@smithing', 1), ('@smithing+0.3', 0)]
    layers = [dict(i=0, src=[(0, POLL_BOX)], dst=[(0, d0), ('e', d1)], ease='linear', alpha=gone, shadow=True)]

    def lift(i, rect, dst, at):
        x, y, w, h = dst
        return dict(i=i, src=[(0, rect)], dst=[(0, (x, y + 30, w, h)), (at, (x, y + 30, w, h)), (f'{at}+0.35', dst)],
                    alpha=[(0, 0), (at, 0), (f'{at}+0.25', 1)], shadow=True)
    layers += [lift(0, POLL_QUESTION, (95, 400, 890, 75), '@smithing'),
               lift(1, POLL_SMITH[0], (100, 560, 264, 96), '@smithing'),
               lift(1, POLL_SMITH[1], (564, 560, 416, 96), '@smithing+0.08'),
               lift(2, POLL_EXPLORE[0], (100, 760, 640, 96), '@exploration'),
               lift(2, POLL_EXPLORE[1], (564, 870, 416, 96), '@exploration+0.08')]
    return dict(bg='blur', layers=layers)


CULT_FULL = (0, 0, 582, 767)


def cult():
    return dict(bg='blur', layers=[dict(src=[(0, CULT_FULL)], dst=[(0, (40, 330, 1000, 1318)), ('e', (10, 310, 1060, 1397))],
                                        ease='linear', shadow=True)])


# ---------------------------------------------------------------- map zoom (smoothed from the real zoom levels)
# The game's map zooms in jumps: it redraws a level every few frames and holds. These are the
# held levels of "world zoom.mkv", from closest to widest, each registered against the next
# wider one by normalised cross-correlation (scale, and where its crop lands in the wider frame).
# Local to World is a different map style; that step is matched on the region hexagon
# (450 px wide on the local map, 135 px on the first world level).
ZOOM_LEVELS = [
    # (frame time, map area in the frame, link: a point in this level, scale, the same point in the next level)
    (2.00, (435, 430, 1889, 1224), ((445, 440), 4.00, (1019, 705))),          # Local, closest (NCC 0.98)
    (2.85, (768, 430, 1556, 1224), ((1161, 827.5), 4.33, (1182.5, 821.75))),  # Local, whole region; matched on its hexagon
    (4.05, (435, 387, 1812, 1311), ((445, 397), 1.58, (683, 550))),           # World, close (NCC 0.87)
    (4.55, (435, 387, 1812, 1311), ((445, 397), 2.08, (624, 466))),           # World, middle (NCC 0.93)
    (5.50, (435, 387, 1812, 1311), None),                                      # World, the whole island
]
ZOOM_START = (1254, 742)       # the player arrow in York on the closest local map
ZOOM_END = (1152, 790)         # the island's centre on the widest level
ZOOM_END_W = 1120              # the whole island across the frame
MAP_PAD = 1400                 # map sources sit on their own sea colour, this far out on every side


def zoom_levels():
    """For each level, k (widest-level pixels per level pixel) and o (where level (0,0) lands)."""
    out = [None] * len(ZOOM_LEVELS)
    out[-1] = (1.0, (0.0, 0.0))
    for i in range(len(ZOOM_LEVELS) - 2, -1, -1):
        (qx, qy), sc, (px, py) = ZOOM_LEVELS[i][2]
        k1, (o1x, o1y) = out[i + 1]
        k = k1 / sc
        out[i] = (k, (o1x + (px - qx / sc) * k1, o1y + (py - qy / sc) * k1))
    return out


def map_zoom(hold0=0.06, hold1=0.16, steps=72):
    """One continuous, eased zoom from York on the local map out to the whole island, built from the
    held zoom levels. Each level fades into the next wider one before the view outgrows it."""
    lv = zoom_levels()
    k0, (ox0, oy0) = lv[0]
    sx0, sy0 = ox0 + ZOOM_START[0] * k0, oy0 + ZOOM_START[1] * k0
    b0 = ZOOM_LEVELS[0][1]
    h0 = (b0[3] - b0[1]) * k0 * 0.75
    h1 = ZOOM_END_W * 16 / 9
    reach = [min(b[3] - b[1], (b[2] - b[0]) * 16 / 9) * k for (_, b, _), (k, _) in zip(ZOOM_LEVELS, lv)]
    layers = [dict(i=j, src=[], alpha=[], dst=[(0, (0, 0, W, H))], ease='linear') for j in range(len(lv))]
    for n in range(steps + 1):
        u = n / steps
        tu = hold0 + (1 - hold0 - hold1) * u
        z = ease(u)
        h = h0 * (h1 / h0) ** z
        c = (h - h0) / (h1 - h0)                  # the centre follows the zoom, so York stays put early on
        cx, cy = sx0 + (ZOOM_END[0] - sx0) * c, sy0 + (ZOOM_END[1] - sy0) * c
        w = h * 9 / 16
        for j, (k, (ox, oy)) in enumerate(lv):
            rect = ((cx - w / 2 - ox) / k + MAP_PAD, (cy - h / 2 - oy) / k + MAP_PAD, w / k, h / k)
            a = 1.0 if j == len(lv) - 1 else float(np.clip((np.log(reach[j]) - np.log(h)) / 0.30, 0, 1))
            layers[j]['src'].append((f'{tu:.4f}*e', rect))
            layers[j]['alpha'].append((f'{tu:.4f}*e', a))
    for L in layers:
        L['src'] = [(0, L['src'][0][1])] + L['src'] + [('e', L['src'][-1][1])]
        L['alpha'] = [(0, L['alpha'][0][1])] + L['alpha'] + [('e', L['alpha'][-1][1])]
    return dict(bg=None, layers=layers[::-1])     # wider levels underneath


MAP_SRCS = [dict(frame=('world zoom', t), mapbox=box) for t, box, _ in ZOOM_LEVELS]


def padded_map(img, box):
    """The map area alone, on a canvas of its own edge colour, MAP_PAD out on every side."""
    piece = img.crop(box)
    a = np.asarray(piece)
    mid = a.shape[0] // 2
    sample = np.concatenate([a[mid - 20:mid + 20, 6:26].reshape(-1, 3), a[6:26, 6:26].reshape(-1, 3),
                             a[-26:-6, 6:26].reshape(-1, 3)])                # inside the map, along its left edge
    colour = tuple(int(v) for v in np.median(sample, axis=0))
    # feather the map's own edge into the pad so no seam shows when a level fades
    mask = Image.new('L', piece.size, 255)
    ImageDraw.Draw(mask).rectangle((0, 0, piece.width - 1, piece.height - 1), outline=0, width=2)
    mask = mask.filter(ImageFilter.GaussianBlur(10))
    canvas = Image.new('RGB', (img.width + 2 * MAP_PAD, img.height + 2 * MAP_PAD), colour)
    canvas.paste(piece, (box[0] + MAP_PAD, box[1] + MAP_PAD), mask)
    return canvas


def still_frame(cx, cy, h):
    """A fixed 9:16 window: the footage plays as recorded, no push, no pan."""
    w = h * 9 / 16
    return dict(bg=None, layers=[dict(src=[(0, (cx - w / 2, cy - h / 2, w, h))], dst=[(0, (0, 0, W, H))])])


def wide(cx, cy, w, aspect=1.15, mid=960):
    """Orbit shots keep more of the horizon: a window wider than 9:16 across the full frame width,
    on a blurred, darkened copy of the same footage (which keeps moving with it)."""
    h = w / aspect
    dh = W / aspect
    return dict(bg='blur', layers=[dict(src=[(0, (cx - w / 2, cy - h / 2, w, h))],
                                        dst=[(0, (0, mid - dh / 2, W, dh))])])


COLLAGE = [  # (still, crop centre x, y, crop width) in the screenshot's pixels
    ('nordheim-walls', 1240, 720, 1700), ('northmost', 900, 520, 1100), ('pyramids', 1420, 560, 1250),
    ('hellheim', 930, 480, 1100), ('york-sea', 960, 560, 1250), ('wefarm', 1230, 520, 1350)]
TILE_W, TILE_H = 450, 294


def collage(start=0.10, stagger=0.16):
    """The York crowd keeps playing, sharp at first, then dims under it as the players' own
    screenshots pop in one after another."""
    layers = [dict(i=0, src=[(0, (1280 - 1300 * 9 / 32, 760 - 650, 1300 * 9 / 16, 1300))], dst=[(0, (0, 0, W, H))],
                   alpha=[(0, 1), (start, 1), (start + 0.5, 0)])]
    for k, (_, cx, cy, cw) in enumerate(COLLAGE):
        ch = cw * TILE_H / TILE_W
        x, y = 80 + (k % 2) * (TILE_W + 20), 318 + (k // 2) * (TILE_H + 20)
        t = start + k * stagger
        small = (x + TILE_W * 0.06, y + TILE_H * 0.06, TILE_W * 0.88, TILE_H * 0.88)
        layers.append(dict(i=k + 1, src=[(0, (cx - cw / 2, cy - ch / 2, cw, ch))],
                           dst=[(0, small), (t, small), (t + 0.4, (x, y, TILE_W, TILE_H))],
                           alpha=[(0, 0), (t, 0), (t + 0.25, 1)], shadow=True))
    return dict(bg='blur', layers=layers)


COLLAGE_SRCS = [dict(v='york4', t='cont')] + [dict(still=name) for name, *_ in COLLAGE]


def swords_static():
    tiles = [(1, (45, 330, 310, 698)), (2, (385, 330, 310, 698)), (3, (725, 330, 310, 698))]
    return dict(bg='blur', layers=[dict(i=i, src=[(0, SMITH_SWORD)], dst=[(0, d)], shadow=True) for i, d in tiles])


# ---------------------------------------------------------------- shots
# at: spoken words that open the shot; a [bracketed] word marks the exact cut word.
# srcs: sources (video dict(v=, t=, speed=, flip=) or still dict). Single video shots use v/t/speed/flip.
# label: the town on screen, set large under the notch.
# sound: None | ('steps',) real game sound, transients only (footsteps, barks; no wind bed) |
#        ('clicks', min_gap) gated UI sounds only.
# Framing: the game camera already follows the player, so most shots are a fixed window centred
# on the player, with no zoom or pan of our own.
CUT_LEAD = 0.07
YB = '2026-10-21 22-37-13'      # York from across the bridge (owner recording 2026-10-21, orbit from 0:18)
ORBIT = 1.5                     # orbits play faster: smoother motion, more energy
SHOTS = [
    dict(at='Last week', v='yorkarrival', t=6.00, look=still_frame(1280, 760, 1300), label='YORK', sound=('steps',),
         note='York last week. Walk from behind starts 6.00. The turn ends 5.93.'),
    dict(at='And [now]', v='yorkbridge-smooth', t=0.20, speed=3.0, look=wide(1300, 822, 1166, aspect=1.0), label='YORK',
         note='York now from across the river: the Great York Bridge, the tower and the new houses (owner select 0:18).'),
    dict(at="It's not the", srcs=MAP_SRCS, look=map_zoom(),
         note='One smooth zoom from the York local map to the whole island, made from the held zoom levels.'),
    dict(at='new ones have', v='fortredleaf', t=37.00, speed=2.0, flip=True, look=wide(1250, 700, 1560, aspect=1.2),
         label='FORT REDLEAF', note='Fort Redleaf orbit (owner select 0:31), mirrored, gatehouse centred.'),
    dict(at="It's been a", v='aphelionorbit', t=34.50, speed=2.0, look=wide(1350, 620, 1380, aspect=1.15), label='APHELION',
         note='Aphelion from afar (owner select from 0:23): the town across the snow.'),
    dict(at='selling plots and', v='plotforsale', t=14.30, look=plots(),
         note='Real plot UI: For sale 50 gold, Buy for 50 gold, the plot grid. Starts after the 14.08 jump.'),
    dict(at='and running their', v='driftwoodmarket', t=47.20, look=still_frame(1280, 760, 1300), label='DRIFTWOOD',
         sound=('steps',), note='Driftwood market street, centred on the player walking (owner select from 0:42).'),
    dict(at='and even kingdoms.', v='nordheim-smooth', t=0.20, speed=2.0, look=wide(1300, 822, 1166, aspect=1.0), label='NORDHEIM',
         note='Nordheim from the mountain, the walled town in view (owner select from 0:08).'),
    dict(at='[guilds] and religions.', srcs=[dict(still='cult')], look=cult(),
         note="Cleptomane's Cult of Akton post (owner screenshot)."),
    dict(at='The player -run', v='orbitdesertmeteor', t=12.00, speed=ORBIT, look=still_frame(1280, 760, 1300),
         note='Star iron meteor crater (library select 12 to 25).'),
    dict(at="There's so little", srcs=[dict(frame=('orbitdesertmeteor', 13.5)), dict(still='wiki')],
         look=star_iron_order(), note='Wiki market row: Star Iron, best buy order 1000g (owner screenshot).'),
    dict(at='This totally caught', v='york4', t=3.00, look=still_frame(1280, 760, 1300), label='YORK', sound=('steps', 8.0),
         note='Walking through York full of players (owner select 0:03).'),
    dict(at='to get the servers', srcs=COLLAGE_SRCS, look=collage(), sound=('steps', 8.0),
         note='Collage of player screenshots from Discord over the York crowd, which carries on from the shot before: '
              'Nordheim (Bill), Castle Northmost (Dirtmole), pyramids (Truly), Hellheim (Monsterrubberduck), '
              'York (Craeb), WE FARM (goRogue).'),
    dict(at='I asked what', srcs=[dict(still='poll'), dict(still='poll', rows=['smithing']),
                                  dict(still='poll', rows=['smithing', 'exploration'])],
         look=poll(), note='Final poll: Smithing then Exploration and Sailing lift out as they are named.'),
    dict(at="[They're] already coming", srcs=[dict(frame=('awesomesmithing', 4.5)), dict(frame=('awesomesmithing', 4.5)),
                                             dict(frame=('awesomesmithing', 17.0)), dict(frame=('awesomesmithing', 24.5))],
         look=swords_static(), note='Three new swords: Masterwork Blacksteel, Needle, Quick Dirk.'),
    dict(at="I've just about", v='raft', t=3.00, look=still_frame(1400, 800, 1180),
         note='Bringing the raft up to the dock (owner select 0:03). The recording has no sound.'),
    dict(at='Comment below what', v=YB, t=22.00, look=still_frame(1300, 819, 1174), endcard=True, lead=0.15,
         note='York bridge orbit under the end card (owner recording from 0:18).'),
]
END_TAIL = 2.5
# Real game sound: only its transients (footsteps, barks, clicks) pass the gate; the steady wind
# bed and other players' forge swells stay out. It sits well under the narration and ducks under words.
GAME_UNDER_VOICE = {'steps': 25.0, 'clicks': 25.0}
SPEECH_DUCK_DB = 6.0
GAME_MAX_GAIN_DB = 8.0
STEP_GATE_DB = 9.0        # a transient must stand this far above the local floor
# Music: the one theme, from its first note, at one level under the narration (measured in LU).
# Music: "meebles", one of the game's own day tracks (owner asked for a piece other than the main
# theme). The reel starts on its first note (0.50 s into the file) and it plays at one level.
MUSIC_FILE = ASSETS / 'music/meebles.ogg'
MUSIC_START = 0.50
MUSIC_UNDER_VOICE = 23.0
# Its beats (about 89 bpm, tracked once with librosa and cached) let the line 7 cut land on a
# beat by lengthening the pause before it (never more than one beat).
MUSIC_BEATS = [t - MUSIC_START for t in json.loads((R / 'music-beats-meebles.json').read_text())['beats']]


def next_beat(t):
    return min(b for b in MUSIC_BEATS if b >= t - 1e-6)


SYNC_CUT = dict(at='This totally caught', pause_after='for a single piece.')
LABEL_SIZE, LABEL_X, LABEL_Y = 54, 84, 285
SERVER = {'YORK': 'EU', 'FORT REDLEAF': 'EU', 'APHELION': 'EU', 'DRIFTWOOD': 'NA', 'NORDHEIM': 'NA'}
PIN = ['..###..', '.#####.', '##...##', '##...##', '.#####.', '.#####.', '..###..', '...#...']   # pixel map pin
LABEL_DELAY, LABEL_FIRST_DELAY, LABEL_FADE = 0.30, 0.90, 0.25
ACCENTS = ['ONE', 'NOW', 'ONLY', 'WORLD', 'BEHOLD', 'PEOPLE', 'VILLAGES', 'PLOTS', 'TAXES', 'MARKETS', 'KINGDOMS',
           'GUILDS', 'RELIGIONS', 'ECONOMY', 'STAR', 'THOUSAND', 'GOLD', 'GUARD', 'SERVERS', 'PLAYERS', 'NEXT',
           'SMITHING', 'EXPLORATION', 'WON', 'COMING', 'SAILING', 'FUN', 'COMMENT', 'GAME', 'SEE']


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
            s['srcs'] = [dict(v=s['v'], t=s['t'], speed=s['speed'], flip=s.get('flip', False))]
        for sp in s['srcs']:
            if sp.get('t') == 'cont':
                assert tl[i - 1].get('v') == sp['v'], s['at']
                sp['t'] = tl[i - 1]['t_out']
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
    e = e.replace('e', str(dur)) if re.fullmatch(r'[e0-9.+\-* ]+', e) else e
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
CAPTION_STARTS = ['selling plots', 'and running', 'and even kingdoms', 'scrambling for', "someone's offering",
                  'to get the servers', 'to handle', 'improvements to', 'which should']


def font(size=CAP_SIZE):
    return ImageFont.truetype(FONT, size)


def measure(text, f):
    b = ImageDraw.Draw(Image.new('L', (1, 1))).textbbox((0, 0), text, font=f, stroke_width=STROKE)
    return b[2] - b[0]


def wrapped_lines(text, f):
    lines, row = 1, []
    for w in text.split():
        if row and measure(' '.join(row + [w]), f) > CAP_MAX_W:
            lines, row = lines + 1, [w]
        else:
            row.append(w)
    return lines


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
                if wrapped_lines(' '.join(x['w'] for x in chunk).upper(), f) > 2:
                    cost += 20
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
def background(img, box=None):
    if box:
        img = img.crop(box)
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
        self.flip = sp.get('flip', False)

    def next(self):
        buf = self.p.stdout.read(self.size)
        if len(buf) < self.size:                   # only ever the final frame or two
            return self.last
        self.last = Image.frombuffer('RGB', (2560, 1440), buf)
        if self.flip:
            self.last = ImageOps.mirror(self.last)
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


def compose(images, look, t, boxes=None):
    boxes = boxes or [None] * len(images)
    canvas = background(images[0], boxes[0]) if look['bg'] else Image.new('RGB', (W, H))
    for layer in look['layers']:
        a = keyed(layer['alpha'], t)
        if a <= 0.01:
            continue
        img = images[layer['i']]
        sx, sy, sw, sh = keyed(layer['src'], t, layer['linear'])
        dx, dy, dw, dh = keyed(layer['dst'], t, layer['linear'])
        cx, sw = sx + sw / 2, sh * dw / dh              # crop aspect always follows the destination
        bx0, by0, bx1, by1 = boxes[layer['i']] or (0, 0, img.width, img.height)
        if sh > by1 - by0:                              # never reach outside the clean area
            sh, sw = by1 - by0, (by1 - by0) * dw / dh
        if sw > bx1 - bx0:
            sw, sh = bx1 - bx0, (bx1 - bx0) * dh / dw
        sx = min(max(cx - sw / 2, bx0), bx1 - sw)
        sy = min(max(sy, by0), by1 - sh)
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


_LABELS = {}


def label_image(text):
    """The town on screen, top-left in the safe area (clear of the notch): a pixel map pin, the
    name in gold caption style, and its server small underneath."""
    if text not in _LABELS:
        f, fs = font(LABEL_SIZE), font(30)
        im = Image.new('RGBA', (W, LABEL_SIZE + 110))
        shadow = Image.new('RGBA', im.size)
        d, ds = ImageDraw.Draw(im), ImageDraw.Draw(shadow)
        px, k = 22, 6                                   # pin cells of 6 px
        for r, row in enumerate(PIN):
            for c, ch in enumerate(row):
                if ch == '#':
                    box = (px + c * k, 16 + r * k, px + (c + 1) * k - 1, 16 + (r + 1) * k - 1)
                    d.rectangle((box[0] - 3, box[1] - 3, box[2] + 3, box[3] + 3), fill=INK)
        for r, row in enumerate(PIN):
            for c, ch in enumerate(row):
                if ch == '#':
                    d.rectangle((px + c * k, 16 + r * k, px + (c + 1) * k - 1, 16 + (r + 1) * k - 1), fill=GOLD)
                    ds.rectangle((px + c * k + 3, 22 + r * k, px + (c + 1) * k + 2, 22 + (r + 1) * k), fill=(0, 0, 0, 170))
        x = px + 7 * k + 18
        ds.text((x + 3, 16), text, font=f, fill=(0, 0, 0, 170), stroke_width=9, stroke_fill=(0, 0, 0, 170))
        d.text((x, 10), text, font=f, fill=GOLD, stroke_width=7, stroke_fill=INK)
        if SERVER.get(text):
            tag = SERVER[text] + ' SERVER'
            ds.text((x + 3, LABEL_SIZE + 26), tag, font=fs, fill=(0, 0, 0, 170), stroke_width=7, stroke_fill=(0, 0, 0, 170))
            d.text((x, LABEL_SIZE + 22), tag, font=fs, fill=WHITE, stroke_width=5, stroke_fill=INK)
        shadow = shadow.filter(ImageFilter.GaussianBlur(6))
        shadow.alpha_composite(im)
        box = shadow.getbbox()
        img = shadow.crop(box)
        pos = (LABEL_X, LABEL_Y)
        assert pos[0] >= SAFE['left'] and pos[0] + img.width <= SAFE['right'] and pos[1] >= SAFE['top'], text
        _LABELS[text] = (img, pos)
    return _LABELS[text]


def overlay_label(frame_img, text, a=1.0):
    if not text or a <= 0:
        return frame_img
    img, pos = label_image(text)
    out = frame_img.convert('RGBA')
    out.alpha_composite(faded(img, a), pos)
    return out.convert('RGB')


def overlay_captions(frame_img, now, caps):
    out = frame_img.convert('RGBA')
    for c in caps:
        if c['start'] <= now < c['end']:
            out.alpha_composite(c['image'], (c['x'], c['y']))
    return out.convert('RGB')


def shot_signature(i, s, look, caps):
    shown = [(c['text'], c['start'], c['end']) for c in caps if c['end'] > s['start'] and c['start'] < s['end']]
    key = dict(look=look, srcs=s['srcs'], frames=s['frames'], start=s['start'], caps=shown, endcard=s.get('endcard'),
               total=s.get('total'), clean=[clean_box(sp) for sp in s['srcs']], label=s.get('label'), label_delay=s.get('label_delay'), v=3)
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
    boxes = [clean_box(sp) for sp in s['srcs']]
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
        raw = compose(images, look, t, boxes)
        out = raw
        if s.get('endcard'):
            # Gameplay keeps running underneath the card, which dissolves in from the previous shot.
            out = end_card(raw, t)
            if t < .6 and prev_last is not None:
                out = Image.blend(prev_last, out, ease_out(t / .6))
            if now > s['total'] - .5:
                out = Image.blend(out, Image.new('RGB', (W, H)), min(1, (now - (s['total'] - .5)) / .5))
        la = 1.0 if s.get('label_delay') is None else ease_out((t - s['label_delay']) / LABEL_FADE)
        enc.stdin.write(overlay_captions(overlay_label(out, s.get('label'), la), now, caps).tobytes())
    for rd in readers:
        if rd:
            rd.close()
    enc.stdin.close()
    assert enc.wait() == 0
    raw.save(last_png)
    stamp.write_text(sig)
    print(f'shot {i:02d} {s["at"]!r} {s["duration"]:.2f}s', flush=True)
    return dest


def label_delays(tl):
    """Labels are on screen from the cut (the owner found a delayed fade-in looked late)."""
    for i, s in enumerate(tl):
        same = i > 0 and tl[i - 1].get('label') == s.get('label')
        s['label_delay'] = None                         # on screen from the first frame of the shot


def render_picture(tl, words, caps, total, jobs):
    label_delays(tl)
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


def transient_gate(a):
    """Pass only what jumps above the local floor (footsteps, barks, clicks); the steady wind bed
    and slow swells (other players' forges) stay out. 5 ms frames, 0.6 s rolling floor."""
    x = a.mean(1)
    hop = int(0.005 * SR)
    m = len(x) // hop
    e = 10 * np.log10((x[:m * hop].reshape(m, hop) ** 2).mean(1) + 1e-12)
    win = int(0.6 / 0.005)
    pad = np.pad(e, (win // 2, win - win // 2 - 1), mode='edge')
    floor = np.array([np.percentile(pad[i:i + win], 30) for i in range(m)])
    open_ = (e - floor) > STEP_GATE_DB
    g = np.zeros(m, np.float32)
    level = 0.0
    for i in range(m):                                # 5 ms attack, 80 ms release
        level = 1.0 if open_[i] else level * np.exp(-0.005 / 0.08)
        g[i] = level
    g = np.repeat(g, hop)
    return np.pad(g, (0, len(x) - len(g)))


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
    spoken = np.concatenate([voice[int(w['s'] * SR):int(w['e'] * SR), 0] for w in words])
    voice_rms = 20 * np.log10(np.sqrt((spoken ** 2).mean()) + 1e-12)
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
        if snd[0] == 'steps':
            assert abs(sp['speed'] - 1) < 0.3, s['at']
            tail = 0.25
            a = read_audio(sp['file'], sp['t'], s['duration'] + tail)
            a *= transient_gate(a)[:, None]
            r = int(0.012 * SR)
            a[:r] *= np.linspace(0, 1, r)[:, None]
            k = int(tail * SR)
            a[-k:] *= np.linspace(1, 0, k)[:, None]
            if sp.get('flip'):
                a = a[:, ::-1].copy()                   # a mirrored picture gets mirrored sound
            m = min(len(a), n - i0)
            part[i0:i0 + m] += a[:m]
            events.append(dict(shot=s['at'], mode='steps', source=[sp['t'], sp['t'] + s['duration']]))
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
        if not np.abs(part[a0:a1]).max() > 0:
            continue
        level = lufs_of(part[a0:a1, 0])
        if np.isfinite(level) and level > -70:
            g = voice_lufs - GAME_UNDER_VOICE[snd[0]] - (snd[1] if snd[0] == 'steps' and len(snd) > 1 else 0) - level
            events[-1].update(measured_lufs=round(level, 2))
        else:
            # Too short or quiet for gated loudness: calibrate on RMS against the voice's RMS instead.
            rms = 20 * np.log10(np.sqrt((part[a0:a1] ** 2).mean()) + 1e-12)
            g = voice_rms - GAME_UNDER_VOICE[snd[0]] - (snd[1] if snd[0] == 'steps' and len(snd) > 1 else 0) - rms
            events[-1].update(measured_rms_db=round(float(rms), 2))
        g = min(g, GAME_MAX_GAIN_DB)
        events[-1].update(gain_db=round(float(g), 2))
        game += part * 10 ** (g / 20)
    speaking = [(w['s'] - 0.05, w['e'] + 0.12) for w in words]
    game *= gain_env(n, speaking, SPEECH_DUCK_DB, ramp=0.08)[:, None]
    out += game
    # The one theme from its first note at one fixed level for the whole reel: no fade-in, no dips.
    # Its level is set against the narration's measured loudness.
    music = read_audio(MUSIC_FILE, ss=MUSIC_START)
    music_db = voice_lufs - MUSIC_UNDER_VOICE - lufs_of(music[:, 0])
    assert len(music) >= n, 'the track is shorter than the reel'
    music = music[:n]
    cut = next(s['start'] for s in tl if s['at'] == SYNC_CUT['at'])
    events.append(dict(music=dict(gain_db=round(float(music_db), 2), line7_cut=round(cut, 3),
                                  nearest_beat=round(min(MUSIC_BEATS, key=lambda b: abs(b - cut)), 3))))
    env = np.full(n, 10 ** (music_db / 20), np.float32)
    k = int(0.4 * SR)
    c = np.concatenate([[0], np.cumsum(env, dtype=np.float64)])
    sm = ((c[k:] - c[:-k]) / k).astype(np.float32)
    env = np.concatenate([np.full(k // 2, env[0]), sm, np.full(n - len(sm) - k // 2, env[-1])])
    env[:k] = 10 ** (music_db / 20)                    # the first note at full level
    fo = int(1.0 * SR)
    env[-fo:] *= np.linspace(1, 0, fo)
    out += music * env[:, None]
    write_wav(O / 'audio/music.wav', music * env[:, None])
    write_wav(O / 'audio/voice.wav', np.pad(voice, ((0, n - len(voice)), (0, 0))))
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


# ---------------------------------------------------------------- preview (framing check, no encode)
def grab(sp, t):
    data = subprocess.check_output(['ffmpeg', '-v', 'error', '-ss', f"{sp['t'] + t * sp['speed']:.3f}", '-i', sp['file'],
                                    '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'])
    img = Image.frombuffer('RGB', (2560, 1440), data)
    return ImageOps.mirror(img) if sp.get('flip') else img


def preview(tl, words, caps, total):
    label_delays(tl)
    q = O / 'preview'
    q.mkdir(exist_ok=True)
    f = ImageFont.truetype(FONT, 18)
    tiles = []
    for i, s in enumerate(tl):
        s['total'] = total
        look = compile_look(s['look'], s, words)
        boxes = [clean_box(sp) for sp in s['srcs']]
        for t in (0.03, s['duration'] / 2, max(0.03, s['duration'] - 0.05)):
            images = [grab(sp, t) if 'v' in sp else load_source(sp) for sp in s['srcs']]
            out = compose(images, look, t, boxes)
            if s.get('endcard'):
                out = end_card(out, t)
            la = 1.0 if s.get('label_delay') is None else ease_out((t - s['label_delay']) / LABEL_FADE)
            out = overlay_captions(overlay_label(out, s.get('label'), la), s['start'] + t, caps).resize((270, 480))
            d = ImageDraw.Draw(out)
            for y in (SAFE['top'] / 4, SAFE['bottom'] / 4):
                d.line((0, y, 270, y), fill=(255, 60, 60))
            for x in (SAFE['left'] / 4, SAFE['right'] / 4):
                d.line((x, 0, x, 480), fill=(255, 60, 60))
            tiles.append((out, f"s{i:02d} {s['start'] + t:5.2f}s {s['at'][:16]}"))
    for k in range(0, len(tiles), 24):
        chunk = tiles[k:k + 24]
        sheet = Image.new('RGB', (270 * 8, 510 * math.ceil(len(chunk) / 8)), '#101b14')
        d = ImageDraw.Draw(sheet)
        for j, (im, label) in enumerate(chunk):
            x, y = (j % 8) * 270, (j // 8) * 510
            sheet.paste(im, (x, y))
            d.text((x + 4, y + 486), label, font=f, fill='white')
        sheet.save(q / f'preview-{k // 24:02d}.jpg', quality=88)
    print('preview sheets', math.ceil(len(tiles) / 24), flush=True)


if __name__ == '__main__':
    jobs = int(sys.argv[sys.argv.index('--jobs') + 1]) if '--jobs' in sys.argv else 3
    words, voice_len = prepare_audio()
    i = anchor_index(words, SYNC_CUT['at'])
    pause = 0.22                                                 # the shortest pause we allow there
    PAUSES[SYNC_CUT['pause_after']] = pause
    words, voice_len = prepare_audio()
    cut = words[i]['s'] - CUT_LEAD
    target = next_beat(cut)                                      # the first beat the sentence can reach
    for _ in range(4):
        if abs(cut - target) < 0.5 / FPS:
            break
        pause += target - cut
        PAUSES[SYNC_CUT['pause_after']] = pause
        words, voice_len = prepare_audio()
        cut = words[i]['s'] - CUT_LEAD
    print(f'line 7 cut at {cut:.3f} s, beat at {target:.3f} (pause {pause:.3f} s)', flush=True)
    tl, total = build_timeline(words, voice_len)
    caps = build_caption_layers(group_captions(words, total))
    (O / 'captions.json').write_text(json.dumps([{k: v for k, v in c.items() if k != 'image'} for c in caps], indent=2))
    print(f'{len(tl)} shots, {len(caps)} captions, {total:.2f}s', flush=True)
    if '--prepare' in sys.argv:
        sys.exit()
    if '--preview' in sys.argv:
        preview(tl, words, caps, total)
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
