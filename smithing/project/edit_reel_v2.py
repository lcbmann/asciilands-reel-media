"""Asciilands smithing reel, revision 2.

Run from anywhere:  python project/edit_reel_v2.py [--prepare] [--qa-only] [--jobs N]

Pipeline: cached Whisper words -> silence-aware narration retime -> word-anchored
shots -> Python/Pillow per-frame compositor (smooth sub-pixel crops) -> Pillow
caption PNGs overlaid in ffmpeg with enable='between(t,a,b)' -> voice + real game
sound + quiet theme, one fixed gain to -14 LUFS and a -1 dBTP limiter -> QA stills.

Everything editorial is data near the top of this file: PAUSES, SHOTS, LOOKS,
ACCENTS, END. Originals are only read. v1 files are never written.
"""
from pathlib import Path
import subprocess, json, re, math, sys, hashlib, io, os, shutil
from concurrent.futures import ThreadPoolExecutor
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

R = Path(__file__).resolve().parent
B = R.parent
V = B / 'media/video'
VO = B / 'media/audio/smithingshort.wav'
ASSETS = B / 'assets'
O = R / 'edit-v2'
for d in ['', 'shots', 'overlays', 'qa', 'audio']:
    (O / d).mkdir(parents=True, exist_ok=True)
FINAL = R / 'smithing-v2.mp4'
REVIEW = R / 'smithing-v2-review.mp4'
W, H, FPS, SR = 1080, 1920, 30, 48000
FONT = str(ASSETS / 'fonts/Bold.ttf')
WHITE, GOLD, INK = '#fff9e9', '#efcc77', '#08110c'
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


# ---------------------------------------------------------------- narration
# ASR corrections are matched by context, never by word index.
WORD_FIXES = [(('ram', 'materials'), 0, 'Rare')]
# Silence kept between words, by the punctuation that ends the previous word.
PAUSE_DEFAULT = {'.': 0.30, '?': 0.32, '!': 0.30, ',': 0.20, '': 0.16}
# Deliberate exceptions, keyed by the words that end before the pause.
PAUSES = {
    'you hammered out?': 0.34,
    'paying for their skill.': 0.85,   # the one intentional beat before the question
}
LEAD_IN = 0.06      # silence before the first word
TAIL_PAD = 0.08     # protects consonant tails after a silence starts
HEAD_PAD = 0.07     # protects attacks before a silence ends


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
    (O / 'silencedetect.txt').write_text(r.stderr)
    st = [float(x) for x in re.findall(r'silence_start: ([\d.]+)', r.stderr)]
    en = [float(x) for x in re.findall(r'silence_end: ([\d.]+)', r.stderr)]
    gaps = list(zip(st, en))
    merged = []
    for a, b in gaps:  # silencedetect sometimes splits one pause in two
        if merged and a - merged[-1][1] < 0.02:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return merged


def pause_for(words, i):
    """Target silence after word i."""
    tail = ' '.join(norm(x['w']) for x in words[max(0, i - 4):i + 1])
    for phrase, sec in PAUSES.items():
        if tail.endswith(' '.join(norm(p) for p in phrase.split())):
            return sec, phrase
    m = re.search(r'[.?!,]$', words[i]['w'])
    return PAUSE_DEFAULT[m.group(0) if m else ''], None


