"""Private resident Kokoro CUDA renderer. Receives phones, never opens ebooks or Plex."""
import base64
import gc
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.metadata
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import threading
import time

import numpy as np
import torch
from kokoro import KModel
from loguru import logger

from speech_engines import native_descriptor, NATIVE_CONFIG_SHA, NATIVE_MODEL_SHA

RATE = 24000


class Busy(Exception):
    pass


class Renderer:
    def __init__(self, config):
        self.token = config['token']
        self.engine = native_descriptor(config['frontend_model_sha256'], config['voices_sha256'])
        self.config_path = Path(config['model_config'])
        self.model_path = Path(config['model_path'])
        self.voices_path = Path(config['voices_path'])
        for path, expected in [(self.config_path, NATIVE_CONFIG_SHA), (self.model_path, NATIVE_MODEL_SHA),
                               (self.voices_path, config['voices_sha256'])]:
            with path.open('rb') as stream:
                checksum = hashlib.sha256()
                for part in iter(lambda: stream.read(1024 * 1024), b''): checksum.update(part)
            if checksum.hexdigest() != expected: raise ValueError('Renderer artifact checksum mismatch.')
        if torch.__version__ != self.engine['torch'] or torch.version.cuda != self.engine['cuda']:
            raise ValueError('This image does not match the pinned Torch/CUDA runtime.')
        if importlib.metadata.version('kokoro') != '0.9.4': raise ValueError('Kokoro version changed.')
        if not torch.cuda.is_available(): raise ValueError('CUDA is unavailable.')
        torch.set_num_threads(4); torch.set_num_interop_threads(1)
        torch.cuda.set_per_process_memory_fraction(.4)
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True
        torch.backends.cudnn.benchmark = False
        self.lock = threading.RLock()
        self.model = None
        self.voices = np.load(self.voices_path, allow_pickle=False)
        self.last_used = time.monotonic()
        self.idle_seconds = config.get('idle_seconds', 120)
        self.completed = 0
        self.render_seconds = 0.0
        self.memory_at = 0.0
        self.memory_free = 0.0
        threading.Thread(target=self.reaper, daemon=True).start()

    def unload(self):
        self.model = None
        gc.collect(); torch.cuda.empty_cache()

    def reaper(self):
        while True:
            time.sleep(10)
            with self.lock:
                if self.model is not None and time.monotonic() - self.last_used > self.idle_seconds:
                    self.unload()

    def health(self):
        with self.lock:
            free = self.free_mib()
            return {'engine': self.engine, 'loaded': self.model is not None, 'free_mib': free,
                    'available': free >= (1400 if self.model is not None else 5000),
                    'completed_requests': self.completed, 'render_seconds': self.render_seconds}

    def free_mib(self):
        # WSL's cudaMemGetInfo excludes some Windows allocations. NVML/nvidia-smi
        # observes the physical shared-GPU usage measured by the benchmark.
        if time.monotonic()-self.memory_at>1:
            try:
                result=subprocess.run(['nvidia-smi','--query-gpu=memory.free','--format=csv,noheader,nounits'],
                                      capture_output=True,text=True,timeout=3,check=True)
                self.memory_free=float(result.stdout.strip().splitlines()[0])
            except (OSError,ValueError,subprocess.SubprocessError):self.memory_free=0
            self.memory_at=time.monotonic()
        return min(self.memory_free,torch.cuda.mem_get_info()[0]/2**20)

    def render(self, request):
        if request.get('engine') != self.engine: raise ValueError('Renderer identity mismatch.')
        voice = request.get('voice')
        batches = request.get('batches')
        speed = request.get('speed')
        if voice not in self.voices.files or not isinstance(batches, list) or not 1 <= len(batches) <= 16:
            raise ValueError('Invalid voice or batch count.')
        if not isinstance(speed, (int, float)) or not math.isfinite(speed) or not .5 <= speed <= 2:
            raise ValueError('Invalid speed.')
        with self.lock:
            if not self.health()['available']: raise Busy('Waiting for GPU memory.')
            if self.model is None:
                try:
                    self.model = KModel(repo_id='hexgrad/Kokoro-82M', config=str(self.config_path),
                                        model=str(self.model_path)).eval().to('cuda')
                except torch.cuda.OutOfMemoryError:
                    self.unload();raise Busy('Waiting for GPU memory.') from None
            vocab_sha = hashlib.sha256(json.dumps(self.model.vocab, sort_keys=True).encode()).hexdigest()
            if request.get('vocab_sha256') != vocab_sha: raise ValueError('Frontend vocabulary mismatch.')
            for phones in batches:
                if not isinstance(phones, str) or not 1 <= len(phones) <= 510 or not all(p in self.model.vocab for p in phones):
                    raise ValueError('Invalid or unsupported phoneme batch.')
            rows=[]; start=time.perf_counter()
            try:
                for phones in batches:
                    if self.free_mib() < 1400: raise Busy('Waiting for GPU memory.')
                    style=torch.from_numpy(self.voices[voice][len(phones)-1]).to('cuda')
                    with torch.inference_mode():
                        output=self.model(phones, style, speed=speed, return_output=True)
                        audio=output.audio.float().numpy()
                        duration=output.pred_dur.numpy().ravel()
                    if not np.isfinite(audio).all() or not RATE*.1 <= len(audio) <= RATE*120:
                        raise ValueError('Invalid native model output.')
                    rows.append({'audio':base64.b64encode(audio.astype('<f4',copy=False).tobytes()).decode(),
                                 'duration':duration.tolist()})
            except (torch.cuda.OutOfMemoryError, Busy):
                self.unload()
                raise Busy('Waiting for GPU resources. This edition keeps its GPU renderer.') from None
            finally:
                self.last_used=time.monotonic()
            self.completed+=1; self.render_seconds+=time.perf_counter()-start
            return {'engine':self.engine,'sample_rate':RATE,'rows':rows}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass  # Do not log book phonemes or authorization headers.

    def reply(self, status, payload):
        body=json.dumps(payload,allow_nan=False).encode()
        self.send_response(status); self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)

    def authorized(self):
        return hmac.compare_digest(self.headers.get('Authorization',''), 'Bearer '+self.server.renderer.token)

    def do_GET(self):
        if not self.authorized(): return self.reply(401,{'error':'Authentication required.'})
        if self.path != '/health': return self.reply(404,{})
        self.reply(200,self.server.renderer.health())

    def do_POST(self):
        if not self.authorized(): return self.reply(401,{'error':'Authentication required.'})
        if self.path != '/render': return self.reply(404,{})
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0 < length <= 131072: return self.reply(413,{'error':'Request too large.'})
            result=self.server.renderer.render(json.loads(self.rfile.read(length)))
            self.reply(200,result)
        except Busy:
            self.reply(503,{'error':'GPU resources are busy; retry this same renderer.'})
        except (ValueError,KeyError,TypeError):
            self.reply(422,{'error':'Invalid renderer input or identity.'})
        except Exception:
            self.reply(500,{'error':'Native rendering failed.'})


if __name__=='__main__':
    logger.remove()
    config=json.loads(Path(os.environ.get('BOOKSMITH_NATIVE_CONFIG','/private/server.json')).read_text())
    renderer=Renderer(config)
    server=ThreadingHTTPServer(('0.0.0.0',8790),Handler)
    server.renderer=renderer
    def shutdown(*_):
        threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,shutdown)
    signal.signal(signal.SIGINT,shutdown)
    print('Booksmith native CUDA service ready.',flush=True)
    try:server.serve_forever()
    finally:server.server_close()
