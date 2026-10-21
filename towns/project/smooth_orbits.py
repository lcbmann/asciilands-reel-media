"""Smooth the orbit recordings the game drew at a very low frame rate (York is a heavy scene: about
4 real frames a second in a 60 fps capture). Only the window a shot uses is processed: repeated
frames are dropped (mpdecimate) and one motion-compensated frame is made between each pair of
real ones (minterpolate obmc, kept light: full 60 fps interpolation smeared the pixel art). The
shot also plays faster. The window is padded back into a 2560x1440 frame so every crop keeps its
source coordinates.

python project/smooth_orbits.py [name ...]      -> media/derived/<name>.mkv
"""
import subprocess, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

R = Path(__file__).resolve().parent
V = R.parent / 'media/video'
OUT = R.parent / 'media/derived'
OUT.mkdir(parents=True, exist_ok=True)
JOBS = {
    # name: (source, start, duration, crop x, y, w, h, target fps in source time)
    # Light interpolation only: one generated frame between each pair of real frames.
    'yorkbridge-smooth': ('2026-10-21 22-37-13', 17.8, 6.4, 450, 240, 1700, 1166, 8),   # about 4 real fps
    'nordheim-smooth': ('nordheim', 17.3, 5.0, 500, 240, 1600, 1166, 18),               # about 9 real fps
}


def run(name):
    src, a, d, x, y, w, h, fps = JOBS[name]
    vf = (f'crop={w}:{h}:{x}:{y},mpdecimate=hi=768:lo=320:frac=0.5:max=0,'
          f'minterpolate=fps={fps}:mi_mode=mci:mc_mode=obmc:me_mode=bidir,fps=60,'
          f'pad=2560:1440:{x}:{y}')
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', f'{a}', '-t', f'{d}', '-i', str(V / f'{src}.mkv'), '-an',
                    '-vf', vf, '-c:v', 'libx264', '-crf', '12', '-preset', 'fast', str(OUT / f'{name}.mkv')], check=True)
    print('done', name, flush=True)


if __name__ == '__main__':
    names = sys.argv[1:] or list(JOBS)
    with ThreadPoolExecutor(len(names)) as ex:
        list(ex.map(run, names))
