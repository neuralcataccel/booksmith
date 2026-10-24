import json
import hashlib
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest
import soundfile as sf
from ebooklib import epub

import worker
import store
from extract import EXTRACTOR_VERSION, read_epub


def make_book(tmp_path, documents, toc):
    book = epub.EpubBook()
    book.set_identifier('chapter-fixture')
    book.set_title('Original test prose')
    book.set_language('en')
    items = []
    for name, content in documents:
        item = epub.EpubHtml(title=name, file_name=name)
        item.content = content
        book.add_item(item)
        items.append(item)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = items
    book.toc = [epub.Link(href, title, str(index)) for index, (href, title) in enumerate(toc)]
    path = tmp_path / 'fixture.epub'
    epub.write_epub(str(path), book)
    return path


def test_chapter_continues_across_files_and_toc_beats_in_story_heading(tmp_path):
    path = make_book(tmp_path, [
        ('first.xhtml', '<p>First chapter opening.</p>'),
        ('continuation.xhtml', '<h2>A NEWSPAPER HEADLINE</h2><p>First chapter ending.</p>'),
        ('second.xhtml', '<h2>ANOTHER HEADLINE</h2><p>Second chapter.</p>')],
        [('first.xhtml', 'Chapter 1'), ('second.xhtml', 'Chapter 2')])
    chapters = read_epub(path)
    assert [c['title'] for c in chapters] == ['Chapter 1', 'Chapter 2']
    assert chapters[0]['chunks'] == ['First chapter opening.', 'A NEWSPAPER HEADLINE', 'First chapter ending.']
    assert chapters[1]['chunks'] == ['ANOTHER HEADLINE', 'Second chapter.']


def test_multiple_chapters_in_one_document_follow_fragment_targets(tmp_path):
    path = make_book(tmp_path, [('all.xhtml',
        '<h1 id="one">One</h1><p>First dialogue.</p><a id="two"></a><h1>Two</h1><p>Second dialogue.</p>')],
        [('all.xhtml#one', 'Chapter 1'), ('all.xhtml#two', 'Chapter 2')])
    chapters = read_epub(path)
    assert [c['title'] for c in chapters] == ['Chapter 1', 'Chapter 2']
    assert [c['chunks'] for c in chapters] == [['One', 'First dialogue.'], ['Two', 'Second dialogue.']]


def test_nested_anchor_does_not_duplicate_or_drop_paragraph(tmp_path):
    path = make_book(tmp_path, [('all.xhtml', '<p id="one">One.</p><p><span id="two"></span>Two.</p>')],
                     [('all.xhtml#one', 'First'), ('all.xhtml#two', 'Second')])
    assert [c['chunks'] for c in read_epub(path)] == [['One.'], ['Two.']]


def test_no_toc_uses_clear_chapter_headings_and_keeps_continuations(tmp_path):
    path = make_book(tmp_path, [('one.xhtml', '<h1>Chapter I</h1><p>One.</p>'),
        ('extra.xhtml', '<p>Continued.</p>'), ('two.xhtml', '<h1>Chapter II</h1><p>Two.</p>')], [])
    chapters = read_epub(path)
    assert [c['title'] for c in chapters] == ['Chapter I', 'Chapter II']
    assert 'Continued.' in chapters[0]['chunks']


def test_legacy_cache_matches_exact_text_instead_of_chapter_position(tmp_path, monkeypatch):
    old_dir = tmp_path / 'chapter-0002'
    old_dir.mkdir()
    wav = old_dir / '00000.wav'
    sf.write(wav, np.ones(2400, dtype=np.float32) * .1, 24000)
    (tmp_path / 'timings.json').write_text(json.dumps({'2/0': .4}))
    monkeypatch.setattr(worker, 'extract_book', lambda *a, **kw: {'chapters': [
        {'chunks': ['Not yet generated.']}, {'chunks': ['Exact preserved dialogue.']}]})
    cached = worker.legacy_chunks(tmp_path, tmp_path / 'source.epub', {})
    assert list(cached) == ['Exact preserved dialogue.']
    assert cached['Exact preserved dialogue.'] == (wav, .4)
    assert worker.read_duration(wav) == pytest.approx(.1)


