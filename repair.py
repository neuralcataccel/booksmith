"""Chapter repairs use the existing worker, review gate and verified publication path."""
import copy
import hashlib
import json
import re
from pathlib import Path

import catalogue
import annotations
import profiles
import reading_plan
import retention
import speech
import store
from extract import EXTRACTOR_VERSION, split_text
from settings import CONFIG, WORK, contained


def parent(jid):
    if not re.fullmatch(r'[0-9a-f]{16}',jid):raise ValueError('Invalid parent conversion.')
    job=store.get_job(jid)
    if not job or job['kind']!='book' or job['status']!='ready':raise ValueError('Choose a completed Booksmith edition.')
    root=WORK/jid
    path=root/'reading-plan.json'
    if not path.is_file():raise ValueError('This older edition has no retained reading plan. Generate a new edition first.')
    plan=reading_plan.validate(json.loads(path.read_text(encoding='utf-8')))
    if plan['extractor']!=EXTRACTOR_VERSION:raise ValueError('Repair requires the current extraction version; generate a new edition for this older plan.')
    output=Path(job['output_path'])
    manifest_path=output/'booksmith.json'
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest['job_id']!=jid or manifest['edition_key']!=job['edition_key'] or manifest.get('reading_plan',{}).get('sha256')!=plan['sha256']:
        raise ValueError('Parent edition identity or reading plan changed.')
    if output.resolve()!=contained(CONFIG['output_root'],manifest['relative_folder']):raise ValueError('Invalid parent output folder.')
    if len(plan['chapters'])!=len(manifest['tracks']) or len(manifest.get('render_index',{}).get('chapters',[]))!=len(plan['chapters']):
        raise ValueError('The parent has no complete retained chapter index.')
    for i,track in enumerate(manifest['tracks']):
        name=track['file']
        if Path(name).name!=name or '/' in name or '\\' in name or track['title']!=plan['chapters'][i]['title']:
            raise ValueError('Invalid parent track identity.')
    return job,plan,manifest,output,retention.digest(manifest_path)


def info(jid):
    job,plan,_,_,_=parent(jid)
    return {'id':jid,'title':job['title'],'chapters':[{'index':i,'title':c['title']} for i,c in enumerate(plan['chapters'])],
            'voice':plan['profile']['voice']['name']}


def create(jid,chapter):
    job,plan,manifest,output,manifest_hash=parent(jid)
    if not 0<=chapter<len(plan['chapters']):raise ValueError('Choose a chapter from this edition.')
    original=json.loads(job['options'])
    options=copy.deepcopy(original)
    # Start with the profile's original rules, then resolve current book/series overrides.
    if '_profile_pronunciations' not in options:
        if options.get('profile'):
            p=options['profile'];options['_profile_pronunciations']=profiles.get(p['id'],p['revision'])['options'].get('pronunciations',[])
        elif any(options.get('_dictionaries',{}).values()):
            raise ValueError('This custom legacy profile has no separate base dictionary; use a new saved-profile edition.')
    book=catalogue.get(job['book_id'])
    options=profiles.resolve(options,book)
    options=annotations.attach(options,book)
    # Repair ancestry describes the immediately preceding ready edition.
    options['_repair']={'parent':jid,'parent_edition':job['edition_key'],'manifest_sha256':manifest_hash,
                        'plan_sha256':plan['sha256'],'chapter':chapter,'strategy':'shorter_chunks_180'}
    key=speech.cache_key(job['book_id'],options,options['source_sha256'])
    return store.create_job('book',{'id':job['book_id'],'title':job['title'],'author':job['author']},options,key)


def prepare(extracted,options):
    spec=options['_repair'];job,old_plan,manifest,output,checksum=parent(spec['parent'])
    if checksum!=spec['manifest_sha256'] or job['edition_key']!=spec['parent_edition'] or old_plan['sha256']!=spec['plan_sha256']:
        raise ValueError('The parent changed after this repair request.')
    if old_plan['source_sha256']!=options['source_sha256']:raise ValueError('The repair source changed.')
    baseline=reading_plan.build(extracted,options['source_sha256'],{})
    def content(plan):
        return [(c['title'],[(s['id'],s['text'],s['kind']) for s in c['spans']]) for c in plan['chapters']]
    if content(baseline)!=content(old_plan):raise ValueError('The source structure changed since the parent render.')
    index=spec['chapter']
    result=reading_plan.to_extracted(old_plan)
    chapter=result['chapters'][index]
    # Preserve every original block and locator; change only its synthesis segmentation.
    chunks=[]
    for block in chapter['blocks']:
        pieces=split_text(block['text'],max_chars=180)
        block.update(chunk_start=len(chunks),chunk_count=len(pieces));chunks.extend(pieces)
    chapter['chunks']=chunks
    return result,old_plan,manifest,output


def payload_digest(path):
    import mutagen
    audio=mutagen.File(path)
    offset=getattr(audio.info,'frame_offset',0)
    with path.open('rb') as stream:
        stream.seek(offset)
        return hashlib.file_digest(stream,'sha256').hexdigest()


def review(root,verify_audio=True):
    plan=reading_plan.validate(json.loads((root/'reading-plan.json').read_text(encoding='utf-8')))
    manifest=json.loads((root/'review.json').read_text(encoding='utf-8'))
    if manifest['reading_plan']['sha256']!=plan['sha256']:raise ValueError('The repair changed after review was prepared.')
    for track in manifest['tracks']:
        if Path(track['file']).name!=track['file'] or '/' in track['file'] or '\\' in track['file']:
            raise ValueError('Invalid repair track filename.')
        if verify_audio and retention.digest(audio_path(root,plan,track['file']))!=track['sha256']:
            raise ValueError('Repair audio changed; regenerate before publication.')
    return plan,manifest


def audio_path(root,plan,name):
    version=plan['extractor']
    if not re.fullmatch(r'\d{1,3}',version) or Path(name).name!=name or '/' in name or '\\' in name:
        raise ValueError('Invalid repair audio path.')
    return contained(root,Path(f'audio-v{version}')/'chapters'/name)


def approve(jid):
    job=store.get_job(jid)
    if not job or job['status']!='review' or not json.loads(job['options']).get('_repair'):
        raise ValueError('This repair is not waiting for review.')
    plan,manifest=review(WORK/jid)
    with store.db() as con:
        con.execute('BEGIN IMMEDIATE')
        changed=con.execute("UPDATE jobs SET status='queued',message='Publishing approved chapter repair' WHERE id=? AND status='review'",(jid,)).rowcount
        if changed!=1:raise ValueError('The repair state changed. Refresh the page.')
        approval={'plan':plan['sha256'],'review':reading_plan.digest(manifest)}
        con.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('repair-approved-'+jid,json.dumps(approval)))
    return store.get_job(jid,public=True)
