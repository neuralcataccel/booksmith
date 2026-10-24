import contextlib
import json
import secrets
import sqlite3
import time
import uuid

from settings import DB_PATH, WORK, prepare_dirs


@contextlib.contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA busy_timeout=15000')
    try:
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def init():
    prepare_dirs()
    with db() as con:
        con.execute('PRAGMA journal_mode=WAL')
        con.executescript('''
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS books(
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, author TEXT NOT NULL,
            series TEXT, series_index REAL, path TEXT NOT NULL,
            formats TEXT NOT NULL, language TEXT, has_cover INTEGER,
            added TEXT, description TEXT
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS book_search USING fts5(title,author,series, tokenize='unicode61 remove_diacritics 2');
        CREATE TABLE IF NOT EXISTS jobs(
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, book_id INTEGER,
            title TEXT NOT NULL, author TEXT NOT NULL, options TEXT NOT NULL,
            edition_key TEXT NOT NULL, status TEXT NOT NULL,
            created REAL NOT NULL, started REAL, updated REAL NOT NULL,
            done_chunks INTEGER NOT NULL DEFAULT 0, total_chunks INTEGER NOT NULL DEFAULT 0,
            done_chapters INTEGER NOT NULL DEFAULT 0, total_chapters INTEGER NOT NULL DEFAULT 0,
            audio_seconds REAL NOT NULL DEFAULT 0, synthesis_seconds REAL NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT '', error TEXT,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            output_path TEXT, plex_key TEXT, plex_url TEXT
        );
        CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status,kind,created);
        CREATE INDEX IF NOT EXISTS jobs_edition ON jobs(edition_key,status);
        CREATE TABLE IF NOT EXISTS reading_profiles(
            id TEXT NOT NULL, revision INTEGER NOT NULL, name TEXT NOT NULL,
            options TEXT NOT NULL, created REAL NOT NULL,
            PRIMARY KEY(id,revision)
        );
        CREATE TABLE IF NOT EXISTS pronunciation_dictionaries(
            scope TEXT NOT NULL, scope_key TEXT NOT NULL, revision INTEGER NOT NULL,
            entries TEXT NOT NULL, created REAL NOT NULL,
            PRIMARY KEY(scope,scope_key,revision)
        );
        CREATE TABLE IF NOT EXISTS jev_calls(
            request_key TEXT PRIMARY KEY, owner TEXT NOT NULL, state TEXT NOT NULL,
            result TEXT, cost REAL NOT NULL DEFAULT 0, reserved REAL NOT NULL,
            created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pronunciation_reviews(
            id TEXT PRIMARY KEY, book_id INTEGER NOT NULL, source_sha TEXT NOT NULL,
            extractor TEXT NOT NULL, parser_options TEXT NOT NULL, occurrence TEXT NOT NULL,
            suggestion TEXT, choice TEXT, state TEXT NOT NULL DEFAULT 'pending',
            revision INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS pronunciation_review_book ON pronunciation_reviews(book_id,source_sha,state);
        CREATE TABLE IF NOT EXISTS pronunciation_review_events(
            id TEXT NOT NULL, revision INTEGER NOT NULL, state TEXT NOT NULL, choice TEXT,
            created REAL NOT NULL, PRIMARY KEY(id,revision)
        );
        CREATE TABLE IF NOT EXISTS quality_records(
            parent_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, task_id TEXT,
            state TEXT NOT NULL, report TEXT, updated REAL NOT NULL
        );
        ''')
        con.execute("INSERT OR IGNORE INTO meta VALUES ('csrf',?)", (secrets.token_urlsafe(32),))
        con.execute("INSERT OR IGNORE INTO meta VALUES ('auto_quality_since',?)", (str(time.time()),))


def meta(key, default=None):
    with db() as con:
        row = con.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return row[0] if row else default


def set_meta(key, value):
    with db() as con:
        con.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, str(value)))


def public_job(row):
    if not row:
        return None
    j = dict(row)
    j['options'] = json.loads(j['options'])
    j.pop('output_path', None)
    j.pop('edition_key', None)
    j['progress'] = min(100, round(100 * j['done_chunks'] / max(1, j['total_chunks'])))
    if j['status'] == 'ready':
        j['progress'] = 100
    j['preview_url'] = f"/api/jobs/{j['id']}/audio" if j['kind'] == 'preview' and j['status'] == 'ready' else None
    j['repair_audio_url']=f"/api/jobs/{j['id']}/repair-audio" if j['status']=='review' and j['options'].get('_repair') else None
    j['can_repair']=j['kind']=='book' and j['status']=='ready' and (WORK/j['id']/'reading-plan.json').is_file()
    j['can_check_quality']=j['kind']=='book' and j['status'] in {'ready','review'} and (WORK/j['id']/'reading-plan.json').is_file()
    if j['kind']=='book':
        with db() as con:quality=con.execute('SELECT state,report FROM quality_records WHERE parent_id=?',(j['id'],)).fetchone()
        if quality:
            report=json.loads(quality['report']) if quality['report'] else {}
            j['quality']={'state':quality['state'],'checked':report.get('checked_passages',0),
                          'total':report.get('total_passages',0),'flags':report.get('flagged_passages',0)}
    if j['kind']=='preview' and j['status']=='ready' and j['options'].get('_occurrence_preview'):
        receipt=WORK/j['id']/'0000.pronunciation.json'
        if receipt.is_file():j['pronunciation_outcomes']=json.loads(receipt.read_text(encoding='utf-8'))['outcomes']
    remaining = max(0, j['total_chunks'] - j['done_chunks'])
    j['eta_seconds'] = round(remaining * j['synthesis_seconds'] / j['done_chunks']) if j['done_chunks'] > 1 and j['status'] == 'running' else None
    return j


