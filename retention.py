"""Prune only a ready book's disposable local audio after fresh delivery checks."""
import hashlib
import json
import logging
import re
import stat
import time
from pathlib import Path

import plex_delivery
import store
from settings import CONFIG, WORK, contained


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def reject_link(path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
        raise ValueError('Cleanup refuses links and junctions.')
    return info


def tree(path):
    """Preflight without following links; callers delete children before parents."""
    info = reject_link(path)
    if path.is_dir():
        entries = [entry for child in path.iterdir() for entry in tree(child)]
        return entries + [(path, True, 0)]
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('Cleanup refuses non-regular files.')
    return [(path, False, info.st_size)]


def save_receipt(path, value):
    temporary = path.with_suffix('.tmp')
    if temporary.exists() or temporary.is_symlink():
        reject_link(temporary)
    temporary.write_text(json.dumps(value, indent=2), encoding='utf-8')
    temporary.replace(path)


def prune(jid):
    # Re-read durable state rather than trust a stale worker snapshot.
    job = store.get_job(jid)
    if not job or job['kind'] != 'book' or job['status'] != 'ready' or not job['plex_key'] or job['cancel_requested']:
        return 0
    if not re.fullmatch(r'[0-9a-f]{16}', jid):
        raise ValueError('Invalid cleanup job identifier.')
    if time.time() - job['updated'] < float(CONFIG.get('intermediate_retention_hours', 0)) * 3600:
        return 0
    root = WORK / jid
    if not root.exists():
        return 0
    # Verify absolute local targets before any recursive traversal or deletion.
    for ancestor in [root, *root.parents]:
        reject_link(ancestor)
    if root.resolve().parent != WORK.resolve() or root.resolve().name != jid:
        raise ValueError('Cleanup target escaped the job workspace.')
    receipt_path = root / 'cleanup.json'
    prior = {}
    if receipt_path.exists():
        reject_link(receipt_path)
        prior = json.loads(receipt_path.read_text(encoding='utf-8'))
        if prior.get('job_id') != jid or prior.get('edition_key') != job['edition_key']:
            raise ValueError('Cleanup receipt belongs to another job.')
        if prior.get('state') == 'complete':
            return 0
    output = Path(job['output_path'])
    manifest = json.loads((output / 'booksmith.json').read_text(encoding='utf-8'))
    if manifest.get('job_id') != jid or manifest.get('edition_key') != job['edition_key']:
        raise ValueError('Final manifest belongs to another job.')
    relative = Path(manifest['relative_folder'])
    if relative.is_absolute() or '..' in relative.parts or output.resolve() != contained(CONFIG['output_root'], relative):
        raise ValueError('Final output is outside the expected generated folder.')
    expected = manifest.get('tracks', [])
    if not expected:
        raise ValueError('Cannot clean a book without final tracks.')
    folder = plex_delivery.plex_path(relative)
    rows = plex_delivery.indexed_tracks(folder)
    wanted = set()
    proof = {}
    # Rehash every final track before touching a single local intermediate.
    for track in expected:
        name = track['file']
        if not name or Path(name).name != name or '/' in name or '\\' in name or name in {'.', '..'} or name in proof:
            raise ValueError('Invalid final track filename.')
        local = root / 'chapters' / name
        checksum = track.get('sha256') or prior.get('tracks', {}).get(name)
        if not checksum:
            # Legacy output has no hash; require its intact local MP3 for comparison.
            reject_link(local.parent)
            reject_link(local)
            checksum = digest(local)
        if not re.fullmatch(r'[0-9a-f]{64}', checksum):
            raise ValueError('Invalid final track checksum.')
        final = output / name
        if digest(final) != checksum or (track.get('bytes') is not None and final.stat().st_size != track['bytes']):
            raise ValueError('Final audio failed its checksum; intermediates retained.')
        proof[name] = checksum
        wanted.add(plex_delivery.normalized(str(Path(folder) / name)))
    matching = [row for row in rows if plex_delivery.normalized(row[2]) in wanted]
    if not wanted.issubset({plex_delivery.normalized(row[2]) for row in matching}) or {str(row[1]) for row in matching} != {str(job['plex_key'])}:
        raise ValueError('Plex no longer contains every final track in the expected album.')
    targets = [root / 'chapters', *sorted(root.glob('chapter-[0-9][0-9][0-9][0-9]')),
               *sorted(child for child in root.iterdir() if re.fullmatch(r'audio-v\d+', child.name))]
    entries = [entry for target in targets if target.exists() for entry in tree(target)]
    # A verified receipt supports retries after interruption partway through deletion.
    receipt = {'schema': 1, 'job_id': jid, 'edition_key': job['edition_key'],
               'state': 'verified', 'verified': time.time(), 'tracks': proof,
               'bytes_removed': prior.get('bytes_removed', 0)}
    save_receipt(receipt_path, receipt)
    removed = 0
    for path, directory, size in entries:
        reject_link(path)
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('Cleanup target changed during deletion.')
        if directory:
            path.rmdir()
        else:
            path.unlink()
            removed += size
    receipt.update(state='complete', completed=time.time(), bytes_removed=receipt['bytes_removed'] + removed)
    save_receipt(receipt_path, receipt)
    logging.info('Pruned %s bytes of verified intermediates for %s', removed, jid)
    return removed


def sweep():
    with store.db() as con:
        ids = [r[0] for r in con.execute("SELECT id FROM jobs WHERE kind='book' AND status='ready'")]
    for jid in ids:
        try:
            prune(jid)
        except Exception as exc:
            # Cleanup failures must never downgrade a ready book or stop the worker.
            logging.warning('Intermediate cleanup deferred for %s: %s', jid, type(exc).__name__)
