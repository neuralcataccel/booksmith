import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

import app
import extract
import profiles
import pronunciation
import speech
import store
import worker
from access import AccessControl


@pytest.fixture
def db(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DB_PATH',tmp_path/'jobs.sqlite')
    monkeypatch.setattr(store,'prepare_dirs',lambda:None)
    store.init()
    work=tmp_path/'extract-work';work.mkdir()
    monkeypatch.setattr(extract,'WORK',work)
    return tmp_path


def test_dictionary_scopes_precedence_and_queued_snapshot(db):
    profiles.save_dictionary('series','Saga',[{'word':'Reed','spoken':'Reed series'}],0)
    profiles.save_dictionary('book',1,[{'word':'Reed','spoken':'Reed book'}],0)
    base={'voice':'af_heart','pronunciations':[{'word':'Reed','spoken':'Reed profile'}]}
    options=profiles.resolve(base,{'id':1,'series':'Saga'})
    assert options['pronunciations'][0]['spoken']=='Reed book'
    j=store.create_job('book',{'id':1,'title':'One','author':'Author'},options,'one')
    profiles.save_dictionary('book',1,[{'word':'Reed','spoken':'changed'}],1)
    assert store.get_job(j['id'],public=True)['options']['pronunciations'][0]['spoken']=='Reed book'
    assert profiles.resolve(base,{'id':2,'series':'Saga'})['pronunciations'][0]['spoken']=='Reed series'
    assert profiles.resolve(base,{'id':3})['pronunciations'][0]['spoken']=='Reed profile'
    with pytest.raises(ValueError,match='changed'):
        profiles.save_dictionary('book',1,[],1)


def test_profile_revisions_and_optimistic_updates_preserve_history(db):
    old=profiles.save('Evening',{'voice':'bf_emma'})
    new=profiles.save('Evening',{'voice':'af_heart'},old['id'],old['revision'])
    assert old['id']==new['id'] and new['revision']==2
    assert profiles.get(old['id'],1)['options']['voice']=='bf_emma'
    assert profiles.get(old['id'])['options']['voice']=='af_heart'
    assert len(profiles.listing())==1
    with pytest.raises(ValueError,match='changed'):
        profiles.save('Evening',{},old['id'],1)


def test_substitution_is_literal_non_chaining_and_whole_word():
    rules=pronunciation.validate([{'word':'A','spoken':'B'},{'word':'B','spoken':'C'},
        {'word':'Mount Doom','spoken':'long phrase'},{'word':'Mount','spoken':'short'},
        {'word':'i','spoken':'eye'}])
    assert pronunciation.apply('A B BA Mount Doom Mount Etna',rules)=='B C BA long phrase short Etna'
    assert pronunciation.apply('İ i',rules)=='eye eye'
    assert pronunciation.apply("Mount's",rules)=="short's"


@pytest.mark.parametrize('entries',[[{'word':'','spoken':'x'}],[{'word':'a','spoken':'x\ny'}],
    [{'word':'a','spoken':'x'},{'word':'A','spoken':'y'}],[{'word':'a','spoken':'x','regex':'.*'}]])
def test_bad_lexicons_are_rejected(entries):
    with pytest.raises(ValueError):pronunciation.validate(entries)


def test_legacy_cache_identity_excludes_only_non_audio_metadata():
    options={'voice':'af_heart','style':'natural','pace':1,'skip_notes':True,'skip_frontmatter':True}
    key=speech.cache_key(1,options,'source')
    assert key==speech.cache_key(1,{**options,'_engine':speech.cpu_descriptor(),'profile':{'revision':5},'_dictionaries':{'book_revision':8}},'source')
    assert key!=speech.cache_key(1,{**options,'pause_mode':'paragraph','paragraph_pause':.35},'source')
    assert key!=speech.cache_key(1,{**options,'pronunciations':[{'word':'a','spoken':'b'}]},'source')


def test_engine_applies_spoken_form_and_exact_pause_without_mutating_source(db,monkeypatch):
    seen=[]
    class Engine:
        def create(self,text,**kw):
            seen.append(text)
            return np.ones(2400,dtype=np.float32)*.1,24000
    monkeypatch.setattr(speech,'load',lambda:Engine())
    options={'voice':'af_heart','style':'natural','pace':1,'pronunciations':[{'word':'Reed','spoken':'reed'}],'_pause_seconds':0}
    text='Reed came home.'
    path=db/'sample.wav'
    duration,_=speech.generate(text,options,path)
    assert seen==['reed came home.'] and text=='Reed came home.'
    assert duration==pytest.approx(.1)
    assert sf.info(path).duration==pytest.approx(.1)
    options['_engine']={'runtime':'wrong'}
    with pytest.raises(ValueError,match='original speech engine'):speech.generate(text,options,path)


def test_story_selection_does_not_modify_the_anthology_plan():
    ex={'chapters':[{'title':'One','chunks':['One.']},{'title':'Two','chunks':['Two.']}],'characters':8}
    selected=worker.select_chapters(ex,{'chapter':1})
    assert selected['chapters']==[ex['chapters'][1]] and len(ex['chapters'])==2
    with pytest.raises(ValueError):worker.select_chapters(ex,{'chapter':5})


def test_tuned_editions_have_distinct_plex_album_tags():
    options={'voice':'af_heart','style':'natural','pace':1,'pause_mode':'paragraph'}
    a,_=worker.output_names({'title':'Story','author':'Author','edition_key':'a'*64},options)
    b,_=worker.output_names({'title':'Story','author':'Author','edition_key':'b'*64},options)
    assert a!=b


@pytest.mark.parametrize('version',['6','7'])
def test_worker_pauses_at_blocks_not_technical_splits(db,monkeypatch,version):
    work=db/'work';work.mkdir()
    monkeypatch.setattr(worker,'WORK',work)
    monkeypatch.setattr(extract,'WORK',work)
    monkeypatch.setattr(worker,'CONFIG',{**worker.CONFIG,'output_root':str(db/'output'),'staging_root':str(db/'stage')})
    source=db/'book.txt';source.write_text(('This paragraph keeps going. '*25).strip()+'\n\nThe end.',encoding='utf-8')
    options=app.Options(pause_mode='paragraph').checked()
    options['source_sha256']=hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(worker.catalogue,'source_file',lambda bid:source)
    monkeypatch.setattr(worker.catalogue,'cover_path',lambda bid:None)
    monkeypatch.setattr(worker.catalogue,'get',lambda *a,**kw:{'description':''})
    pauses=[]
    def generate(text,opts,path):
        pauses.append(opts['_pause_seconds'])
        sf.write(path,np.ones(2400,dtype=np.float32)*.1,24000)
        return .1,.01
    monkeypatch.setattr(speech,'generate',generate)
    monkeypatch.setattr(worker,'service_preview',lambda:None)
    monkeypatch.setattr(worker,'tag_audio',lambda *a:None)
    monkeypatch.setattr(worker,'ffmpeg',lambda args:Path(args[-1]).write_bytes(b'encoded'))
    monkeypatch.setattr(worker.plex_delivery,'scan',lambda relative:None)
    job=store.create_job('book',{'id':1,'title':'Book','author':'Author'},options,'edition')
    job=store.claim_next()
    if version=='6':
        ex=extract.extract_book(source,options,version='6',include_structure=True)
        plan=worker.reading_plan.build(ex,options['source_sha256'],speech.profile_snapshot(options))
        root=work/job['id'];root.mkdir()
        worker.reading_plan.save(root/'reading-plan.json',plan)
    worker.STOP.clear();worker.process_book(job)
    assert pauses[:-2]==[0]*(len(pauses)-2)
    assert pauses[-2:]==[.35,.9]
    finished=store.get_job(job['id'])
    manifest=json.loads((Path(finished['output_path'])/'booksmith.json').read_text(encoding='utf-8'))
    assert manifest['extraction_version']==version


def test_preview_boundaries_keep_split_paragraph_contiguous():
    chapter={'chunks':['Heading','a'*350,'b'*350,'c'*350,'Do not jump here.'],
        'blocks':[{'kind':'h1','chunk_start':0,'chunk_count':1},
                  {'kind':'p','chunk_start':1,'chunk_count':3},
                  {'kind':'p','chunk_start':4,'chunk_count':1}]}
    assert worker.preview_parts(chapter)==[('a'*350,False),('b'*350,False)]


def test_profile_and_preview_api_reject_silent_unsupported_controls(db,monkeypatch):
    monkeypatch.setattr(app,'AUTH',AccessControl(settings={}))
    monkeypatch.setattr(app.catalogue,'sync',lambda *a,**kw:None)
    monkeypatch.setattr(worker,'start',lambda:None)
    monkeypatch.setattr(worker,'stop',lambda:None)
    source=db/'book.txt';source.write_text('One.\n\nTwo.',encoding='utf-8')
    book={'id':1,'title':'Test','author':'Author','series':None}
    monkeypatch.setattr(app.catalogue,'get',lambda *a,**kw:book)
    monkeypatch.setattr(app.catalogue,'source_file',lambda bid:source)
    with TestClient(app.app,base_url='http://localhost:8787',client=('127.0.0.1',50000)) as client:
        headers={'X-Booksmith-Token':client.get('/api/config').json()['csrf']}
        assert client.post('/api/previews',json={'direction':'whisper angrily'},headers=headers).status_code==422
        saved=client.post('/api/profiles',json={'name':'Evening','options':{'voice':'bf_emma','pause_mode':'paragraph'}},headers=headers).json()
        req={**saved['options'],'profile_id':saved['id'],'profile_revision':saved['revision'],'book_id':1}
        preview=client.post('/api/previews',json=req,headers=headers)
        assert preview.status_code==202 and preview.json()['options']['_preview_book']==1
        req['voice']='af_heart'
        assert client.post('/api/jobs',json=req,headers=headers).status_code==422
        invalid=client.post('/api/jobs',json={'book_id':1,'chapter':3},headers=headers)
        assert invalid.status_code==400
        response=client.post('/api/books/1/pronunciations',json={'scope':'series','entries':[]},headers=headers)
        assert response.status_code==400


def test_audition_media_is_limited_to_indexed_mp3s(db,monkeypatch):
    monkeypatch.setattr(app,'AUTH',AccessControl(settings={}))
    monkeypatch.setattr(app,'RUNTIME',db)
    monkeypatch.setattr(app.catalogue,'sync',lambda *a,**kw:None)
    monkeypatch.setattr(worker,'start',lambda:None)
    monkeypatch.setattr(worker,'stop',lambda:None)
    root=db/'data'/'auditions';root.mkdir(parents=True)
    index=root/'index.json'
    index.write_text(json.dumps({'items':[{'id':'safe','file':'safe.mp3','name':'Safe'}]}))
    (root/'safe.mp3').write_bytes(b'audio')
    with TestClient(app.app,base_url='http://localhost:8787',client=('127.0.0.1',50000)) as c:
        assert 'file' not in c.get('/api/auditions').json()['items'][0]
        assert c.get('/api/auditions/safe/audio').content==b'audio'
        assert c.get('/api/auditions/unlisted/audio').status_code==404
        index.write_text(json.dumps({'items':[{'id':'bad','file':'../outside.mp3'}]}))
        assert c.get('/api/auditions/bad/audio').status_code==404
