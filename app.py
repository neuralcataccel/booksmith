import hashlib
import ipaddress
import json
import logging
import re
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from access import AUTH
import catalogue
import annotations
import jev_client
import quality
import plex_delivery
import profiles
import pronunciation
import repair
import retention
import speech
import store
import worker
from settings import CONFIG, MODELS, ROOT, WORK, RUNTIME, contained


@asynccontextmanager
async def lifespan(app):
    store.init()
    catalogue.sync()
    worker.start()
    yield
    worker.stop()


app = FastAPI(title='Booksmith', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
NETWORKS = [ipaddress.ip_network(value) for value in CONFIG.get('allowed_networks',['127.0.0.0/8','::1/128'])]
HOSTS = set(CONFIG.get('allowed_hosts',['localhost','127.0.0.1','[::1]']))
PUBLIC_HOST = CONFIG.get('public_hostname')
PROXY_PEERS = set(CONFIG.get('trusted_proxy_peers',[]))


@app.middleware('http')
async def private_network(request: Request, call_next):
    try:
        ip = ipaddress.ip_address(request.client.host)
        if not any(ip in network for network in NETWORKS):
            return JSONResponse({'detail': 'Booksmith is available on the home network only.'}, status_code=403)
    except ValueError:
        return JSONResponse({'detail': 'Invalid client address.'}, status_code=403)
    host = request.headers.get('host', '').lower()
    hostname = urlsplit('http://' + host).hostname
    public = hostname == PUBLIC_HOST
    if not public and hostname not in HOSTS and ('[' + str(hostname) + ']') not in HOSTS:
        return JSONResponse({'detail': 'Unrecognized host.'}, status_code=400)
    if public:
        if not AUTH.enabled or request.client.host not in PROXY_PEERS or not AUTH.gateway_valid(request.headers.get('x-booksmith-gateway', '')):
            return JSONResponse({'detail': 'Use the protected Booksmith address.'}, status_code=403)
        # Only the NAS proxy can supply scheme/client metadata. Uvicorn leaves the actual peer intact.
        try:
            edge_scheme = json.loads(request.headers.get('cf-visitor', '{}')).get('scheme')
        except (ValueError, AttributeError):
            edge_scheme = None
        edge_scheme = edge_scheme or request.headers.get('x-forwarded-proto', '').split(',')[0].strip()
        if edge_scheme != 'https':
            from fastapi.responses import RedirectResponse
            return RedirectResponse('https://' + PUBLIC_HOST + request.url.path + ('?' + request.url.query if request.url.query else ''), status_code=308)
    internal_health = request.url.path == '/healthz' and ip.is_loopback and hostname in {'localhost', '127.0.0.1', '::1'}
    if AUTH.enabled and not internal_health:
        client_key = request.client.host
        if public:
            try:
                client_key = str(ipaddress.ip_address(request.headers.get('cf-connecting-ip', request.client.host)))
            except ValueError:
                pass
        if AUTH.limited(client_key):
            return JSONResponse({'detail': 'Too many failed sign-in attempts. Try again in 15 minutes.'}, status_code=429, headers={'Retry-After': '900', 'Cache-Control': 'no-store'})
        authorization = request.headers.get('authorization', '')
        if not AUTH.authenticated(authorization):
            if authorization:
                AUTH.failed(client_key)
            return JSONResponse({'detail': 'Sign in to Booksmith.'}, status_code=401, headers={'WWW-Authenticate': 'Basic realm="Booksmith", charset="UTF-8"', 'Cache-Control': 'no-store'})
        AUTH.succeeded(client_key)
    if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
        origin = request.headers.get('origin')
        expected = 'https://' + PUBLIC_HOST if public else f'{request.url.scheme}://{host}'
        if origin and origin.rstrip('/') != expected:
            return JSONResponse({'detail': 'Cross-site requests are not allowed.'}, status_code=403)
        token = request.headers.get('x-booksmith-token', '')
        if not secrets.compare_digest(token, store.meta('csrf', 'uninitialized')):
            return JSONResponse({'detail': 'Refresh the page before making changes.'}, status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
    if public:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000'
    response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; media-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'"
    if request.url.path.startswith('/api/') and not request.url.path.endswith('/cover'):
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.get('/healthz')
def health(request: Request):
    if request.client.host not in {'127.0.0.1', '::1'}:
        raise HTTPException(404)
    return {'worker_alive': bool(worker.THREAD and worker.THREAD.is_alive())}


class Options(BaseModel):
    model_config = ConfigDict(extra='forbid')
    voice: str = speech.DEFAULT_VOICE
    style: Literal['natural', 'unhurried', 'brisk'] = 'natural'
    pace: float = Field(default=1.0, ge=0.75, le=1.25)
    skip_notes: bool = True
    skip_frontmatter: bool = True
    pause_mode: Literal['legacy','paragraph'] = 'legacy'
    paragraph_pause: float = Field(default=.35, ge=0, le=2, allow_inf_nan=False)
    chapter_pause: float = Field(default=.9, ge=0, le=5, allow_inf_nan=False)
    pronunciations: list[dict[str,str]] = Field(default_factory=list, max_length=100)
    profile_id: str | None = Field(default=None, pattern=r'^[0-9a-f]{16}$')
    profile_revision: int | None = Field(default=None, ge=1)

    def checked(self):
        if self.voice not in {v['id'] for v in speech.VOICES}:
            raise HTTPException(422, 'Choose a voice from the list.')
        result={key:getattr(self,key) for key in ('voice','style','pace','skip_notes','skip_frontmatter')}
        if self.pause_mode=='paragraph':
            result.update(pause_mode='paragraph',paragraph_pause=self.paragraph_pause,chapter_pause=self.chapter_pause)
        try:
            entries=pronunciation.validate(self.pronunciations)
            if entries:result['pronunciations']=entries
            if self.profile_id:
                if not self.profile_revision:raise ValueError('Select a saved profile revision.')
                saved=profiles.get(self.profile_id,self.profile_revision)
                if result!=saved['options']:raise ValueError('These settings differ from the saved profile. Save a revision or select Custom settings.')
                result['profile']={k:saved[k] for k in ('id','revision','name')}
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from exc
        result['_engine']=speech.default_engine()
        return result


class BookRequest(Options):
    book_id: int = Field(gt=0)
    chapter: int | None = Field(default=None, ge=0, le=10000)
    use_reviewed_pronunciations: bool = True
    contextual_pauses: bool = True


class PreviewRequest(Options):
    book_id: int | None = Field(default=None, gt=0)
    chapter: int | None = Field(default=None, ge=0, le=10000)
    use_reviewed_pronunciations: bool = True


class AnalysisRequest(Options):
    chapter: int = Field(default=0,ge=0,le=10000)
    offset: int = Field(default=0,ge=0,le=1000000)


class PronunciationDecision(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=1)
    choice: str | None = Field(default=None,max_length=30)


class PronunciationPreview(Options):
    expected_revision: int = Field(ge=1)
    choice: str | None = Field(default=None,max_length=30)


@app.get('/api/books/{bid}/context-pronunciations')
def context_pronunciations(bid:int,chapter:int|None=Query(None,ge=0,le=10000),skip_notes:bool=True,skip_frontmatter:bool=True):
    try:return annotations.listing(bid,chapter,{'skip_notes':skip_notes,'skip_frontmatter':skip_frontmatter})
    except ValueError as exc:raise HTTPException(400,str(exc)) from exc
    except OSError as exc:raise HTTPException(503,'The ebook is currently unavailable.') from exc


@app.post('/api/books/{bid}/context-pronunciations',status_code=202)
def check_pronunciations(bid:int,payload:AnalysisRequest):
    try:
        book=catalogue.get(bid)
        if not book:raise ValueError('Book not found.')
        if not jev_client.available():raise ValueError('Hosted pronunciation checks are currently unavailable. Book conversion still works.')
        options=payload.checked()
        options['source_sha256']=retention.digest(catalogue.source_file(bid))
        result=annotations.create(book,options,payload.chapter,payload.offset)
    except ValueError as exc:raise HTTPException(400,str(exc)) from exc
    except OSError as exc:raise HTTPException(503,'The ebook is currently unavailable.') from exc
    worker.WAKE.set()
    return result


@app.post('/api/pronunciation-reviews/{rid}/decision')
def review_pronunciation(rid:str,payload:PronunciationDecision):
    try:return annotations.review(rid,payload.expected_revision,payload.choice)
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc
    except OSError as exc:raise HTTPException(503,'The ebook is currently unavailable.') from exc


@app.post('/api/pronunciation-reviews/{rid}/preview',status_code=202)
def preview_pronunciation(rid:str,payload:PronunciationPreview):
    try:
        row=annotations.get(rid)
        book=catalogue.get(row['book_id'])
        options=profiles.resolve(payload.checked(),book)
        options=annotations.preview_options(rid,payload.expected_revision,payload.choice,options)
        return enqueue_preview(options)
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc
    except OSError as exc:raise HTTPException(503,'The ebook is currently unavailable.') from exc


class ProfileRequest(BaseModel):
    name: str = Field(min_length=1,max_length=80)
    options: Options
    id: str | None = Field(default=None,pattern=r'^[0-9a-f]{16}$')
    expected_revision: int | None = Field(default=None,ge=1)


class DictionaryRequest(BaseModel):
    scope: Literal['book','series'] = 'book'
    entries: list[dict[str,str]] = Field(default_factory=list,max_length=100)
    expected_revision: int = Field(default=0,ge=0)


class RepairRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    chapter:int=Field(ge=0,le=10000)


@app.get('/api/jobs/{jid}/quality')
def job_quality(jid:str):
    try:return quality.summary(jid)
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc
    except OSError as exc:raise HTTPException(503,'Audio or retained quality records are currently unavailable.') from exc


@app.post('/api/jobs/{jid}/quality',status_code=202)
def request_quality(jid:str):
    try:result=quality.request(jid)
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc
    except OSError as exc:raise HTTPException(503,'Audio or retained quality records are currently unavailable.') from exc
    worker.WAKE.set()
    return result


@app.get('/api/jobs/{jid}/quality-audio')
def quality_audio(jid:str,chapter:int=Query(ge=0,le=10000)):
    try:path=quality.audio_path(jid,chapter)
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc
    except OSError as exc:raise HTTPException(503,'This chapter is currently unavailable.') from exc
    return FileResponse(path,media_type='audio/mpeg')


@app.get('/api/jobs/{jid}/repair')
def repair_info(jid:str):
    try:return repair.info(jid)
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc
    except OSError as exc:raise HTTPException(503,'Media storage is unavailable. The original edition is retained.') from exc


@app.post('/api/jobs/{jid}/repair',status_code=202)
def request_repair(jid:str,payload:RepairRequest):
    try:result=repair.create(jid,payload.chapter)
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc
    except OSError as exc:raise HTTPException(503,'Media storage is unavailable. The original edition is retained.') from exc
    worker.WAKE.set()
    return result


@app.post('/api/jobs/{jid}/approve-repair',status_code=202)
def approve_repair(jid:str):
    try:result=repair.approve(jid)
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc
    except OSError as exc:raise HTTPException(503,'Repair audio is unavailable. The original edition is retained.') from exc
    worker.WAKE.set()
    return result


@app.get('/api/jobs/{jid}/repair-audio')
def repair_audio(jid:str):
    j=store.get_job(jid)
    if not j or j['status']!='review':raise HTTPException(404,'This repair is not waiting for review.')
    options=json.loads(j['options'])
    if not options.get('_repair'):raise HTTPException(404)
    try:
        plan,manifest=repair.review(WORK/jid,verify_audio=False)
        track=manifest['tracks'][options['_repair']['chapter']]
        path=repair.audio_path(WORK/jid,plan,track['file'])
        if retention.digest(path)!=track['sha256']:raise ValueError('Repair audio changed; regenerate before publication.')
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc
    except OSError as exc:raise HTTPException(503,'Repair audio is unavailable. The original edition is retained.') from exc
    return FileResponse(path,media_type='audio/mpeg')


def dictionary_key(bid, scope):
    book=catalogue.get(bid)
    if not book:raise HTTPException(404,'Book not found.')
    key=book['id'] if scope=='book' else book.get('series')
    if not key:raise HTTPException(400,'This book has no series in Calibre.')
    return key


@app.get('/api/profiles')
def saved_profiles():
    return {'items':profiles.listing()}


@app.post('/api/profiles')
def save_profile(payload: ProfileRequest):
    try:
        return profiles.save(payload.name,payload.options.checked(),payload.id,payload.expected_revision)
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from exc


@app.get('/api/books/{bid}/pronunciations')
def get_dictionary(bid:int,scope:Literal['book','series']='book'):
    return profiles.dictionary(scope,dictionary_key(bid,scope))


@app.post('/api/books/{bid}/pronunciations')
def save_dictionary(bid:int,payload:DictionaryRequest):
    try:
        return profiles.save_dictionary(payload.scope,dictionary_key(bid,payload.scope),payload.entries,payload.expected_revision)
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from exc


@app.get('/api/books/{bid}/chapters')
def book_chapters(bid:int,skip_notes:bool=True,skip_frontmatter:bool=True):
    from extract import extract_book
    try:
        ex=extract_book(catalogue.source_file(bid),{'skip_notes':skip_notes,'skip_frontmatter':skip_frontmatter})
        return {'items':[{'index':i,'title':c['title'],'words':sum(len(t.split()) for t in c['chunks'])} for i,c in enumerate(ex['chapters'])]}
    except ValueError as exc:
        raise HTTPException(400,str(exc)) from exc


@app.get('/')
def home():
    return FileResponse(ROOT / 'web' / 'index.html',headers={'Cache-Control':'no-store'})


@app.get('/api/config')
def config():
    return {'name': 'Booksmith', 'csrf': store.meta('csrf'), 'voices': speech.VOICES, 'default_voice': speech.DEFAULT_VOICE, 'styles': speech.STYLES, 'engine': speech.engine_label(), 'library': plex_delivery.status()['library'], 'capabilities': {'expressive_style': False,'chapter_repair':True,'contextual_pauses':bool(CONFIG.get('contextual_pauses',False)),'auditions':True,'context_pronunciation':True,'context_languages':['en-us'],'hosted_checks_available':jev_client.available(), 'supported_formats': list(catalogue.SUPPORTED)}}


@app.get('/api/auditions')
def auditions():
    path=RUNTIME/'data'/'auditions'/'index.json'
    if not path.is_file():return {'items':[]}
    data=json.loads(path.read_text(encoding='utf-8'))
    return {'items':[{k:v for k,v in item.items() if k!='file'} for item in data['items']]}


@app.get('/api/auditions/{aid}/audio')
def audition_audio(aid:str):
    if not re.fullmatch(r'[a-z0-9-]{1,80}',aid):raise HTTPException(404)
    root=RUNTIME/'data'/'auditions'
    index=root/'index.json'
    if not index.is_file():raise HTTPException(404)
    data=json.loads(index.read_text(encoding='utf-8'))
    item=next((x for x in data['items'] if x['id']==aid),None)
    if not item:raise HTTPException(404)
    name=item['file']
    if Path(name).name!=name or '/' in name or '\\' in name or not name.endswith('.mp3'):
        raise HTTPException(404)
    path=contained(root,name)
    if not path.is_file():raise HTTPException(404)
    return FileResponse(path,media_type='audio/mpeg')


@app.get('/api/status')
def status():
    with store.db() as con:
        queued = con.execute("SELECT count(*) FROM jobs WHERE kind='book' AND status IN ('queued','running','delivering','awaiting_plex')").fetchone()[0]
        ready = con.execute("SELECT count(*) FROM jobs WHERE kind='book' AND status='ready'").fetchone()[0]
        row = con.execute("SELECT audio_seconds,synthesis_seconds FROM jobs WHERE kind='book' AND status='ready' AND synthesis_seconds>0 ORDER BY updated DESC LIMIT 1").fetchone()
    speed = round(row['audio_seconds'] / row['synthesis_seconds'], 1) if row else None
    return {'catalogue': catalogue.counts(), 'queue': queued, 'ready': ready, 'worker_alive': bool(worker.THREAD and worker.THREAD.is_alive()), 'engine_ready': all((MODELS / f).is_file() for f in ['kokoro-v1.0.onnx', 'voices-v1.0.bin']), 'measured_speed': speed, 'plex': plex_delivery.status(), 'catalog_synced': store.meta('catalog_synced')}


@app.get('/api/books')
def books(q: str = Query('', max_length=200), page: int = Query(1, ge=1, le=10000), convertible: bool = True):
    return catalogue.search(q, page, convertible)


@app.get('/api/books/{bid}')
def book(bid: int):
    b = catalogue.get(bid, detail=True)
    if not b:
        raise HTTPException(404, 'Book not found.')
    return b


@app.get('/api/books/{bid}/cover')
def cover(bid: int):
    try:
        path = catalogue.cover_path(bid)
    except ValueError:
        path = None
    if not path:
        raise HTTPException(404, 'No cover available.')
    return FileResponse(path, media_type='image/jpeg', headers={'Cache-Control': 'private, max-age=3600'})


@app.get('/api/jobs')
def jobs():
    return {'items': store.jobs()}


@app.get('/api/jobs/{jid}')
def job(jid: str):
    result = store.get_job(jid, public=True)
    if not result:
        raise HTTPException(404, 'Conversion not found.')
    return result


@app.post('/api/jobs', status_code=202)
def convert(payload: BookRequest):
    options = Options(**payload.model_dump(exclude={'book_id','chapter','use_reviewed_pronunciations','contextual_pauses'})).checked()
    try:
        b = catalogue.get(payload.book_id, detail=True)
        if not b:
            raise ValueError('Book not found.')
        source = catalogue.source_file(payload.book_id)
        if source.stat().st_size > 200 * 1024 * 1024:
            raise ValueError('This ebook is too large for the proof of concept.')
        with source.open('rb') as stream:
            options['source_sha256'] = hashlib.file_digest(stream, 'sha256').hexdigest()
        options=profiles.resolve(options,b)
        if payload.chapter is not None:
            from extract import extract_book
            ex=extract_book(source,options)
            if payload.chapter>=len(ex['chapters']):raise ValueError('The selected chapter does not exist.')
            options['chapter']=payload.chapter
            b={**b,'title':b['title']+' — '+ex['chapters'][payload.chapter]['title']}
        options=annotations.attach(options,b,payload.use_reviewed_pronunciations)
        import context_pauses
        options=context_pauses.attach(options,payload.contextual_pauses and CONFIG.get('contextual_pauses',False))
        key = speech.cache_key(payload.book_id, options, options['source_sha256'])
        result = store.create_job('book', b, options, key)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    worker.WAKE.set()
    return result


@app.post('/api/previews', status_code=202)
def preview(payload: PreviewRequest):
    options = payload.checked()
    try:
        if payload.book_id is not None:
            b=catalogue.get(payload.book_id)
            if not b:raise ValueError('Book not found.')
            source=catalogue.source_file(payload.book_id)
            with source.open('rb') as stream:options['source_sha256']=hashlib.file_digest(stream,'sha256').hexdigest()
            options=profiles.resolve(options,b)
            options['_preview_book']=payload.book_id
            if payload.chapter is not None:options['chapter']=payload.chapter
            options=annotations.attach(options,b,payload.use_reviewed_pronunciations)
        key = speech.cache_key(None, options, options.get('source_sha256',speech.PREVIEW_TEXT))
    except ValueError as exc:
        raise HTTPException(400,str(exc)) from exc
    return enqueue_preview(options)


def enqueue_preview(options):
    key = speech.cache_key(None,options,options.get('source_sha256',speech.PREVIEW_TEXT))
    with store.db() as con:
        existing = con.execute("SELECT * FROM jobs WHERE kind='preview' AND edition_key=? AND status IN ('queued','running','ready') ORDER BY created DESC LIMIT 1", (key,)).fetchone()
    if existing:
        result = store.public_job(existing)
        if result['status'] != 'ready' or (WORK / result['id'] / 'preview.mp3').is_file():
            return result
    try:
        result = store.create_job('preview', {'title': 'Voice preview', 'author': speech.voice_info(options['voice'])['name']}, options, key)
    except ValueError as exc:
        raise HTTPException(429, str(exc)) from exc
    worker.WAKE.set()
    return result


@app.get('/api/jobs/{jid}/audio')
def audio(jid: str):
    j = store.get_job(jid)
    if not j or j['kind'] != 'preview' or j['status'] != 'ready':
        raise HTTPException(404, 'The preview is not ready.')
    path = WORK / j['id'] / 'preview.mp3'
    if not path.is_file():
        raise HTTPException(404, 'This preview has expired. Record another one.')
    return FileResponse(path, media_type='audio/mpeg')


@app.post('/api/jobs/{jid}/cancel')
def cancel(jid: str):
    j = store.get_job(jid)
    if not j:
        raise HTTPException(404, 'Conversion not found.')
    if j['status'] not in {'queued', 'running','review'}:
        raise HTTPException(409, 'Only waiting or narrating jobs can be cancelled. Completed audio is kept in Plex.')
    values = {'cancel_requested': 1}
    if j['status'] in {'queued','review'}:
        values.update(status='cancelled', message='Cancelled')
    store.update_job(jid, **values)
    return store.get_job(jid, public=True)


@app.post('/api/jobs/{jid}/retry')
def retry(jid: str):
    j = store.get_job(jid)
    if not j:
        raise HTTPException(404, 'Conversion not found.')
    if j['status'] not in {'failed', 'cancelled', 'awaiting_plex'}:
        raise HTTPException(409, 'This conversion is already active or complete.')
    if j['status'] == 'awaiting_plex':
        try:
            manifest = json.loads((Path(j['output_path']) / 'booksmith.json').read_text(encoding='utf-8'))
            plex_delivery.scan(Path(manifest['relative_folder']))
        except Exception as exc:
            raise HTTPException(503, 'Plex is unavailable. The completed audio is safe on the NAS.') from exc
    else:
        with store.db() as con:
            duplicate = con.execute("SELECT id FROM jobs WHERE edition_key=? AND id<>? AND status NOT IN ('failed','cancelled')", (j['edition_key'], jid)).fetchone()
        if duplicate:
            raise HTTPException(409, 'Another conversion already exists for these narration settings.')
        store.update_job(jid, status='queued', cancel_requested=0, error=None, message='Resuming completed chapters')
    worker.WAKE.set()
    return store.get_job(jid, public=True)


@app.post('/api/catalogue/refresh')
def refresh():
    catalogue.sync(force=True)
    return catalogue.counts()


app.mount('/static', StaticFiles(directory=ROOT / 'web'), name='static')
