import json
import re
from pathlib import Path
from unittest.mock import Mock

import pytest
from ebooklib import epub
from fastapi.testclient import TestClient

import catalogue
import plex_delivery
import store
import worker
from extract import html_blocks, read_epub, split_text
from settings import contained


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DB_PATH', tmp_path / 'jobs.sqlite')
    monkeypatch.setattr(store, 'prepare_dirs', lambda: None)
    store.init()
    # Authentication has independent tests; do not read production family credentials.
    import app
    from access import AccessControl
    monkeypatch.setattr(app, 'AUTH', AccessControl(settings={}))
    monkeypatch.setattr(catalogue,'sync',lambda *a,**kw:None)
    monkeypatch.setattr(worker,'start',lambda:None)
    monkeypatch.setattr(worker,'stop',lambda:None)
    return tmp_path


def test_splitter_keeps_every_word_and_punctuation():
    text = ('Dr. Smith waited. “Why now?” she asked.\n\nThe answer was 42. ' * 50).strip()
    parts = split_text(text)
    assert max(map(len, parts)) <= 360
    assert ' '.join(parts) == re.sub(r'\s+', ' ', text)


def test_notes_are_optional_without_losing_surrounding_text():
    html = '<html><body><p>First sentence.<a epub:type="noteref">1</a> Second sentence.</p><aside epub:type="footnote"><p>A note.</p></aside><p>Final paragraph.</p></body></html>'
    _, without = html_blocks(html, skip_notes=True)
    _, with_notes = html_blocks(html, skip_notes=False)
    assert without == ['First sentence. Second sentence.', 'Final paragraph.']
    assert any('A note.' in b for b in with_notes)


def test_epub_follows_spine_and_skips_only_identified_frontmatter(tmp_path):
    book = epub.EpubBook()
    book.set_identifier('test')
    book.set_title('Test Book')
    book.set_language('en')
    title = epub.EpubHtml(title='Title Page', file_name='front.xhtml')
    title.content = '<p>A Test Book</p>'
    second = epub.EpubHtml(title='Second', file_name='two.xhtml')
    second.content = '<h1>Second chapter</h1><p>This comes second.</p>'
    first = epub.EpubHtml(title='First', file_name='one.xhtml')
    first.content = '<h1>First chapter</h1><p>This comes first.</p>'
    for item in [title, second, first]:
        book.add_item(item)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.toc = [epub.Link('front.xhtml', 'Title Page', 'front'), epub.Link('one.xhtml', 'First chapter', 'first'), epub.Link('two.xhtml', 'Second chapter', 'second')]
    book.spine = [title, first, second]
    path = tmp_path / 'test.epub'
    epub.write_epub(str(path), book)
    chapters = read_epub(path)
    assert [c['title'] for c in chapters] == ['First chapter', 'Second chapter']
    assert len(read_epub(path, skip_frontmatter=False)) == 3


def test_catalog_cannot_escape_library(tmp_path, monkeypatch):
    monkeypatch.setattr(catalogue, 'LIBRARY', tmp_path)
    monkeypatch.setattr(catalogue, 'raw', lambda bid: {'path': '../other', 'formats': json.dumps([{'format':'EPUB','name':'secret'}])})
    with pytest.raises(ValueError, match='outside'):
        catalogue.source_file(1)
    with pytest.raises(ValueError):
        contained(tmp_path, tmp_path.parent / 'private.txt')


def test_queue_deduplicates_and_recovers_interrupted_work(isolated_store):
    b = {'id': 1, 'title': 'A book', 'author': 'An author'}
    a = store.create_job('book', b, {'voice':'af_heart'}, 'edition')
    same = store.create_job('book', b, {'voice':'af_heart'}, 'edition')
    assert a['id'] == same['id']
    running = store.claim_next()
    assert running['status'] == 'running'
    store.update_job(a['id'], done_chunks=5, total_chunks=12)
    store.recover()
    recovered = store.get_job(a['id'])
    assert recovered['status'] == 'queued'
    assert recovered['done_chunks'] == 5


