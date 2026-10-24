import html
import json
import logging
import re
import sqlite3
import threading
import time

from bs4 import BeautifulSoup

import store
from settings import LIBRARY, SUPPORTED, contained

INDEX_LOCK = threading.Lock()


def source_db():
    con = sqlite3.connect((LIBRARY / 'metadata.db').as_uri() + '?mode=ro', uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def sync(force=False):
    with INDEX_LOCK:
        stamp = str((LIBRARY / 'metadata.db').stat().st_mtime_ns)
        if not force and store.meta('catalog_stamp') == stamp:
            return
        started = time.perf_counter()
        src = source_db()
        try:
            # Read the source in one consistent transaction. Never change Calibre.
            src.execute('BEGIN')
            rows = [dict(r) for r in src.execute('''SELECT b.id,b.title,b.path,b.has_cover,b.timestamp AS added,b.series_index,
                COALESCE((SELECT group_concat(a.name, ' & ') FROM books_authors_link l JOIN authors a ON a.id=l.author WHERE l.book=b.id),'Unknown') AS author,
                (SELECT s.name FROM books_series_link l JOIN series s ON s.id=l.series WHERE l.book=b.id LIMIT 1) AS series,
                (SELECT group_concat(g.lang_code, ',') FROM books_languages_link l JOIN languages g ON g.id=l.lang_code WHERE l.book=b.id) AS language,
                (SELECT text FROM comments c WHERE c.book=b.id LIMIT 1) AS description
                FROM books b''')]
            formats = {}
            for f in src.execute('SELECT book,format,name,uncompressed_size FROM data'):
                formats.setdefault(f['book'], []).append(dict(f))
        finally:
            src.close()
        with store.db() as con:
            con.execute('DELETE FROM books')
            con.execute('DELETE FROM book_search')
            con.executemany('''INSERT INTO books(id,title,author,series,series_index,path,formats,language,has_cover,added,description)
                VALUES (:id,:title,:author,:series,:series_index,:path,:formats,:language,:has_cover,:added,:description)''',
                ({**r, 'formats': json.dumps(formats.get(r['id'], []))} for r in rows))
            con.executemany('INSERT INTO book_search(rowid,title,author,series) VALUES (?,?,?,?)',
                            ((r['id'], r['title'], r['author'], r['series'] or '') for r in rows))
            con.execute("INSERT OR REPLACE INTO meta VALUES ('catalog_stamp',?)", (stamp,))
            con.execute("INSERT OR REPLACE INTO meta VALUES ('catalog_synced',?)", (str(time.time()),))
        logging.info('Indexed %d books in %.2fs', len(rows), time.perf_counter() - started)


def serialize(row, detail=False):
    b = dict(row)
    records = json.loads(b['formats'])
    b['formats'] = list(dict.fromkeys(f['format'] for f in records))
    b['supported'] = any(f in SUPPORTED for f in b['formats'])
    b['cover'] = f"/api/books/{b['id']}/cover" if b['has_cover'] else None
    b.pop('path', None)
    if detail:
        b['description'] = BeautifulSoup(b.get('description') or '', 'lxml').get_text(' ', strip=True)
        try:
            f = source_file(b['id'])
            b['source_format'] = f.suffix.upper()[1:]
            b['available'] = True
        except ValueError as exc:
            b['available'] = False
            b['availability_note'] = str(exc)
    else:
        b.pop('description', None)
    return b


def search(query='', page=1, available_only=True):
    tokens = re.findall(r'\w+', query, re.UNICODE)[:12]
    params = []
    where = []
    join = ''
    if tokens:
        join = 'JOIN book_search ON book_search.rowid=b.id'
        where.append('book_search MATCH ?')
        params.append(' AND '.join('"' + t.replace('"', '""') + '"*' for t in tokens))
    if available_only:
        where.append("EXISTS(SELECT 1 FROM json_each(b.formats) f WHERE json_extract(f.value,'$.format') IN ('EPUB','MOBI','AZW3','TXT'))")
    condition = (' WHERE ' + ' AND '.join(where)) if where else ''
    with store.db() as con:
        total = con.execute(f'SELECT count(*) FROM books b {join}{condition}', params).fetchone()[0]
        order = 'bm25(book_search), b.title' if tokens else 'b.added DESC, b.id DESC'
        rows = con.execute(f'SELECT b.* FROM books b {join}{condition} ORDER BY {order} LIMIT 36 OFFSET ?', (*params, (page - 1) * 36)).fetchall()
    return {'items': [serialize(r) for r in rows], 'total': total, 'page': page, 'pages': max(1, (total + 35) // 36)}


def get(bid, detail=False):
    with store.db() as con:
        row = con.execute('SELECT * FROM books WHERE id=?', (bid,)).fetchone()
    if not row:
        return None
    return serialize(row, detail=detail)


def raw(bid):
    with store.db() as con:
        row = con.execute('SELECT * FROM books WHERE id=?', (bid,)).fetchone()
    if not row:
        raise ValueError('This book is no longer in the catalog.')
    return dict(row)


def source_file(bid):
    b = raw(bid)
    records = json.loads(b['formats'])
    for preferred in SUPPORTED:
        for f in records:
            if f['format'] == preferred:
                p = contained(LIBRARY, b['path'] + '/' + f['name'] + '.' + preferred.lower())
                if p.is_file():
                    return p
    if any(f['format'] in SUPPORTED for f in records):
        raise ValueError('The catalog entry exists, but its ebook file could not be found.')
    raise ValueError('This proof of concept supports EPUB, MOBI, AZW3 and TXT. PDF/OCR comes later.')


def cover_path(bid):
    p = contained(LIBRARY, raw(bid)['path'] + '/cover.jpg')
    return p if p.is_file() else None


def counts():
    with store.db() as con:
        total = con.execute('SELECT count(*) FROM books').fetchone()[0]
        supported = con.execute("SELECT count(*) FROM books b WHERE EXISTS(SELECT 1 FROM json_each(b.formats) f WHERE json_extract(f.value,'$.format') IN ('EPUB','MOBI','AZW3','TXT'))").fetchone()[0]
    return {'total': total, 'supported': supported}
