"""Download the pinned public Qwen audition model and verify every LFS SHA-256."""
import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path
import time
import urllib.parse
import urllib.request

REPO = 'Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice'
REVISION = '0c0e3051f131929182e2c023b9537f8b1c68adfe'


def main(args):
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    url = f'https://huggingface.co/api/models/{REPO}/revision/{REVISION}?blobs=true'
    with urllib.request.urlopen(url, timeout=30) as response:
        info = json.load(response)
    if info['sha'] != REVISION:
        raise ValueError('Unexpected model revision')

    def fetch(row):
        name = row['rfilename']
        dest = root / name
        if not dest.resolve().is_relative_to(root):
            raise ValueError('Model filename escapes the output directory')
        expected = row.get('lfs', {}).get('sha256')
        size = row.get('size')
        if dest.exists() and dest.stat().st_size == size:
            if expected:
                with dest.open('rb') as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                        raise ValueError('Existing hash mismatch: ' + name)
            return name
        dest.parent.mkdir(parents=True, exist_ok=True)
        temporary = dest.with_name(dest.name + '.download')
        encoded = urllib.parse.quote(name, safe='/')
        url = f'https://huggingface.co/{REPO}/resolve/{REVISION}/{encoded}?download=true&booksmith={int(time.time())}'
        digest = hashlib.sha256()
        count = 0
        with urllib.request.urlopen(url, timeout=90) as response, temporary.open('wb') as output:
            while block := response.read(4 * 1024 * 1024):
                output.write(block)
                digest.update(block)
                count += len(block)
        if size is not None and count != size:
            raise ValueError('Download size mismatch: ' + name)
        if expected and digest.hexdigest() != expected:
            raise ValueError('Download hash mismatch: ' + name)
        temporary.replace(dest)
        print('Verified', name, count, flush=True)
        return name

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(fetch, info['siblings']))
    manifest = {'repo': REPO, 'revision': REVISION,
                'license': info.get('cardData', {}).get('license'), 'files': info['siblings']}
    temporary = root / 'booksmith-model-source.tmp'
    temporary.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    temporary.replace(root / 'booksmith-model-source.json')
    print('Pinned model download verified.', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path,
                        default=Path(r'C:\Booksmith\models\qwen3-1.7b-customvoice'))
    main(parser.parse_args())
