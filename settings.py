import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads(Path(os.environ.get('BOOKSMITH_CONFIG', ROOT / 'config.json')).read_text())
RUNTIME = Path(CONFIG['runtime_root'])
DB_PATH = RUNTIME / 'data' / 'booksmith.sqlite'
WORK = RUNTIME / 'work'
MODELS = RUNTIME / 'models'
LIBRARY = Path(CONFIG['library_root'])
SUPPORTED = ('EPUB', 'MOBI', 'AZW3', 'TXT')


def prepare_dirs():
    for p in (RUNTIME / 'data', RUNTIME / 'logs', WORK, MODELS):
        p.mkdir(parents=True, exist_ok=True)


def contained(root, relative):
    root = Path(root).resolve()
    result = (root / relative).resolve()
    if not result.is_relative_to(root):
        raise ValueError('Invalid path outside the book library.')
    return result


def safe_name(text, limit=95):
    import re
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', text).strip(' .')[:limit].strip(' .')
    if not clean:
        return 'Untitled'
    if clean.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
        clean = '_' + clean
    return clean
