"""Host adapter: existing phonemizer/timing pipeline, authenticated local CUDA inference."""
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import threading
import time

import requests

from settings import CONFIG, RUNTIME
from speech_engines import native_descriptor


class NativeUnavailable(RuntimeError):
    pass


_probe_lock=threading.Lock()
_probe_at=0.0
_probe_ok=False


def connection():
    path=Path(CONFIG.get('native_connection_file',RUNTIME/'data'/'native-speech'/'client.json'))
    data=json.loads(path.read_text(encoding='utf-8'))
    if data.get('url')!='http://127.0.0.1:18790': raise ValueError('Native renderer must use its dedicated loopback endpoint.')
    return data


def call(endpoint,payload=None):
    try:
        data=connection()
        with requests.Session() as session:
            session.trust_env=False
            response=session.request('GET' if payload is None else 'POST',data['url']+endpoint,
                headers={'Authorization':'Bearer '+data['token']},json=payload,
                timeout=(1,2) if payload is None else (2,60))
    except (OSError,requests.RequestException,ValueError,KeyError):
        raise NativeUnavailable('The local GPU renderer is unavailable; completed audio is retained.') from None
    if response.status_code==503:raise NativeUnavailable('Waiting for GPU resources; this edition keeps its GPU renderer.')
    if response.status_code==401 or response.status_code>=500:
        raise NativeUnavailable('The native renderer is unavailable or its identity changed.')
    if response.status_code!=200:raise ValueError('The native renderer rejected the phonemes or engine identity.')
    if len(response.content)>32*1024*1024:raise ValueError('Native response is too large.')
    return response.json()


def frigate_available():
    global _probe_at,_probe_ok
    with _probe_lock:
        if time.monotonic()-_probe_at<15:return _probe_ok
        try:
            result=subprocess.run(['docker','exec','frigate','python3','-c',
                "import json,urllib.request;d=json.load(urllib.request.urlopen('http://127.0.0.1:5000/api/stats',timeout=3));print(sum(float(c.get('skipped_fps',0)) for c in d.get('cameras',{}).values()))"],
                capture_output=True,text=True,timeout=6,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            _probe_ok=result.returncode==0 and float(result.stdout.strip())<=2
        except (OSError,ValueError,subprocess.TimeoutExpired):_probe_ok=False
        _probe_at=time.monotonic()
        return _probe_ok


def health():
    row=call('/health')
    expected=native_descriptor(CONFIG['model_sha256'],CONFIG['voices_sha256'])
    if row.get('engine')!=expected:raise NativeUnavailable('The native renderer does not match this deployment.')
    return row


def available():
    try:return health()['available'] and frigate_available()
    except (NativeUnavailable,KeyError):return False


def render(k,text,arguments,engine,return_timings=False):
    import numpy as np
    from kokoro_onnx.trim import trim
    from kokoro_onnx.sliding import token_edges,timings
    from kokoro_onnx.pauses import insert
    if not frigate_available():raise NativeUnavailable('Waiting for camera/GPU headroom; completed audio is retained.')
    if not k.has_timings:raise ValueError('Native speech requires the pinned duration-reporting frontend model.')
    _,_,batches=k._prepare(text,arguments['voice'],arguments['speed'],arguments['lang'],arguments.get('is_phonemes',False),.25,.1)
    payload={'engine':engine,'voice':arguments['voice'],'speed':arguments['speed'],
             'batches':[p for p,_ in batches],
             'vocab_sha256':hashlib.sha256(json.dumps(k.tokenizer.vocab,sort_keys=True).encode()).hexdigest()}
    result=call('/render',payload)
    if result.get('engine')!=engine or result.get('sample_rate')!=24000 or len(result.get('rows',[]))!=len(batches):
        raise ValueError('Native response identity or batch count changed.')
    parts=[];spoken=[];offset=0
    for (phones,pause),row in zip(batches,result['rows']):
        audio=np.frombuffer(base64.b64decode(row['audio'],validate=True),dtype='<f4').copy()
        duration=np.asarray(row['duration'],dtype=np.int64)
        if duration.size!=len(phones)+2 or (duration<0).any() or duration.sum()<=0:
            raise ValueError('Native timing output is invalid.')
        edges=token_edges(duration,len(audio))[1:]
        audio,(head,_)=trim(audio);edges=np.clip(edges-head,0,len(audio))
        if pause:audio=np.concatenate([audio,np.zeros(int(pause*24000),dtype=np.float32)])
        parts.append(audio);spoken+=timings(phones,edges+offset,24000);offset+=len(audio)
    audio,spoken=insert(np.concatenate(parts),spoken,24000,.25,.1)
    return (audio,24000,spoken) if return_timings else (audio,24000)
