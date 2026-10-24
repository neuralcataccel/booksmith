"""Fetch the pinned upstream model files; no book text leaves this machine."""
import hashlib
import urllib.request

from settings import CONFIG, MODELS, prepare_dirs

prepare_dirs()
base = 'https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/'
files = {'kokoro-v1.0.onnx': CONFIG['model_sha256'], 'voices-v1.0.bin': CONFIG['voices_sha256']}
for name, expected in files.items():
    destination = MODELS / name
    if not destination.exists():
        partial = destination.with_suffix('.download')
        urllib.request.urlretrieve(base + name, partial)
        with partial.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != expected:
            raise RuntimeError(f'Model checksum mismatch: {name}')
        partial.replace(destination)
    with destination.open('rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    if actual != expected:
        raise RuntimeError(f'Existing model checksum mismatch: {name}')
    print(f'Verified {name} ({destination.stat().st_size:,} bytes)')