def test_preview_gets_priority_between_books(isolated_store):
    b = {'id':1,'title':'Book','author':'Author'}
    store.create_job('book', b, {}, 'book-edition')
    preview = store.create_job('preview', b, {}, 'preview-edition')
    assert store.claim_next()['id'] == preview['id']


def test_running_extraction_upgrade_preserves_job_and_deduplicates(isolated_store):
    book = {'id':1,'title':'Book','author':'Author'}
    old = store.create_job('book', book, {}, 'old-plan')
    store.claim_next()
    store.rekey_running_job(old['id'], 'old-plan', 'new-plan')
    assert store.create_job('book', book, {}, 'new-plan')['id'] == old['id']
    with pytest.raises(ValueError, match='changed'):
        store.rekey_running_job(old['id'], 'old-plan', 'another-plan')


def test_extraction_upgrade_cannot_collide_with_another_edition(isolated_store):
    book = {'id':1,'title':'Book','author':'Author'}
    old = store.create_job('book', book, {}, 'old-plan')
    store.claim_next()
    store.create_job('book', book, {}, 'new-plan')
    with pytest.raises(ValueError, match='Another conversion'):
        store.rekey_running_job(old['id'], 'old-plan', 'new-plan')
    assert store.get_job(old['id'])['edition_key'] == 'old-plan'


def test_never_marks_ready_until_all_chapters_are_in_plex(isolated_store, monkeypatch):
    root = isolated_store / 'output'
    root.mkdir()
    manifest = {'relative_folder':'Author/Book','tracks':[{'file':'001.mp3'},{'file':'002.mp3'}],'album_title':'A Book','summary':'A summary'}
    (root / 'booksmith.json').write_text(json.dumps(manifest))
    j = store.create_job('book', {'id':1,'title':'Book','author':'Author'}, {}, 'edition')
    store.update_job(j['id'],status='awaiting_plex',output_path=str(root))
    monkeypatch.setattr(plex_delivery,'indexed_tracks',lambda folder, **kw:[(1,2,str(Path(folder)/'001.mp3'))])
    connect = Mock()
    monkeypatch.setattr(plex_delivery,'connect',connect)
    assert not plex_delivery.confirm_import(store.get_job(j['id']))
    assert store.get_job(j['id'])['status'] == 'awaiting_plex'
    connect.return_value.fetchItem.assert_not_called()
    folder = plex_delivery.plex_path(Path('Author/Book'))
    monkeypatch.setattr(plex_delivery,'indexed_tracks',lambda ignored, **kw:[(1,2,str(Path(folder)/'001.mp3')),(3,2,str(Path(folder)/'002.mp3'))])
    server = Mock(machineIdentifier='test-server')
    connect.return_value = server
    assert plex_delivery.confirm_import(store.get_job(j['id']))
    assert store.get_job(j['id'])['status'] == 'ready'
    server.fetchItem.return_value.editTitle.assert_called_once_with('A Book')


def test_web_rejects_cross_site_and_untrusted_hosts(isolated_store, monkeypatch):
    import app
    monkeypatch.setattr(catalogue,'sync',lambda *a,**kw: None)
    monkeypatch.setattr(worker,'start',lambda: None)
    monkeypatch.setattr(worker,'stop',lambda: None)
    with TestClient(app.app,base_url='http://localhost:8787',client=('127.0.0.1',50000)) as client:
        config = client.get('/api/config')
        assert config.status_code == 200
        csrf = config.json()['csrf']
        assert client.post('/api/previews',json={}).status_code == 403
        assert client.post('/api/previews',json={},headers={'X-Booksmith-Token':csrf,'Origin':'https://example.com'}).status_code == 403
        assert client.get('/api/config',headers={'Host':'evil.example'}).status_code == 400
        assert client.post('/api/previews',json={'voice':'imaginary'},headers={'X-Booksmith-Token':csrf}).status_code == 422
        assert client.post('/api/previews',json={'pace':9},headers={'X-Booksmith-Token':csrf}).status_code == 422


def test_web_rejects_non_lan_clients(isolated_store):
    import app
    with TestClient(app.app,base_url='http://localhost:8787',client=('203.0.113.1',50000)) as client:
        assert client.get('/api/config').status_code == 403
