"""Smooth the orbit recordings the game drew at a low frame rate (York is a heavy scene: about 4
real frames a second in a 60 fps capture). Only the window a shot uses is processed: repeated
frames are dropped (mpdecimate), the motion between the real frames is interpolated back to
60 fps (minterpolate, motion compensated), and the window is padded back into a 2560x1440
frame so every crop keeps its source coordinates.

python project/smooth_orbits.py [name ...]      -> media/derived/<name>.mkv
"""
import subprocess, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

R = Path(__file__).resolve().parent
V = R.parent / 'media/video'
OUT = R.parent / 'media/derived'
OUT.mkdir(parents=True, exist_ok=True)
# name: (source, start, duration, crop x, y, w, h) in source pixels and seconds
JOBS = {
    'yorkbridge-smooth': ('2026-10-21 22-37-13', 17.8, 4.8, 920, 240, 760, 1166),   # about 4 fps as recorded
    'nordheim-smooth': ('nordheim', 17.3, 4.2, 1040, 240, 760, 1166),              # about 9 fps
    'aphelion-smooth': ('aphelionorbit', 32.8, 5.0, 1100, 100, 700, 1100),         # about 14 fps
    'fortredleaf-smooth': ('fortredleaf', 31.3, 11.2, 240, 100, 1580, 1306),       # about 18 fps; both uses
}


def run(name):
    src, a, d, x, y, w, h = JOBS[name]
    vf = (f'crop={w}:{h}:{x}:{y},mpdecimate=hi=768:lo=320:frac=0.5:max=0,'
          f'minterpolate=fps=60:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1,'
          f'pad=2560:1440:{x}:{y}')
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', f'{a}', '-t', f'{d}', '-i', str(V / f'{src}.mkv'), '-an',
                    '-vf', vf, '-c:v', 'libx264', '-crf', '12', '-preset', 'fast', str(OUT / f'{name}.mkv')], check=True)
    print('done', name, flush=True)


if __name__ == '__main__':
    names = sys.argv[1:] or list(JOBS)
    with ThreadPoolExecutor(len(names)) as ex:
        list(ex.map(run, names))
