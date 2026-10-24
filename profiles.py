"""Shared family profiles and scoped dictionaries, with immutable revision history."""
import json
import time
import uuid

import pronunciation
import store


def decode(row):
    if row is None:return None
    item=dict(row)
    item['options']=json.loads(item['options'])
    return item


def get(pid, revision=None):
    with store.db() as con:
        if revision is None:
            row=con.execute('SELECT * FROM reading_profiles WHERE id=? ORDER BY revision DESC LIMIT 1',(pid,)).fetchone()
        else:
            row=con.execute('SELECT * FROM reading_profiles WHERE id=? AND revision=?',(pid,revision)).fetchone()
    if row is None:raise ValueError('This saved profile no longer exists.')
    return decode(row)


def listing():
    with store.db() as con:
        return [decode(r) for r in con.execute('''SELECT p.* FROM reading_profiles p
            WHERE revision=(SELECT max(revision) FROM reading_profiles WHERE id=p.id)
            ORDER BY name COLLATE NOCASE,id''')]


def save(name, options, pid=None, expected_revision=None):
    name=name.strip()
    if not name or len(name)>80:raise ValueError('Give the profile a name up to 80 characters.')
    options={k:v for k,v in options.items() if not k.startswith('_') and k not in {'profile','source_sha256','chapter'}}
    with store.db() as con:
        con.execute('BEGIN IMMEDIATE')
        if pid:
            current=con.execute('SELECT max(revision) FROM reading_profiles WHERE id=?',(pid,)).fetchone()[0]
            if current is None or current!=expected_revision:
                raise ValueError('This profile changed. Reload it before saving another revision.')
        else:
            if con.execute('SELECT count(DISTINCT id) FROM reading_profiles').fetchone()[0]>=100:
                raise ValueError('The shared library already has 100 profiles.')
            pid=uuid.uuid4().hex[:16]
            current=0
        con.execute('INSERT INTO reading_profiles VALUES (?,?,?,?,?)',
                    (pid,current+1,name,json.dumps(options,sort_keys=True),time.time()))
    return get(pid,current+1)


def dictionary(scope,key):
    with store.db() as con:
        row=con.execute('SELECT * FROM pronunciation_dictionaries WHERE scope=? AND scope_key=? ORDER BY revision DESC LIMIT 1',(scope,str(key))).fetchone()
    if row is None:return {'scope':scope,'revision':0,'entries':[]}
    return {'scope':scope,'revision':row['revision'],'entries':json.loads(row['entries'])}


def save_dictionary(scope,key,entries,expected_revision):
    if scope not in {'book','series'}:raise ValueError('Choose book or series dictionary scope.')
    entries=pronunciation.validate(entries)
    with store.db() as con:
        con.execute('BEGIN IMMEDIATE')
        current=con.execute('SELECT max(revision) FROM pronunciation_dictionaries WHERE scope=? AND scope_key=?',(scope,str(key))).fetchone()[0] or 0
        if current!=expected_revision:raise ValueError('This dictionary changed. Reload it before saving.')
        con.execute('INSERT INTO pronunciation_dictionaries VALUES (?,?,?,?,?)',(scope,str(key),current+1,json.dumps(entries),time.time()))
    return dictionary(scope,key)


def resolve(options,book):
    series=dictionary('series',book['series']) if book.get('series') else {'revision':0,'entries':[]}
    local=dictionary('book',book['id'])
    base=options.get('_profile_pronunciations',options.get('pronunciations',[]))
    entries=pronunciation.merge(base,series['entries'],local['entries'])
    result=dict(options)
    result.pop('pronunciations',None)
    if entries:result['pronunciations']=entries
    result['_profile_pronunciations']=base
    result['_dictionaries']={'book_revision':local['revision'],'series_revision':series['revision']}
    return result