def prepare_audio():
    words = load_words()
    audio = np.frombuffer(subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(VO), '-ar', str(SR), '-ac', '1',
                                                   '-f', 'f32le', '-']), dtype=np.float32).copy()
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
        length = b - a
        if length <= target + 0.03:
            report.append(dict(after=words[i]['w'], at=a, kept=length, target=target, cut=0))
            continue
        pre = max(TAIL_PAD, target * 0.55)
        post = max(HEAD_PAD, target - pre)
        remove.append((a + pre, b - post))
        report.append(dict(after=words[i]['w'], at=round(a, 3), original=round(length, 3), kept=round(pre + post, 3), target=target))
    assert used == set(PAUSES), ('pause phrase not found', set(PAUSES) - used)
    keep, cursor = [], 0.0
    for a, b in remove:
        if a > cursor:
            keep.append((cursor, a))
        cursor = max(cursor, b)
    if cursor < duration:
        keep.append((cursor, duration))

    def mapped(t):
        return sum(max(0.0, min(t, b) - a) for a, b in keep)

    # 6 ms ramps at every join: joins fall inside measured silence, so this only removes clicks.
    chunks = []
    for a, b in keep:
        c = audio[round(a * SR):round(b * SR)].copy()
        n = min(int(0.006 * SR), len(c) // 2)
        c[:n] *= np.linspace(0, 1, n)
        c[-n:] *= np.linspace(1, 0, n)
        chunks.append(c)
    voice = np.concatenate(chunks)
    ff(['-f', 'f32le', '-ar', SR, '-ac', 1, '-i', '-', '-c:a', 'pcm_s24le', O / 'voice.wav'], input=voice.tobytes())
    for w in words:
        w.update(source_start=w['s'], source_end=w['e'], s=round(mapped(w['s']), 4), e=round(mapped(w['e']), 4))
    voice_len = len(voice) / SR
    (O / 'words.json').write_text(json.dumps(words, indent=2))
    (O / 'audio-cuts.json').write_text(json.dumps(dict(keep=keep, removed=remove, pauses=report,
                                                       original=duration, voice=voice_len), indent=2))
    return words, voice_len




# ---------------------------------------------------------------- layouts
# All source rectangles are (x, y, w, h) in the original 2560x1440 recordings.
# Destination rectangles are (x, y, w, h) on the 1080x1920 canvas.
# Keyframe times are seconds into the shot; '@phrase' means "when that phrase is
# spoken", 'e' is the shot end. Values ease smoothly between keyframes.

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


# Forge window, measured on smithbadsteel/smithtwoswords/starironhard (same UI build).
FORGE_WINDOW = (410, 150, 1740, 1140)
FORGE_FIT = ((1500, 465, 625, 100), (80, 300, 920, 147))          # Fit % and Condition
FORGE_CONTROLS = ((448, 548, 1030, 150), (0, 468, 1080, 157))      # heat bar, Heat/Cool, hammer, blow
FORGE_ANVIL = ((640, 700, 830, 465), (0, 645, 1080, 605))          # workpiece and "Your piece"


def wide_dst(rect):
    """Where a part of the forge window sits when the whole window is shown 1080 wide."""
    x, y, w, h = rect
    k = W / FORGE_WINDOW[2]
    top = 560
    return ((x - FORGE_WINDOW[0]) * k, top + (y - FORGE_WINDOW[1]) * k, w * k, h * k)


def forge(reveal_at=None, reveal_len=0.9):
    """Vertical arrangement of the real forge UI. With reveal_at, the shot opens on the
    unmodified full window and its three parts then lift into the vertical layout."""
    parts = [FORGE_FIT, FORGE_CONTROLS, FORGE_ANVIL]
    layers = []
    if reveal_at is None:
        for s, d in parts:
            layers.append(dict(src=[(0, s)], dst=[(0, d)]))
        return dict(bg='blur', layers=layers)
    end = (reveal_at, reveal_len)
    layers.append(dict(src=[(0, FORGE_WINDOW)], dst=[(0, wide_dst(FORGE_WINDOW))],
                       alpha=[(0, 1), ('%s' % reveal_at, 1), (f'{reveal_at}+{reveal_len * .6}', 0)]))
    for s, d in parts:
        layers.append(dict(src=[(0, s)], dst=[(0, wide_dst(s)), (reveal_at, wide_dst(s)), (f'{reveal_at}+{reveal_len}', d)],
                           alpha=[(0, 0), (reveal_at, 0), (f'{reveal_at}+0.12', 1)]))
    return dict(bg='blur', layers=layers)


# Inspect window (same geometry for Cleaver, Swordy and the bronze rapier).
INSPECT_SWORD = ((905, 335, 360, 870), (80, 295, 397, 960))
INSPECT_STATS = ((1488, 470, 384, 610), (500, 400, 499, 793))
STATS_METAL = (1488, 612, 384, 222)      # METAL, MAKER, QUALITY rows
STATS_MAKER = (1488, 696, 384, 222)      # MAKER, QUALITY, GRIP rows
STATS_BIG = (80, 420, 920, 532)


def inspect(stats_in=0.0):
    return dict(bg='blur', layers=[
        dict(src=[(0, INSPECT_SWORD[0])], dst=[(0, INSPECT_SWORD[1])]),
        dict(src=[(0, INSPECT_STATS[0])], dst=[(0, INSPECT_STATS[1])],
             alpha=[(0, 0 if stats_in else 1), (stats_in, 0 if stats_in else 1), (stats_in + 0.35, 1)])])


def inspect_detail(steps):
    """Inspect, then the stats column takes over and moves between the rows being spoken
    about. steps: [(time, stats_source_rect)]; the first step leaves the sword."""
    t0 = steps[0][0]
    src = [(0, INSPECT_STATS[0]), (t0, INSPECT_STATS[0]), (f'{t0}+0.45', steps[0][1])]
    dst = [(0, INSPECT_STATS[1]), (t0, INSPECT_STATS[1]), (f'{t0}+0.45', STATS_BIG)]
    for (_, prev), (t, rect) in zip(steps, steps[1:]):
        src += [(f'{t}-0.05', prev), (f'{t}+0.4', rect)]
    return dict(bg='blur', layers=[
        dict(src=[(0, INSPECT_SWORD[0])], dst=[(0, INSPECT_SWORD[1])], alpha=[(0, 1), (t0, 1), (f'{t0}+0.3', 0)]),
        dict(src=src, dst=dst)])


def shape_match():
    """Push into the finished steel piece so its blade lands exactly where the Inspect
    sword blade appears in the next shot (tip y=311, guard y=1036, centre x=286)."""
    tip, guard_y, cx = 643, 920, 1279          # finished-piece preview in smithbadsteel at 64.5s
    (sx, sy, sw, sh), (dx, dy, dw, dh) = INSPECT_SWORD
    ks = dh / sh                                # Swordy Inspect blade: tip y349, guard y979, centre x1084
    out_tip, out_guard, out_cx = dy + (349 - sy) * ks, dy + (979 - sy) * ks, dx + (1084 - sx) * ks
    k = (out_guard - out_tip) / (guard_y - tip)
    end = (cx - out_cx / k, tip - out_tip / k, W / k, H / k)
    start = (893, 60, 771, 1371)
    return dict(bg=None, layers=[dict(src=[(0, start), ('e', end)], dst=[(0, (0, 0, W, H))])])


def pack():
    return dict(bg='blur', layers=[
        dict(src=[(0, (866, 300, 614, 330))], dst=[(0, (80, 300, 920, 494))]),       # weapons and tools, Cleaver selected
        dict(src=[(0, (440, 300, 416, 420))], dst=[(0, (330, 815, 420, 424))])])     # the player who made them


def materials():
    return dict(bg='blur', layers=[
        dict(src=[(0, (868, 362, 762, 112))], dst=[(0, (80, 300, 920, 135))]),       # material row
        dict(src=[(0, (1772, 372, 345, 520))], dst=[(0, (272, 455, 537, 810))])])    # selected description, enlarged


def naming():
    return dict(bg='blur', layers=[
        dict(src=[(0, (1503, 590, 620, 120))], dst=[(0, (82, 320, 917, 178))]),      # Item name field
        dict(src=[(0, (1100, 715, 370, 440))], dst=[(0, (244, 540, 592, 704))])])    # the piece being named


# ---------------------------------------------------------------- shots
# at: first spoken words of the shot (cut lands CUT_LEAD before them).
# t: source in-point (seconds) or 'cont' to continue the previous shot's source.
# speed: 'fit:<source out>' derives the speed from the narration length.
# sound: None | ('live', dB) real recorded audio | ('hits', dB, min_gap) gated real strikes.
CUT_LEAD = 0.07
SHOTS = [
    dict(at='I want players', src='yorkarrival', t=6.00, look=world(1280, 750, 1280, 1230),
         note='Walk through York starts at 6.00; turn ends 5.93.'),
    dict(at='cross a massive map', src='yorkarrival', t='cont', look=world(1280, 750, 1230, 1180),
         provisional='mapzoomout.mkv', note='PROVISIONAL: York walk continues until the world-map zoom-out pickup exists.'),
    dict(at='just to buy a', src='smithtwoswords', t=83.62, speed='fit:86.45',
         look=inspect_detail([('@what it\'s made', STATS_METAL), ('@because of who', STATS_MAKER)]),
         note='Cleaver Inspect: sword, then METAL Iron, then MAKER Smither.'),
    dict(at="I'm building Asciilands", src='yorksmith', t=18.0, look=world(1280, 700, 1280, 1200)),
    dict(at='Weapons and tools', src='smithtwoswords', t=80.95, speed='fit:83.30', look=pack()),
    dict(at='But for that', src='yorksmith', t=14.55, look=world(1290, 700, 1280, 900, 1290, 690),
         note='York Public Blacksmith sign, Built by Louie.'),
    dict(at='people need a', src='smithersmith', t=4.60, look=world(1265, 740, 1280, 1060, 1250, 700),
         note='Walk up to the door, enter at 5.43, Smither is inside.'),
    dict(at='Rare materials', src='orbitdesertmeteor', t=12.0, look=world(1280, 760, 1280, 1150)),
    dict(at='Better damage and', src='inventorymaterials', t=17.95, speed='fit:20.75', look=materials(),
         note='Star Iron description.'),
    dict(at='But if anyone', src='inventorymaterials', t=0.0, look=materials(), provisional='oneclickarrows.mkv',
         note='PROVISIONAL: ingredients in the Pack. Replace with the one-click Arrows pickup.'),
    dict(at='I wanted being', src='smithbadsteel', t=3.00, look=forge(reveal_at='@So in Asciilands'),
         sound=('live', 7.0), note='Real-time steel forging with its own sound; full UI first.'),
    dict(at='Two players can', src='smithbadsteel', t='cont', speed='fit:55.60', look=forge(),
         sound=('hits', 6.0, 0.42), note='Timelapse of the same steel piece to Fit 97%.'),
    dict(at='And that shape', src='smithbadsteel', t=64.48, speed='fit:65.45', look=shape_match(), sound=('live', 6.0),
         note='Finished steel piece, matched in size and position to the Inspect blade.'),
    dict(at="That's the actual", src='smithbadsteel', t=75.55, look=inspect(stats_in=0.45),
         note='Swordy Inspect: the same imperfect blade.'),
    dict(at='You can give', src='smithbadsteel', t=57.40, speed='fit:60.75', look=naming(),
         note='Typing the name Swordy.'),
    dict(at="and your maker's", src='smithbadsteel', t=77.49, speed='fit:79.65',
         look=inspect_detail([(0.35, STATS_MAKER)]), note='Swordy Inspect: MAKER Smither.'),
    dict(at='The basic metals', src='smithtwoswords', t=118.0, speed='fit:145.0', look=forge(),
         sound=('hits', 4.0, 0.30), note='Bronze rapier to its late shape.'),
    dict(at='The higher -tier', src='starironhard', t=4.60, look=forge(), sound=('hits', 6.0, 0.25),
         note='Star Iron heat window and surges, real time.'),
    dict(at='A good smith', src='yorksmith', t=10.5, look=world(1280, 660, 1280, 1180)),
    dict(at='Someone asks who', src='smithtwoswords', t=83.62, speed='fit:86.45',
         look=inspect_detail([(0.3, STATS_MAKER)]), note='Cleaver: MAKER Smither, QUALITY Masterwork.'),
    dict(at='you point them', src='smithersmith', t=8.17, look=world(1250, 680, 1100, 860, 1240, 650)),
    dict(at="You're not just", src='orbitsnowmeteor', t=48.0, look=world(1280, 780, 1280, 1150)),
    dict(at="You're paying for their", src='smithtwoswords', t=38.0, look=forge(), sound=('hits', 7.0, 0.2),
         note='Iron arming sword, real-time strikes carry the beat and the question.'),
    dict(at='Tell me in', src='yorksmith', t=21.0, look=world(1280, 700, 1280, 1200), end=True),
]
END_TAIL = 2.5          # seconds of picture after the voice ends
ACCENTS = ['SWORD', 'SWORDS', 'MAP', 'MASSIVE', 'WHO', 'ECONOMY', 'PLAYERS', 'INTERESTING', 'RARE', 'DURABILITY',
           'DAMAGE', 'CLICK', 'BUTTON', 'SAME', 'VALUE', 'GOOD', 'BLADE', 'HEAT', 'STRIKE', 'SHAPE', 'METAL', 'STEEL',
           'DIFFERENT', 'FORGING', 'HARDER', 'LONGER', 'HAMMERED', 'KEEP', 'NAME', "MAKER'S", 'MARK', 'BUYS',
           'FORGIVING', 'SERIOUS', 'SKILL', 'SHOP', 'REPUTATION', 'MASTERWORK', 'CUSTOMER', 'MATERIALS', 'TIME',
           'REWARDING', 'CHORE', 'COMMENTS']


def anchor_index(words, phrase):
    text = [norm(x['w']) for x in words]
    parts = [norm(x) for x in phrase.split()]
    hits = [i for i in range(len(text) - len(parts) + 1) if text[i:i + len(parts)] == parts]
    assert len(hits) == 1, (phrase, hits)
    return hits[0]


def frame(t):
    return round(t * FPS) / FPS


def build_timeline(words, voice_len):
    total = frame(voice_len + END_TAIL)
    tl = []
    for s in SHOTS:
        wi = anchor_index(words, s['at'])
        start = 0.0 if wi == 0 else frame(words[wi]['s'] - CUT_LEAD)
        tl.append(dict(s, word=wi, start=start))
    for i, s in enumerate(tl):
        s['end'] = tl[i + 1]['start'] if i + 1 < len(tl) else total
        s['duration'] = round(s['end'] - s['start'], 4)
        s['frames'] = round(s['duration'] * FPS)
        s['file'] = str(src(s['src']))
        if s['t'] == 'cont':
            assert tl[i - 1]['src'] == s['src']
            s['t'] = tl[i - 1]['t_out']
        sp = s.get('speed', 1)
        if isinstance(sp, str):
            sp = (float(sp.split(':')[1]) - s['t']) / s['duration']
        s['speed'] = round(sp, 5)
        s['t_out'] = round(s['t'] + s['duration'] * s['speed'], 4)
        avail = probe_duration(s['file']) - 0.05
        assert s['t_out'] <= avail, (s['at'], s['t_out'], avail)
        assert 0.25 < s['speed'] < 30, (s['at'], s['speed'])
    (O / 'timeline.json').write_text(json.dumps([{k: v for k, v in s.items() if k != 'look'} for s in tl], indent=2))
    return tl, total


def resolve_time(expr, dur, words, shot_start):
    if isinstance(expr, (int, float)):
        return float(expr)
    e = str(expr)

    def at(m):
        return str(words[anchor_index(words, m.group(1))]['s'] - shot_start)
    e = re.sub(r"@([^+\-]+[^+\-\s])", lambda m: at(m), e)
    e = e.replace('e', str(dur)) if re.fullmatch(r'[e0-9.+\- ]+', e) else e
    return float(eval(e, {'__builtins__': {}}))


def ease(u):
    u = min(1.0, max(0.0, u))
    return u * u * (3 - 2 * u)


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
    for layer in look['layers']:
        lin = layer.get('ease') == 'linear'
        conv = lambda seq: sorted([(resolve_time(t, shot['duration'], words, shot['start']), v) for t, v in seq], key=lambda x: x[0])
        out['layers'].append(dict(src=conv(layer['src']), dst=conv(layer['dst']),
                                  alpha=conv(layer.get('alpha', [(0, 1)])), linear=lin))
    return out


# ---------------------------------------------------------------- picture
def background(img, cx=1280):
    small = img.reduce(10)
    w = 144 * 9 / 16
    x0 = min(max(cx / 10 - w / 2, 0), 256 - w)
    bg = small.resize((108, 192), Image.BILINEAR, box=(x0, 0, x0 + w, 144)).filter(ImageFilter.GaussianBlur(5))
    bg = bg.resize((W, H), Image.BILINEAR)
    return Image.blend(bg, Image.new('RGB', (W, H), (6, 14, 9)), 0.55)


def render_shot(i, s, words):
    dest = O / 'shots' / f'{i:02d}.mp4'
    look = compile_look(s['look'], s, words)
    extra = {k: v for k, v in s['look'].items() if k not in ('layers',)}
    key = json.dumps(dict(look=look, extra=extra, t=s['t'], speed=s['speed'], frames=s['frames'], file=s['file'], v=3),
                     sort_keys=True, default=str)
    sig = hashlib.sha256(key.encode()).hexdigest()
    stamp = dest.with_suffix('.sha')
    if dest.exists() and stamp.exists() and stamp.read_text() == sig:
        return dest
    n = s['frames']
    dec = subprocess.Popen(['ffmpeg', '-v', 'error', '-ss', f"{s['t']:.4f}", '-i', s['file'],
                            '-vf', f"setpts=(PTS-STARTPTS)/{s['speed']:.6f},fps={FPS}",
                            '-frames:v', str(n), '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], stdout=subprocess.PIPE)
    enc = subprocess.Popen(['ffmpeg', '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{W}x{H}',
                            '-r', str(FPS), '-i', '-', '-c:v', 'libx264', '-preset', 'medium', '-crf', '16',
                            '-pix_fmt', 'yuv420p', '-threads', '2', str(dest)], stdin=subprocess.PIPE)
    size = 2560 * 1440 * 3
    last = None
    for f in range(n):
        buf = dec.stdout.read(size)
        if len(buf) < size:          # never freeze mid-motion: only allowed on the very last frames
            assert last is not None and f >= n - 2, (s['at'], f, n)
            buf = last
        last = buf
        img = Image.frombuffer('RGB', (2560, 1440), buf)
        t = f / FPS
        if look['bg']:
            canvas = background(img)
            fade = s['look'].get('bg_fade')
            if fade:   # during the reveal the backdrop starts as the plain game screen
                ta = resolve_time(fade[0], s['duration'], words, s['start'])
                u = ease((t - ta) / fade[1])
                plain = Image.new('RGB', (W, H), (6, 14, 9))
                canvas = Image.blend(plain, canvas, u)
        else:
            canvas = Image.new('RGB', (W, H))
        for layer in look['layers']:
            a = keyed(layer['alpha'], t)
            if a <= 0.01:
                continue
            sx, sy, sw, sh = keyed(layer['src'], t, layer['linear'])
            dx, dy, dw, dh = keyed(layer['dst'], t, layer['linear'])
            cx, sw = sx + sw / 2, sh * dw / dh      # crop aspect always follows the destination: no stretching
            sx = cx - sw / 2
            sx = min(max(sx, 0), 2560 - sw)
            sy = min(max(sy, 0), 1440 - sh)
            piece = img.resize((max(1, round(dw)), max(1, round(dh))), Image.LANCZOS, box=(sx, sy, sx + sw, sy + sh))
            if a < 0.99:
                canvas.paste(Image.blend(canvas.crop((round(dx), round(dy), round(dx) + piece.width, round(dy) + piece.height)), piece, a),
                             (round(dx), round(dy)))
            else:
                canvas.paste(piece, (round(dx), round(dy)))
        enc.stdin.write(canvas.tobytes())
    dec.stdout.close()
    enc.stdin.close()
    assert enc.wait() == 0
    dec.wait()
    stamp.write_text(sig)
    print(f'shot {i:02d} {s["at"]!r} {s["duration"]:.2f}s', flush=True)
    return dest


def render_picture(tl, words, jobs):
    with ThreadPoolExecutor(jobs) as ex:
        list(ex.map(lambda a: render_shot(a[0], a[1], words), enumerate(tl)))
    (O / 'concat.txt').write_text('\n'.join(f"file 'shots/{i:02d}.mp4'" for i in range(len(tl))))
    ff(['-f', 'concat', '-safe', 0, '-i', O / 'concat.txt', '-c', 'copy', O / 'picture.mp4'])


# ---------------------------------------------------------------- captions
CAP_SIZE, CAP_MAX_W, CAP_TOP = 74, 860, 1270   # text width; the soft shadow adds about 30 px
STROKE = 9


def font(size=CAP_SIZE):
    return ImageFont.truetype(FONT, size)


def measure(text, f):
    d = ImageDraw.Draw(Image.new('L', (1, 1)))
    b = d.textbbox((0, 0), text, font=f, stroke_width=STROKE)
    return b[2] - b[0]


WEAK_END = {'a', 'an', 'the', 'to', 'of', 'and', 'in', 'with', 'for', 'from', 'at', 'your', 'their', 'into', 'on',
            'just', 'can', 'be', 'by', 'it', 'its', 'im', 'that', 'this', 'does', 'or', 'but', 'if', 'some'}
STRONG_START = {'but', 'and', 'or', 'just', 'instead', 'when', 'to', 'for', 'into'}
# Editorial caption breaks: a caption starts on these spoken words.
CAPTION_STARTS = ['because of what', 'can click a button', 'with very different', 'when someone else',
                  'mark stays', 'and end up']


def group_captions(words, total):
    """2-4 words per caption, breaking at punctuation and phrase edges, never ending on a
    weak word when it can be avoided, never splitting hyphen fragments."""
    f = font()
    groups, cur = [], []
    tokens = []
    for w in words:   # attach '-run', '-tier' to the previous word
        if w['w'].startswith('-') and tokens:
            tokens[-1] = dict(tokens[-1], w=tokens[-1]['w'] + w['w'], e=w['e'])
        else:
            tokens.append(dict(w))
    # Split each spoken phrase (between punctuation) into balanced chunks of 2-4 words.
    phrases, cur = [], []
    for w in tokens:
        cur.append(w)
        if re.search(r'[.,?!]$', w['w']):
            phrases.append(cur)
            cur = []
    if cur:
        phrases.append(cur)
    for ph in phrases:
        n = len(ph)
        if n <= 4:
            groups.append(ph)
            continue
        # chunks are 2-4 words; 5 only when a shorter split would flash too briefly
        best = None

        def splits(k):          # all ways to cut k words into chunks of 2..4
            if k == 0:
                return [[]]
            return [[c] + rest for c in (2, 3, 4, 5) if c <= k for rest in splits(k - c)]
        for sizes in splits(n):
            pos, cost = 0, 0.0
            for size in sizes:
                chunk = ph[pos:pos + size]
                pos += size
                last = norm(chunk[-1]['w'])
                if pos < n and last in WEAK_END:
                    cost += 3
                if pos < n and norm(ph[pos]['w']) in STRONG_START:
                    cost -= 1
                if measure(' '.join(x['w'] for x in chunk).upper(), f) > CAP_MAX_W:
                    cost += 2
                head = ' '.join(norm(x['w']) for x in chunk)
                if any(head.startswith(' '.join(norm(p) for p in c.split())) for c in CAPTION_STARTS):
                    cost -= 5
                cost += abs(size - 3) * 0.4 + (3 if size == 5 else 0)
                shown = (ph[pos]['s'] if pos < n else chunk[-1]['e']) - chunk[0]['s']
                if shown < 0.4:          # a caption must stay up long enough to read
                    cost += 6
            cost += len(sizes) * 0.2
            if best is None or cost < best[0]:
                best = (cost, sizes)
        pos = 0
        for size in best[1]:
            groups.append(ph[pos:pos + size])
            pos += size
    # avoid orphan single words by merging them into the previous group when it fits
    merged = []
    for g in groups:
        if len(g) == 1 and merged and not re.search(r'[.,?!]$', merged[-1][-1]['w']) and len(merged[-1]) < 4:
            merged[-1] = merged[-1] + g
        else:
            merged.append(g)
    caps = []
    for i, g in enumerate(merged):
        start = g[0]['s']
        nxt = merged[i + 1][0]['s'] if i + 1 < len(merged) else total
        end = nxt if nxt - g[-1]['e'] < 0.45 else g[-1]['e'] + 0.25
        caps.append(dict(text=' '.join(x['w'] for x in g).upper(), start=round(start, 3), end=round(end, 3)))
    caps[-1]['end'] = total          # the closing ask stays up over the ending
    return caps


def caption_image(text):
    f = font()
    words = text.split()
    lines, row = [], []
    for w in words:                  # wrap, never shrink
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
        x = (W - width) / 2
        y = 10 + li * lh
        for w in line:
            fill = GOLD if w == accent else WHITE
            ds.text((x + 4, y + 8), w, font=f, fill=(0, 0, 0, 170), stroke_width=STROKE + 3, stroke_fill=(0, 0, 0, 170))
            d.text((x, y), w, font=f, fill=fill, stroke_width=STROKE, stroke_fill=INK)
            x += d.textlength(w, font=f) + space
    shadow = shadow.filter(ImageFilter.GaussianBlur(7))
    shadow.alpha_composite(im)
    return shadow, len(lines)


def end_card(total, voice_end):
    """A restrained end: the round emblem and the name in the caption type, floating over
    the moving town. No panel, no textured wordmark (it reads as a sign in the scene)."""
    emblem = Image.open(ASSETS / 'brand/profile-round-transparent-1080.png').convert('RGBA').resize((190, 190), Image.LANCZOS)
    f = font(96)
    name = 'ASCIILANDS'
    tw = measure(name, f)
    im = Image.new('RGBA', (W, 380))
    shadow = Image.new('RGBA', im.size)
    shadow.paste((0, 0, 0, 150), ((W - 190) // 2 + 4, 28), emblem.split()[3])
    d, ds = ImageDraw.Draw(im), ImageDraw.Draw(shadow)
    im.alpha_composite(emblem, ((W - 190) // 2, 20))
    x = (W - d.textlength(name, font=f)) / 2
    ds.text((x + 4, 240), name, font=f, fill=(0, 0, 0, 170), stroke_width=STROKE + 4, stroke_fill=(0, 0, 0, 170))
    d.text((x, 232), name, font=f, fill=GOLD, stroke_width=STROKE + 1, stroke_fill=INK)
    shadow = shadow.filter(ImageFilter.GaussianBlur(8))
    shadow.alpha_composite(im)
    box = shadow.getbbox()
    card = shadow.crop(box)
    card.save(O / 'overlays/end-card.png')
    y = 300
    assert box[0] >= SAFE['left'] and box[2] <= SAFE['right'] and y + card.height < CAP_TOP - 20, box
    return dict(file=O / 'overlays/end-card.png', x=box[0], y=y, start=round(voice_end - 0.2, 3), fade=0.7, w=card.width, h=card.height)


def captions_and_overlays(caps, total, voice_end, provisional, review):
    items = []
    for i, c in enumerate(caps):
        im, nlines = caption_image(c['text'])
        box = im.getbbox()
        im = im.crop(box)
        x, y = box[0], CAP_TOP + box[1]       # fixed top line: a second line grows downward, never into the footage
        c.update(lines=nlines, x=x, y=y, w=im.width, h=im.height)
        assert x >= SAFE['left'] and x + im.width <= SAFE['right'] and y >= SAFE['top'] and y + im.height <= SAFE['bottom'], c
        p = O / 'overlays' / f'cap-{i:03d}.png'
        im.save(p)
        items.append(dict(file=p, x=x, y=y, start=c['start'], end=c['end']))
    if review:
        f = font(34)
        for p in provisional:
            im = Image.new('RGBA', (W, 90))
            d = ImageDraw.Draw(im)
            msg = 'PROVISIONAL: ' + p['needs']
            tw = d.textlength(msg, font=f)
            d.rounded_rectangle(((W - tw) / 2 - 22, 12, (W + tw) / 2 + 22, 78), 14, fill=(150, 30, 30, 230))
            d.text(((W - tw) / 2, 22), msg, font=f, fill='white')
            path = O / 'overlays' / f"prov-{p['needs'].split('.')[0]}.png"
            im.save(path)
            items.append(dict(file=path, x=0, y=170, start=p['start'], end=p['end']))
    (O / 'captions.json').write_text(json.dumps(caps, indent=2))
    return items


def composite(tl, caps, total, voice_end, out, review=False):
    provisional = [dict(needs=s['provisional'], start=s['start'], end=s['end']) for s in tl if s.get('provisional')]
    items = captions_and_overlays(caps, total, voice_end, provisional, review)
    endc = end_card(total, voice_end)
    args = ['-i', O / 'picture.mp4']
    chain, last = [], '0:v'
    for k, it in enumerate(items):
        # each still is only decoded over its own window (itsoffset), not for the whole reel
        args += ['-loop', 1, '-framerate', FPS, '-t', f"{it['end'] - it['start']:.3f}", '-itsoffset', f"{it['start']:.3f}", '-i', it['file']]
        chain.append(f"[{last}][{k + 1}:v]overlay={it['x']}:{it['y']}:eof_action=pass:enable='between(t,{it['start']:.3f},{it['end'] - 0.001:.3f})'[v{k}]")
        last = f'v{k}'
    n = len(items) + 1
    args += ['-loop', 1, '-framerate', FPS, '-t', f"{total - endc['start']:.3f}", '-itsoffset', f"{endc['start']:.3f}", '-i', endc['file']]
    chain.append(f"[{n}:v]format=rgba,fade=t=in:st={endc['start']}:d={endc['fade']}:alpha=1[logo]")
    chain.append(f"[{last}][logo]overlay={endc['x']}:{endc['y']}:enable='between(t,{endc['start']:.3f},{total})',format=yuv420p[vout]")
    (O / 'filter.txt').write_text(';\n'.join(chain))
    ff([*args, '-filter_complex_script', O / 'filter.txt', '-map', '[vout]', '-t', total, '-an', '-c:v', 'libx264',
        '-preset', 'slow', '-crf', '17' if not review else '25', '-threads', 4, out])
    return endc


# ---------------------------------------------------------------- sound
def read_audio(path, ss=0.0, t=None, ch=2):
    a = ['ffmpeg', '-v', 'error', '-ss', f'{ss:.4f}'] + (['-t', f'{t:.4f}'] if t else []) + \
        ['-i', str(path), '-vn', '-ac', str(ch), '-ar', str(SR), '-f', 'f32le', '-']
    return np.frombuffer(subprocess.check_output(a), dtype=np.float32).reshape(-1, ch).copy()


def strike_onsets(path, a, b):
    """Hammer strikes in [a, b] of a recording: sharp, tonal onsets (not the heat whoosh)."""
    x = read_audio(path, a, b - a, 1)[:, 0]
    hp = np.diff(x, prepend=0)
    hop = 240
    n = len(hp) // hop
    db = 10 * np.log10((hp[:n * hop].reshape(n, hop) ** 2).mean(1) + 1e-12)
    hits = []
    for i in range(12, n - 8):
        if db[i] - db[i - 12:i - 2].mean() > 14 and db[i] > db.max() - 45 and db[i] >= db[i - 1] and (not hits or i - hits[-1] > 30):
            seg = x[i * hop + 240:i * hop + 240 + 2048]
            if len(seg) < 2048:
                continue
            # Strikes ring at a metal-specific pitch; the heat whoosh is noise. Spectral
            # flatness separates them (measured: strikes 0.08-0.41, whooshes 0.71-0.80).
            spec = np.abs(np.fft.rfft(seg * np.hanning(2048))) + 1e-9
            fr = np.fft.rfftfreq(2048, 1 / SR)
            band = spec[(fr > 200) & (fr < 4000)]
            if np.exp(np.log(band).mean()) / band.mean() < 0.55:
                hits.append(i)
    return [a + i * hop / SR for i in hits]


def mix(tl, total):
    n = int(round(total * SR))
    voice = read_audio(O / 'voice.wav')
    out = np.zeros((n, 2), np.float32)
    out[:len(voice)] += voice[:n]
    game = np.zeros((n, 2), np.float32)
    events = []
    for s in tl:
        snd = s.get('sound')
        if not snd:
            continue
        i0 = int(s['start'] * SR)
        if snd[0] == 'live':
            # real-time sound; picture may be held up to 30% slower on a static, finished panel
            assert abs(s['speed'] - 1) < 0.3, s['at']
            tail = 0.35 if s is not tl[-1] else 0       # let the last ring decay across the cut
            a = read_audio(s['file'], s['t'], s['duration'] + tail) * 10 ** (snd[1] / 20)
            r = int(0.012 * SR)
            a[:r] *= np.linspace(0, 1, r)[:, None]
            k = int(tail * SR) or r
            a[-k:] *= np.linspace(1, 0, k)[:, None]
            m = min(len(a), n - i0)
            game[i0:i0 + m] += a[:m]
            events.append(dict(shot=s['at'], mode='live', source=[s['t'], s['t'] + s['duration']]))
        else:
            gain, gap = snd[1], snd[2]
            onsets = strike_onsets(s['file'], s['t'], s['t_out'])
            placed, last = [], -9
            for o in onsets:
                t_out = s['start'] + (o - s['t']) / s['speed']
                if t_out - last < gap or t_out > s['end'] - 0.05:
                    continue
                hit = read_audio(s['file'], max(0, o - 0.012), 0.42) * 10 ** (gain / 20)
                hit *= np.concatenate([np.linspace(0, 1, int(0.004 * SR)), np.ones(len(hit) - int(0.004 * SR))])[:, None]
                env = np.exp(-np.linspace(0, 4.5, len(hit)))[:, None]   # short, clean tail: no music bleed
                hit *= env
                j = int((t_out - 0.012) * SR)
                m = min(len(hit), n - j)
                game[j:j + m] += hit[:m]
                placed.append(round(t_out, 3))
                last = t_out
            events.append(dict(shot=s['at'], mode='hits', onsets_source=[round(o, 3) for o in onsets], placed_output=placed))
    out += game
    # Theme: quiet, a little lower while real forge sound plays, fixed envelope (no ducking detector).
    music = read_audio(ASSETS / 'music/asciilands-theme-loop.ogg')
    reps = int(np.ceil(n / len(music)))
    music = np.tile(music, (reps, 1))[:n]
    env = np.full(n, 10 ** (-27 / 20), np.float32)
    for s in tl:
        if s.get('sound') and s['sound'][0] == 'live':
            a, b = int(s['start'] * SR), int(s['end'] * SR)
            env[a:b] = 10 ** (-33 / 20)
    k = int(0.4 * SR)
    c = np.concatenate([[0], np.cumsum(env, dtype=np.float64)])     # 0.4 s moving average: smooth fixed level changes
    env = ((c[k:] - c[:-k]) / k).astype(np.float32)
    env = np.concatenate([np.full(k // 2, env[0]), env, np.full(n - len(env) - k // 2, env[-1])])
    fi, fo = int(1.0 * SR), int(2.2 * SR)
    env[:fi] *= np.linspace(0, 1, fi)
    env[-fo:] *= np.linspace(1, 0, fo)
    out += music * env[:, None]
    write_wav(O / 'audio/mix.wav', out)
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
    report = dict(before=before, fixed_gain_db=round(gain, 3), master=m)
    (O / 'audio-report.json').write_text(json.dumps(report, indent=2))
    return report


def write_wav(path, a):
    ff(['-f', 'f32le', '-ar', SR, '-ac', a.shape[1], '-i', '-', '-c:a', 'pcm_f32le', path], input=a.astype(np.float32).tobytes())


def loudness(p):
    r = subprocess.run(['ffmpeg', '-hide_banner', '-i', str(p), '-af', 'loudnorm=I=-14:TP=-1:LRA=11:print_format=json',
                        '-f', 'null', os.devnull], capture_output=True, text=True)
    d = json.loads(re.findall(r'\{[^{}]*"input_i"[^{}]*\}', r.stderr, re.S)[-1])
    return {k: float(v) if re.fullmatch(r'-?[\d.]+|-inf', v) else v for k, v in d.items()}


def mux(video, out, total, aac='192k'):
    ff(['-i', video, '-i', O / 'audio/master.wav', '-map', '0:v', '-map', '1:a', '-c:v', 'copy', '-c:a', 'aac', '-b:a', aac,
        '-t', total, '-movflags', '+faststart', out])


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
        for y in (SAFE['top'] / 4, SAFE['bottom'] / 4):   # safe-area guides on the QA copy only
            d.line((0, y, 270, y), fill=(255, 60, 60), width=1)
        for x in (SAFE['left'] / 4, SAFE['right'] / 4):
            d.line((x, 0, x, 480), fill=(255, 60, 60), width=1)
        im.save(q / f'{i:03d}.jpg')
        shot = next(j for j, s in enumerate(tl) if s['start'] <= t < s['end'] or j == len(tl) - 1)
        recs.append(dict(time=t, file=f'{i:03d}.jpg', shot=shot, at=tl[shot]['at']))
    per = 24
    for k in range(0, len(recs), per):
        chunk = recs[k:k + per]
        sheet = Image.new('RGB', (270 * 8, 510 * math.ceil(len(chunk) / 8)), '#101b14')
        d = ImageDraw.Draw(sheet)
        for j, r in enumerate(chunk):
            x, y = (j % 8) * 270, (j // 8) * 510
            sheet.paste(Image.open(q / r['file']), (x, y))
            d.text((x + 6, y + 484), f"{r['file'][:3]} {r['time']:.2f}s s{r['shot']:02d}", font=f, fill='white')
        sheet.save(q / f'sheet-{k // per:02d}.jpg', quality=88)
    (q / 'index.json').write_text(json.dumps(recs, indent=2))
    return recs


def checks(tl, caps, total, report):
    problems = []
    for a, b in zip(caps, caps[1:]):
        if a['end'] > b['start'] + 1e-6:
            problems.append(f"caption overlap {a['text']} / {b['text']}")
    for c in caps:
        if c['x'] < SAFE['left'] or c['x'] + c['w'] > SAFE['right'] + 8 or c['y'] < SAFE['top'] or c['y'] + c['h'] > SAFE['bottom']:
            problems.append(f"caption outside safe area: {c['text']}")
        if c['end'] - c['start'] < 0.25:
            problems.append(f"caption too brief ({c['end'] - c['start']:.2f}s): {c['text']}")
    if sum(s['frames'] for s in tl) != round(total * FPS):
        problems.append('shot frames do not add up to total')
    if report['master']['input_tp'] > -1.0:
        problems.append(f"true peak {report['master']['input_tp']}")
    if abs(report['master']['input_i'] + 14) > 0.3:
        problems.append(f"loudness {report['master']['input_i']}")
    (O / 'checks.json').write_text(json.dumps(dict(problems=problems), indent=2))
    return problems


# ---------------------------------------------------------------- main
if __name__ == '__main__':
    jobs = int(sys.argv[sys.argv.index('--jobs') + 1]) if '--jobs' in sys.argv else 3
    words, voice_len = prepare_audio()
    tl, total = build_timeline(words, voice_len)
    caps = group_captions(words, total)
    print(f'{len(tl)} shots, {len(caps)} captions, {total:.2f}s', flush=True)
    if '--prepare' in sys.argv:
        sys.exit()
    if '--qa-only' not in sys.argv:
        render_picture(tl, words, jobs)
        composite(tl, caps, total, voice_len, O / 'captioned.mp4')
        composite(tl, caps, total, voice_len, O / 'captioned-review.mp4', review=True)
        report = mix(tl, total)
        mux(O / 'captioned.mp4', FINAL, total)
        mux(O / 'captioned-review.mp4', REVIEW, total, aac='128k')
        print('audio', report['master'], flush=True)
    else:
        report = json.loads((O / 'audio-report.json').read_text())
        caps = json.loads((O / 'captions.json').read_text())
    recs = qa(tl, caps, total, FINAL)
    problems = checks(tl, caps, total, report)
    print('QA frames', len(recs), 'problems', problems, flush=True)