@pytest.mark.parametrize('cache_version', ['4', '5', '6'])
def test_worker_reuses_only_matching_legacy_audio_after_chapter_regrouping(tmp_path, monkeypatch, cache_version):
    monkeypatch.setattr(store, 'DB_PATH', tmp_path / 'jobs.sqlite')
    monkeypatch.setattr(store, 'prepare_dirs', lambda: None)
    store.init()
    work = tmp_path / 'work'
    work.mkdir()
    monkeypatch.setattr(worker, 'WORK', work)
    monkeypatch.setattr(worker, 'CONFIG', {**worker.CONFIG, 'output_root': str(tmp_path / 'final'), 'staging_root': str(tmp_path / 'stage')})
    source = tmp_path / 'source.txt'
    source.write_text('Later phrase. Already generated.')
    options = {'voice':'af_heart', 'style':'natural', 'pace':1.0, 'skip_notes':True, 'skip_frontmatter':True,
               'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest()}
    job = store.create_job('book', {'id':1,'title':'Test','author':'Author'}, options, 'legacy-key')
    job = store.claim_next()
    root = work / job['id']
    previous = root if cache_version == '4' else root / f'audio-v{cache_version}'
    (previous / 'chapter-0001').mkdir(parents=True)
    old = previous / 'chapter-0001' / '00000.wav'
    sf.write(old, np.ones(2400, dtype=np.float32) * .2, 24000)
    original_hash = hashlib.sha256(old.read_bytes()).hexdigest()
    (previous / 'timings.json').write_text(json.dumps({'1/0':.5}))
    monkeypatch.setattr(worker, 'extract_book', lambda *a, **kw: {
        'chapters':[{'title':'Chapter 1', 'chunks':['Already generated.'] if kw.get('version') in {'4','5','6'} else ['Later phrase.', 'Already generated.']}], 'characters':32})
    monkeypatch.setattr(worker.catalogue, 'source_file', lambda bid: source)
    monkeypatch.setattr(worker.catalogue, 'cover_path', lambda bid: None)
    monkeypatch.setattr(worker.catalogue, 'get', lambda *a, **kw: {'description':'An original test.'})
    def generate(text, options, path):
        sf.write(path, np.ones(4800, dtype=np.float32) * .1, 24000)
        return .2, .3
    generator = Mock(side_effect=generate)
    monkeypatch.setattr(worker.speech, 'generate', generator)
    monkeypatch.setattr(worker, 'service_preview', lambda: None)
    monkeypatch.setattr(worker, 'tag_audio', lambda *args: None)
    monkeypatch.setattr(worker, 'ffmpeg', lambda args: Path(args[-1]).write_bytes(b'encoded test'))
    monkeypatch.setattr(worker.plex_delivery, 'scan', lambda relative: None)
    worker.STOP.clear()
    worker.process_book(job)
    assert generator.call_count == 1 and generator.call_args.args[0] == 'Later phrase.'
    reused = root / f'audio-v{EXTRACTOR_VERSION}' / 'chapter-0001' / '00001.wav'
    assert hashlib.sha256(reused.read_bytes()).hexdigest() == original_hash
    assert hashlib.sha256(old.read_bytes()).hexdigest() == original_hash
    finished = store.get_job(job['id'])
    assert finished['status'] == 'awaiting_plex' and finished['done_chunks'] == 2
    manifest = json.loads((Path(finished['output_path']) / 'booksmith.json').read_text())
    assert manifest['extraction_version'] == EXTRACTOR_VERSION
    assert manifest['tracks'][0]['sha256'] == hashlib.sha256(b'encoded test').hexdigest()
    plan = json.loads((root / 'reading-plan.json').read_text(encoding='utf-8'))
    index = json.loads((root / 'render-index.json').read_text(encoding='utf-8'))
    assert index == manifest['render_index']
    assert index['plan_sha256'] == plan['sha256'] == manifest['reading_plan']['sha256']
    assert [c['start_seconds'] for c in index['chapters'][0]['chunks']] == pytest.approx([0, .2])


@pytest.mark.parametrize('problem', ['corruption', 'missing', 'unexpected'])
def test_nas_copy_validation_blocks_corruption_and_stale_tracks(tmp_path, problem):
    audio = tmp_path / '001.mp3'
    audio.write_bytes(b'valid audio')
    (tmp_path / 'booksmith.json').write_text('{}')
    tracks = [{'file':'001.mp3', 'bytes':11, 'sha256':hashlib.sha256(b'valid audio').hexdigest()}]
    if problem == 'corruption':
        audio.write_bytes(b'wrong audio')
    elif problem == 'missing':
        audio.unlink()
    else:
        (tmp_path / 'old-chapter.mp3').write_bytes(b'left over')
    with pytest.raises(RuntimeError):
        worker.verify_staging(tmp_path, tracks, False)
    assert (tmp_path / 'booksmith.json').exists()


@pytest.mark.parametrize('toc', [[], [('one.xhtml', 'Only navigation entry')]])
def test_sparse_navigation_preserves_document_boundaries(tmp_path, toc):
    path = make_book(tmp_path, [('one.xhtml', '<h1>Beginning</h1><p>One.</p>'),
        ('two.xhtml', '<h1>The next adventure</h1><p>Two.</p>'),
        ('three.xhtml', '<p>Three.</p>')], toc)
    chapters = read_epub(path)
    assert len(chapters) == 3
    assert [t for c in chapters for t in c['chunks']] == ['Beginning', 'One.', 'The next adventure', 'Two.', 'Three.']


def test_version_five_remains_reproducible_for_safe_cache_migration(tmp_path):
    path = make_book(tmp_path, [('one.xhtml', '<p>One.</p>'), ('two.xhtml', '<p>Two.</p>')], [])
    assert len(read_epub(path)) == 2
    old = read_epub(path, document_fallback=False)
    assert len(old) == 1 and old[0]['chunks'] == ['One.', 'Two.']


def test_empty_block_elements_do_not_mask_real_body_prose(tmp_path):
    path=make_book(tmp_path,[('one.xhtml','<p></p><div>Actual story prose.</div>')],[('one.xhtml','Chapter 1')])
    chapters=read_epub(path,include_structure=True)
    assert chapters[0]['chunks']==['Actual story prose.']
    assert chapters[0]['blocks'][0]['kind']=='body'
    with pytest.raises(ValueError,match='No readable text'):
        read_epub(path,body_fallback=False)
