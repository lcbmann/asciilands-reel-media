"""Measure the towns-v1 stems: integrated loudness, RMS, peaks, per-shot game level under the voice.
python project/check_audio_towns.py  ->  edit-v1/audio-stems.json and edit-v1/qa/spectrograms.png"""
from pathlib import Path
import json, re, subprocess, os
import numpy as np
R = Path(__file__).resolve().parent
O = R / 'edit-v2'
SR = 48000


def read(p):
    """Stereo samples; levels below are over both channels (no downmix, which would sum them)."""
    return np.frombuffer(subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(p), '-ac', '2', '-ar', str(SR), '-f', 'f32le', '-']),
                         np.float32).reshape(-1, 2)


def lufs(p):
    r = subprocess.run(['ffmpeg', '-hide_banner', '-i', str(p), '-af', 'ebur128=peak=true', '-f', 'null', os.devnull], capture_output=True, text=True)
    i = re.findall(r'I:\s+(-?[\d.]+|-inf) LUFS', r.stderr)
    tp = re.findall(r'Peak:\s+(-?[\d.]+|-inf) dBFS', r.stderr)
    return float(i[-1]), float(tp[-1]) if tp else None


def db(x):
    return round(float(20 * np.log10(x + 1e-12)), 2)


stems = {k: O / 'audio' / f'{k}.wav' for k in ('voice', 'game', 'music', 'master')}
rep = {}
for k, p in stems.items():
    a = read(p)
    i, tp = lufs(p)
    rep[k] = dict(lufs=i, true_peak=tp, rms_db=db(np.sqrt((a ** 2).mean())), peak_db=db(np.abs(a).max()))
words = json.loads((O / 'words.json').read_text())
tl = json.loads((O / 'timeline.json').read_text())
v, g, m = read(stems['voice']), read(stems['game']), read(stems['music'])
v, g, m = v.mean(1), g.mean(1), m.mean(1)
speech = np.zeros(len(v), bool)
for w in words:
    speech[int(w['s'] * SR):int(w['e'] * SR)] = True
shots = []
for s in tl:
    a, b = int(s['start'] * SR), int(s['end'] * SR)
    gs = g[a:b]
    if np.abs(gs).max() < 1e-6:
        continue
    vs, sp = v[a:b], speech[a:b]
    shots.append(dict(shot=s['at'], game_rms_db=db(np.sqrt((gs ** 2).mean())),
                      game_under_voice_db=round(db(np.sqrt((vs[sp] ** 2).mean())) - db(np.sqrt((gs[sp] ** 2).mean())), 1) if sp.any() else None,
                      game_peak_vs_voice_peak_db=round(db(np.abs(gs).max()) - db(np.abs(vs).max()), 1)))
rep['shots'] = shots
k = int(0.4 * SR)
mr = [db(np.sqrt((m[i:i + k] ** 2).mean())) for i in range(0, len(m) - k, k)]
rep['music_rms_per_0.4s'] = dict(min=min(mr[:-3]), max=max(mr[:-3]), first=mr[0])
(O / 'audio-stems.json').write_text(json.dumps(rep, indent=2))
subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(stems['voice']), '-i', str(stems['game']), '-i', str(stems['music']), '-i', str(stems['master']),
                '-filter_complex', '[0]showspectrumpic=s=1600x200:legend=0:stop=12000[a];[1]showspectrumpic=s=1600x200:legend=0:stop=12000[b];'
                '[2]showspectrumpic=s=1600x200:legend=0:stop=12000[c];[3]showspectrumpic=s=1600x200:legend=0:stop=12000[d];[a][b][c][d]vstack=4',
                str(O / 'qa/spectrograms.png')], check=True)
print(json.dumps({k: rep[k] for k in ('voice', 'game', 'music', 'master', 'music_rms_per_0.4s')}, indent=1))
for s in shots:
    print(s)