def get_job(jid, public=False):
    with db() as con:
        row = con.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
    return public_job(row) if public else (dict(row) if row else None)


def jobs(limit=80):
    with db() as con:
        return [public_job(r) for r in con.execute("SELECT * FROM jobs WHERE kind IN ('book','preview') ORDER BY created DESC LIMIT ?", (limit,))]


def create_job(kind, book, options, edition_key):
    now = time.time()
    with db() as con:
        con.execute('BEGIN IMMEDIATE')
        if kind in {'book','analysis','quality'}:
            row = con.execute("SELECT * FROM jobs WHERE edition_key=? AND kind=? AND status NOT IN ('failed','cancelled') ORDER BY created DESC LIMIT 1", (edition_key,kind)).fetchone()
            if row:
                return public_job(row)
        count = con.execute("SELECT count(*) FROM jobs WHERE kind<>'quality' AND status IN ('queued','running','delivering','awaiting_plex','review')").fetchone()[0]
        if count >= 25:
            raise ValueError('The queue is full. Finish or cancel a conversion first.')
        if kind == 'preview' and con.execute("SELECT count(*) FROM jobs WHERE kind='preview' AND status IN ('queued','running')").fetchone()[0] >= 3:
            raise ValueError('A few voice previews are already waiting. Try again shortly.')
        if kind in {'analysis','quality'} and con.execute("SELECT count(*) FROM jobs WHERE kind=? AND status IN ('queued','running')",(kind,)).fetchone()[0]>=2:
            raise ValueError('A couple of checks are already waiting. Try again shortly.')
        jid = uuid.uuid4().hex[:16]
        message={'analysis':'Waiting to check pronunciations','quality':'Waiting for local sample checks'}.get(kind,'Waiting for the narrator')
        con.execute('''INSERT INTO jobs(id,kind,book_id,title,author,options,edition_key,status,created,updated,message)
            VALUES (?,?,?,?,?,?,?,'queued',?,?,?)''',
            (jid, kind, book.get('id'), book['title'], book['author'], json.dumps(options, sort_keys=True), edition_key, now, now, message))
    return get_job(jid, public=True)


def update_job(jid, **values):
    allowed = {'status', 'started', 'done_chunks', 'total_chunks', 'done_chapters', 'total_chapters', 'audio_seconds', 'synthesis_seconds', 'message', 'error', 'cancel_requested', 'output_path', 'plex_key', 'plex_url'}
    if not values.keys() <= allowed:
        raise ValueError('Invalid job update')
    values['updated'] = time.time()
    with db() as con:
        con.execute('UPDATE jobs SET ' + ','.join(f'{k}=?' for k in values) + ' WHERE id=?', (*values.values(), jid))


def claim_next(previews_only=False,interactive_only=False):
    with db() as con:
        con.execute('BEGIN IMMEDIATE')
        extra = "AND kind='preview'" if previews_only else "AND kind IN ('preview','analysis')" if interactive_only else ''
        row = con.execute(f"SELECT * FROM jobs WHERE status='queued' {extra} ORDER BY CASE kind WHEN 'preview' THEN 0 WHEN 'analysis' THEN 1 WHEN 'quality' THEN 3 ELSE 2 END,created LIMIT 1").fetchone()
        if not row:
            return None
        now = time.time()
        con.execute("UPDATE jobs SET status='running',started=COALESCE(started,?),updated=?,message='Preparing narration' WHERE id=?", (now, now, row['id']))
    return get_job(row['id'])


def rekey_running_job(jid, expected, replacement):
    """Upgrade an in-flight extraction identity without creating duplicate editions."""
    if expected == replacement:
        return
    with db() as con:
        con.execute('BEGIN IMMEDIATE')
        current = con.execute('SELECT edition_key,status FROM jobs WHERE id=?', (jid,)).fetchone()
        if not current or current['status'] != 'running' or current['edition_key'] != expected:
            raise ValueError('The conversion changed during its extraction upgrade.')
        duplicate = con.execute("SELECT id FROM jobs WHERE edition_key=? AND id<>? AND status NOT IN ('failed','cancelled')", (replacement, jid)).fetchone()
        if duplicate:
            raise ValueError('Another conversion already has the upgraded narration settings.')
        con.execute('UPDATE jobs SET edition_key=?,updated=? WHERE id=?', (replacement, time.time(), jid))


def recover():
    with db() as con:
        con.execute("UPDATE jobs SET status='queued',message='Resuming after restart',updated=? WHERE status IN ('running','delivering')", (time.time(),))
        con.execute("UPDATE jobs SET status='cancelled',message='Cancelled' WHERE status='queued' AND cancel_requested=1")
