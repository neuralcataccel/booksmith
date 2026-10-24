"""Isolated CPU/CUDA comparison; never changes Booksmith's provider or services.

Use a separate environment containing kokoro-onnx, soundfile and onnxruntime-gpu
with its matching CUDA/cuDNN extras. Do not install GPU dependencies into .venv.
Run this script with the app interpreter and --bench-python for telemetry mode.
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
import statistics
import subprocess
import time
from pathlib import Path

TEXTS = [
    'At the end of the garden, a small wooden gate opened onto the hills. The morning was quiet. She put the book in her pocket, took one last look at the house, and set off.',
    '“Are you certain?” Mara asked. “The clock stopped at midnight.” Daniel closed the door gently. “I am certain of one thing,” he said. “Someone was here before us.”',
    'On October 23, the station reported 42 arrivals and a balance of $19.50. Dr. Fenwick checked the figures twice. The next train would leave at 8:45 in the morning.',
    'She had expected a grand farewell, but the room was empty. For a moment she simply stood there, listening to the rain. Then she smiled, picked up the letter, and began to read.',
    'The corridor stretched ahead of them, quiet except for the steady hum of the ventilation system. Mara counted the doors as they passed, while Daniel checked each number against the map. Neither wanted to say what they were thinking: if the last room was empty too, they would have to begin the search again before sunrise. Time was running short.'
]


def write(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2), encoding='utf-8')
    temporary.replace(path)


def synthesize(provider, output, seconds, cuda_search, arena_mib):
    import numpy as np
    import onnxruntime as ort
    import soundfile as sf
    from kokoro_onnx import Kokoro
    config = json.loads((Path(__file__).parent / 'config.json').read_text())
    models = Path(config['runtime_root']) / 'models'
    output.mkdir(parents=True, exist_ok=True)
    for name, expected in [('kokoro-v1.0.onnx', config['model_sha256']), ('voices-v1.0.bin', config['voices_sha256'])]:
        with (models / name).open('rb') as stream:
            assert hashlib.file_digest(stream, 'sha256').hexdigest() == expected
    options = ort.SessionOptions()
    options.intra_op_num_threads = config.get('cpu_threads', 6)
    options.inter_op_num_threads = 1
    if provider == 'cuda':
        ort.preload_dlls(directory='')
        assert 'CUDAExecutionProvider' in ort.get_available_providers()
        providers = [('CUDAExecutionProvider', {
            'gpu_mem_limit': str(arena_mib * 1024 * 1024), 'arena_extend_strategy': 'kSameAsRequested',
            'cudnn_conv_algo_search': cuda_search, 'cudnn_conv_use_max_workspace': '0'}), 'CPUExecutionProvider']
    else:
        providers = ['CPUExecutionProvider']
    started = time.perf_counter()
    session = ort.InferenceSession(str(models / 'kokoro-v1.0.onnx'), sess_options=options, providers=providers)
    if provider == 'cuda':
        assert session.get_providers()[0] == 'CUDAExecutionProvider', 'CUDA initialization fell back to CPU'
        session.disable_fallback()
    engine = Kokoro.from_session(session, str(models / 'voices-v1.0.bin'))
    load_seconds = time.perf_counter() - started
    results = []
    start = time.perf_counter()
    while len(results) < 12 or time.perf_counter() - start < seconds:
        index = len(results) % len(TEXTS)
        begin = time.perf_counter()
        audio, rate = engine.create(TEXTS[index], voice='am_eric', speed=.9, lang='en-us')
        assert len(audio) > rate * .1 and np.isfinite(audio).all() and np.max(np.abs(audio)) > .0001
        audio = np.concatenate([audio, np.zeros(int(rate * .42), dtype=np.float32)])
        elapsed = time.perf_counter() - begin
        results.append({'sample': index, 'audio_seconds': len(audio) / rate, 'synthesis_seconds': elapsed})
        if len(results) <= len(TEXTS):
            sf.write(output / f'{provider}-{index}.wav', audio, rate, subtype='PCM_16')
    warm = results[len(TEXTS):]
    report = {'provider': provider, 'actual_providers': session.get_providers(), 'onnxruntime': ort.__version__,
              'kokoro_onnx': importlib.metadata.version('kokoro-onnx'), 'model_sha256': config['model_sha256'],
              'cpu_threads': options.intra_op_num_threads, 'voice': 'am_eric', 'speed': .9, 'chunk_pause': .42,
              'cuda_search': cuda_search, 'cuda_arena_limit_mib': arena_mib, 'cudnn_conv_use_max_workspace': False,
              'load_seconds': load_seconds, 'first_chunk_seconds': results[0]['synthesis_seconds'],
              'warm_audio_seconds': sum(x['audio_seconds'] for x in warm),
              'warm_synthesis_seconds': sum(x['synthesis_seconds'] for x in warm),
              'warm_median_chunk_seconds': statistics.median(x['synthesis_seconds'] for x in warm), 'runs': results}
    report['warm_audio_per_second'] = report['warm_audio_seconds'] / report['warm_synthesis_seconds']
    write(output / f'{provider}.json', report)
    print(json.dumps({k:report[k] for k in ('provider','actual_providers','load_seconds','warm_audio_per_second')}), flush=True)


def gpu_state():
    result = subprocess.run(['nvidia-smi', '--query-gpu=memory.used,memory.free,utilization.gpu', '--format=csv,noheader,nounits'],
                            capture_output=True, text=True, timeout=8, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    result.check_returncode()
    used, free, utilization = [float(x.strip()) for x in result.stdout.strip().splitlines()[0].split(',')]
    return {'used_mib': used, 'free_mib': free, 'utilization': utilization}


def frigate_state():
    code = ("import urllib.request,json; d=json.load(urllib.request.urlopen('http://127.0.0.1:5000/api/stats',timeout=3)); "
            "print(json.dumps({'inference_ms':{k:v.get('inference_speed') for k,v in d.get('detectors',{}).items()},"
            "'process_fps':sum(v.get('process_fps',0) for v in d.get('cameras',{}).values()),"
            "'skipped_fps':sum(v.get('skipped_fps',0) for v in d.get('cameras',{}).values()),'camera_count':len(d.get('cameras',{}))}))")
    result = subprocess.run(['docker', 'exec', 'frigate', 'python3', '-c', code], capture_output=True, text=True, timeout=8,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    result.check_returncode()
    return json.loads(result.stdout)


def orchestrate(bench_python, output, seconds, cuda_search, arena_mib):
    output.mkdir(parents=True, exist_ok=True)
    telemetry = []
    def observe(stage):
        import store
        row = {'time': time.time(), 'stage': stage, 'gpu': gpu_state(), 'frigate': frigate_state()}
        with store.db() as con:
            row['active_books'] = [dict(r) for r in con.execute("SELECT id,done_chunks,total_chunks,synthesis_seconds FROM jobs WHERE kind='book' AND status='running'")]
        telemetry.append(row)
        write(output / 'telemetry.json', telemetry)
        return row
    for _ in range(5):
        observe('baseline')
        time.sleep(2)
    for provider in ('cpu', 'cuda'):
        if provider == 'cuda' and gpu_state()['free_mib'] < max(2800, arena_mib + 1100):
            raise RuntimeError('GPU headroom is below the benchmark start threshold; no GPU process started.')
        with (output / f'{provider}.log').open('w', encoding='utf-8') as log:
            child = subprocess.Popen([bench_python, str(Path(__file__).resolve()), '--provider', provider,
                                      '--output', str(output), '--seconds', str(seconds), '--cuda-search', cuda_search,
                                      '--cuda-arena-mib', str(arena_mib)], stdout=log, stderr=log,
                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                                     env={**os.environ, 'OMP_NUM_THREADS':'6'})
            deadline = time.monotonic() + 300
            try:
                while child.poll() is None:
                    row = observe(provider)
                    if row['gpu']['free_mib'] < 768:
                        raise RuntimeError('GPU reserve fell below 768 MiB; stopping the benchmark.')
                    if row['frigate']['skipped_fps'] > 2:
                        raise RuntimeError('Frigate is skipping frames; stopping the benchmark.')
                    if time.monotonic() > deadline:
                        raise TimeoutError('Benchmark exceeded five minutes.')
                    time.sleep(1)
            finally:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=20)
            if child.returncode:
                raise RuntimeError(f'{provider} benchmark failed; see its isolated log.')
        print(f'{provider} benchmark complete', flush=True)
    for _ in range(5):
        observe('after')
        time.sleep(2)
    cpu = json.loads((output / 'cpu.json').read_text())
    cuda = json.loads((output / 'cuda.json').read_text())
    report = {'state':'complete', 'speedup':cuda['warm_audio_per_second']/cpu['warm_audio_per_second'],
              'cpu_audio_per_second':cpu['warm_audio_per_second'], 'cuda_audio_per_second':cuda['warm_audio_per_second'],
              'gpu_min_free_mib':min(x['gpu']['free_mib'] for x in telemetry),
              'frigate_max_skipped_fps':max(x['frigate']['skipped_fps'] for x in telemetry),
              'production_provider':'CPUExecutionProvider', 'cuda_search': cuda_search, 'cuda_arena_limit_mib': arena_mib,
              'limitations':['Short original prose under observed nighttime load; not a whole-book/daytime guarantee.',
                             'CUDA arena limit is not a limit on every allocation in the process.',
                             'Audio quality parity requires listening; no production provider change was made.']}
    write(output / 'report.json', report)
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', choices=['cpu','cuda'])
    parser.add_argument('--bench-python')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=30)
    parser.add_argument('--cuda-search', choices=['DEFAULT','HEURISTIC','EXHAUSTIVE'], default='HEURISTIC')
    parser.add_argument('--cuda-arena-mib', type=int, default=1800)
    args = parser.parse_args()
    if args.provider:
        synthesize(args.provider, args.output, args.seconds, args.cuda_search, args.cuda_arena_mib)
    elif args.bench_python:
        try:
            orchestrate(args.bench_python, args.output, args.seconds, args.cuda_search, args.cuda_arena_mib)
        except Exception as exc:
            write(args.output / 'report.json', {'state':'deferred_or_failed','reason':str(exc),'production_provider':'CPUExecutionProvider'})
            raise
    else:
        parser.error('Supply --provider or --bench-python.')
