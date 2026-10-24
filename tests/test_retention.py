import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

import retention
import store


@pytest.fixture
def delivered(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DB_PATH', tmp_path / 'jobs.sqlite')
    monkeypatch.setattr(store, 'prepare_dirs', lambda: None)
    store.init()
    work = tmp_path / 'work'
    work.mkdir()
    monkeypatch.setattr(retention, 'WORK', work)
    monkeypatch.setattr(retention, 'CONFIG', {'output_root': str(tmp_path / 'final'), 'intermediate_retention_hours': 0})
    j = store.create_job('book', {'id': 1, 'title': 'Novel', 'author': 'Author'}, {}, 'edition')
    root = work / j['id']
    (root / 'chapters').mkdir(parents=True)
    (root / 'chapter-0001').mkdir()
    (root / 'chapter-0001' / '00000.wav').write_bytes(b'cached audio')
    (root / 'chapters' / '001.mp3').write_bytes(b'final audio')
    (root / 'timings.json').write_text('{}')
    (root / 'reading-plan.json').write_text('{"schema":1}')
    (root / 'audio-v5' / 'chapter-0001').mkdir(parents=True)
    (root / 'audio-v5' / 'chapter-0001' / '00000.wav').write_bytes(b'new audio')
    (root / 'audio-v6' / 'chapter-0001').mkdir(parents=True)
    (root / 'audio-v6' / 'chapter-0001' / '00000.wav').write_bytes(b'newer audio')
    output = tmp_path / 'final' / 'Author' / 'Novel'
    output.mkdir(parents=True)
    (output / '001.mp3').write_bytes(b'final audio')
    manifest = {'job_id': j['id'], 'edition_key': 'edition', 'relative_folder': 'Author/Novel',
                'tracks': [{'file': '001.mp3', 'sha256': hashlib.sha256(b'final audio').hexdigest(), 'bytes': 11}]}
    (output / 'booksmith.json').write_text(json.dumps(manifest))
    store.update_job(j['id'], status='ready', output_path=str(output), plex_key='7')
    monkeypatch.setattr(retention.plex_delivery, 'indexed_tracks', lambda folder: [('1', '7', str(Path(folder) / '001.mp3'))])
    return store.get_job(j['id']), root, output, manifest


def test_prune_keeps_final_audio_metadata_preview_and_deduplication(delivered):
    job, root, output, _ = delivered
    sibling = root.parent / 'another-job'
    sibling.mkdir()
    (sibling / 'preview.mp3').write_bytes(b'preview')
    assert retention.prune(job['id']) == len(b'cached audiofinal audionew audionewer audio')
    assert not (root / 'chapters').exists() and not (root / 'chapter-0001').exists()
    assert not (root / 'audio-v5').exists()
    assert not (root / 'audio-v6').exists()
    assert (output / '001.mp3').read_bytes() == b'final audio'
    assert (root / 'timings.json').exists() and (sibling / 'preview.mp3').exists()
    assert (root / 'reading-plan.json').exists()
    assert json.loads((root / 'cleanup.json').read_text())['state'] == 'complete'
    assert retention.prune(job['id']) == 0
    store.recover()
    assert store.create_job('book', {'id': 1, 'title': 'Novel', 'author': 'Author'}, {}, 'edition')['id'] == job['id']
    assert store.get_job(job['id'])['status'] == 'ready'


@pytest.mark.parametrize('status', ['queued', 'running', 'delivering', 'awaiting_plex', 'failed', 'cancelled'])
def test_never_prunes_unfinished_or_retryable_job(delivered, status):
    job, root, _, _ = delivered
    store.update_job(job['id'], status=status)
    assert retention.prune(job['id']) == 0
    assert (root / 'chapter-0001' / '00000.wav').exists()


def test_preview_and_retention_delay_are_respected(delivered, monkeypatch):
    job, root, _, _ = delivered
    retention.CONFIG['intermediate_retention_hours'] = 24
    assert retention.prune(job['id']) == 0
    retention.CONFIG['intermediate_retention_hours'] = 0
    with store.db() as con:
        con.execute("update jobs set kind='preview' where id=?", (job['id'],))
    assert retention.prune(job['id']) == 0
    assert (root / 'chapters').exists()


@pytest.mark.parametrize('problem', ['corrupt', 'missing', 'wrong_album', 'not_indexed', 'offline', 'wrong_manifest', 'path_escape'])
def test_delivery_failure_keeps_every_intermediate(delivered, monkeypatch, problem):
    job, root, output, manifest = delivered
    if problem == 'corrupt':
        (output / '001.mp3').write_bytes(b'corrupted')
    elif problem == 'missing':
        (output / '001.mp3').unlink()
    elif problem in {'wrong_album', 'not_indexed'}:
        monkeypatch.setattr(retention.plex_delivery, 'indexed_tracks', lambda folder: [('1', 'wrong', str(Path(folder) / '001.mp3'))] if problem == 'wrong_album' else [])
    elif problem == 'offline':
        monkeypatch.setattr(retention.plex_delivery, 'indexed_tracks', lambda folder: (_ for _ in ()).throw(ConnectionError('offline')))
    else:
        manifest['edition_key' if problem == 'wrong_manifest' else 'relative_folder'] = 'wrong' if problem == 'wrong_manifest' else '../elsewhere'
        (output / 'booksmith.json').write_text(json.dumps(manifest))
    with pytest.raises((ValueError, FileNotFoundError, ConnectionError)):
        retention.prune(job['id'])
    assert (root / 'chapters' / '001.mp3').exists()
    assert (root / 'chapter-0001' / '00000.wav').exists()
    assert store.get_job(job['id'])['status'] == 'ready'


def test_interrupted_legacy_cleanup_resumes_without_local_mp3(delivered, monkeypatch):
    job, root, output, manifest = delivered
    del manifest['tracks'][0]['sha256']
    (output / 'booksmith.json').write_text(json.dumps(manifest))
    original = Path.unlink
    def interrupted(path, *args, **kwargs):
        if path.suffix == '.wav':
            raise OSError('simulated interruption after MP3 deletion')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', interrupted)
    with pytest.raises(OSError):
        retention.prune(job['id'])
    assert not (root / 'chapters' / '001.mp3').exists()
    assert json.loads((root / 'cleanup.json').read_text())['state'] == 'verified'
    monkeypatch.setattr(Path, 'unlink', original)
    assert retention.prune(job['id']) > 0
    assert (output / '001.mp3').read_bytes() == b'final audio'


def test_junction_cannot_make_cleanup_delete_outside_workspace(delivered, tmp_path):
    job, root, _, _ = delivered
    outside = tmp_path / 'outside'
    outside.mkdir()
    valuable = outside / 'keep.wav'
    valuable.write_bytes(b'keep')
    junction = root / 'chapter-0002'
    if os.name == 'nt':
        # Directory junctions do not require Windows developer-mode symlink privileges.
        result = subprocess.run(['powershell', '-NoProfile', '-Command',
            '$ErrorActionPreference="Stop"; New-Item -ItemType Junction -Path $env:BOOKSMITH_TEST_JUNCTION -Target $env:BOOKSMITH_TEST_OUTSIDE | Out-Null'],
            env={**os.environ, 'BOOKSMITH_TEST_JUNCTION': str(junction), 'BOOKSMITH_TEST_OUTSIDE': str(outside)}, capture_output=True)
        assert result.returncode == 0, result.stderr.decode(errors='replace')
    else:
        junction.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(ValueError, match='links and junctions'):
            retention.prune(job['id'])
        assert valuable.read_bytes() == b'keep'
        assert (root / 'chapters' / '001.mp3').exists()
    finally:
        if os.name == 'nt':
            junction.rmdir()
        else:
            junction.unlink()
