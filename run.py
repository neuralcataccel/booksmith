"""Single-instance, private LAN service. Run with the project virtual environment."""
import logging
from logging.handlers import RotatingFileHandler
import msvcrt
import os
import sys

from settings import CONFIG, RUNTIME, prepare_dirs

prepare_dirs()
lock = (RUNTIME / 'data' / 'service.lock').open('a+b')
lock.seek(0)
try:
    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
except OSError:
    sys.exit(0)

os.environ.setdefault('OMP_NUM_THREADS', str(CONFIG.get('cpu_threads', 6)))
handler = RotatingFileHandler(RUNTIME / 'logs' / 'booksmith.log', maxBytes=5_000_000, backupCount=3, encoding='utf-8')
logging.basicConfig(level=logging.INFO, handlers=[handler], format='%(asctime)s %(levelname)s %(threadName)s %(message)s')
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('kokoro_onnx').setLevel(logging.WARNING)

if __name__ == '__main__':
    import uvicorn
    logging.info('Starting Booksmith at %s', CONFIG['public_url'])
    uvicorn.run('app:app', host=CONFIG['host'], port=CONFIG['port'], workers=1, log_config=None, access_log=False, use_colors=False, proxy_headers=False)
