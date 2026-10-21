"""Dense framing check of one shot: python project/preview_shot.py SHOT_INDEX [FRAMES]"""
import sys, importlib.util
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
spec = importlib.util.spec_from_file_location('ed', Path(__file__).with_name('edit_towns_v2.py'))
ed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ed)
words, voice_len = ed.prepare_audio()
i7 = ed.anchor_index(words, ed.SYNC_CUT['at'])
tl, total = ed.build_timeline(words, voice_len)
k, n = int(sys.argv[1]), int(sys.argv[2]) if len(sys.argv) > 2 else 12
s = tl[k]
look = ed.compile_look(s['look'], s, words)
boxes = [ed.clean_box(sp) for sp in s['srcs']]
stills = [None if 'v' in sp else ed.load_source(sp) for sp in s['srcs']]
f = ImageFont.truetype('arial.ttf', 18)
sheet = Image.new('RGB', (270 * min(n, 8), 500 * ((n + 7) // 8)))
d = ImageDraw.Draw(sheet)
for j in range(n):
    t = s['duration'] * j / (n - 1) if n > 1 else 0
    t = min(t, s['duration'] - 0.02)
    imgs = [ed.grab(sp, t) if 'v' in sp else st for sp, st in zip(s['srcs'], stills)]
    out = ed.compose(imgs, look, t, boxes).resize((270, 480))
    x, y = (j % 8) * 270, (j // 8) * 500
    sheet.paste(out, (x, y))
    d.text((x + 4, y + 482), f'{t:.2f}s', fill='white', font=f)
p = ed.O / 'preview' / f'shot{k:02d}.jpg'
sheet.save(p, quality=88)
print(p)
