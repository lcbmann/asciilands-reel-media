"""Contact sheets for footage review: one frame every STEP seconds, labelled with source time.
python project/footage_sheets.py NAME [STEP] [START] [END]"""
import sys, subprocess, io, math
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
R = Path(__file__).resolve().parent
V = R.parent / 'media/video'
out = R / 'edit-v1/review'
out.mkdir(parents=True, exist_ok=True)
name = sys.argv[1]
step = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0
dur = float(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(V / f'{name}.mkv')]))
a = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
b = min(dur - 0.05, float(sys.argv[4])) if len(sys.argv) > 4 else dur - 0.05
ts = [round(a + k * step, 3) for k in range(int((b - a) / step) + 1)]
TW, TH, C = 400, 225, 6
f = ImageFont.truetype('arialbd.ttf', 20)
sheet = Image.new('RGB', (TW * C, (TH + 4) * math.ceil(len(ts) / C)), 'black')
d = ImageDraw.Draw(sheet)
for k, t in enumerate(ts):
    data = subprocess.check_output(['ffmpeg', '-v', 'error', '-ss', f'{t:.3f}', '-i', str(V / f'{name}.mkv'), '-frames:v', '1',
                                    '-vf', f'scale={TW}:{TH}', '-f', 'image2pipe', '-c:v', 'mjpeg', '-'])
    x, y = (k % C) * TW, (k // C) * (TH + 4)
    sheet.paste(Image.open(io.BytesIO(data)), (x, y))
    d.text((x + 6, y + 4), f'{t:.2f}', fill='yellow', font=f, stroke_width=2, stroke_fill='black')
p = out / f'{name.replace(" ", "_")}_{a:g}-{b:g}_s{step:g}.jpg'
sheet.save(p, quality=82)
print(p)
