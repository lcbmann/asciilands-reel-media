import json,subprocess,re,difflib
from pathlib import Path
import numpy as np
from faster_whisper import WhisperModel
R=Path(__file__).parent
model=WhisperModel('small.en',device='cpu',compute_type='int8',cpu_threads=6,download_root=str(R/'models'))
audio=np.frombuffer(subprocess.check_output(['ffmpeg','-v','error','-i',str(R/'edit/voice.wav'),'-ar','16000','-ac','1','-f','f32le','-']),dtype=np.float32)
segs,_=model.transcribe(audio,beam_size=5,initial_prompt='Asciilands. Rare materials. Star Iron. Player-run economy. Masterwork sword.')
actual=' '.join(s.text.strip() for s in segs)
expected=' '.join(w['w'] for w in json.loads((R/'words-raw.json').read_text())).replace('Ram materials','Rare materials').replace(' -','-')
norm=lambda s:re.findall(r'[a-z0-9]+',s.lower())
a,b=norm(expected),norm(actual);diff=[]
for tag,i,j,k,l in difflib.SequenceMatcher(None,a,b).get_opcodes():
 if tag!='equal':diff.append(dict(kind=tag,expected=a[i:j],heard=b[k:l]))
report=dict(transcript=actual,token_match=difflib.SequenceMatcher(None,a,b).ratio(),differences=diff)
(R/'edit/audio-word-check.json').write_text(json.dumps(report,indent=2));print(json.dumps(dict(token_match=report['token_match'],differences=diff)),flush=True)
