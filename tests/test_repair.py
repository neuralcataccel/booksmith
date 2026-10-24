import hashlib
import json
from pathlib import Path
import shutil

import numpy as np
import pytest
import soundfile as sf

import catalogue
import extract
import profiles
import pronunciation
import reading_plan
import repair
import retention
import speech
import store
import worker
from test_chapters import make_book


@pytest.fixture
def parent_edition(tmp_path,monkeypatch):
    if not shutil.which('ffmpeg'):pytest.skip('ffmpeg required for real MP3 reuse checks')
    monkeypatch.setattr(store,'DB_PATH',tmp_path/'jobs.sqlite')
    monkeypatch.setattr(store,'prepare_dirs',lambda:None)
    store.init()
    work=tmp_path/'work';work.mkdir()
    for module in (store,worker,extract,repair,retention):monkeypatch.setattr(module,'WORK',work)
    config={**worker.CONFIG,'output_root':str(tmp_path/'final'),'staging_root':str(tmp_path/'stage'),'intermediate_retention_hours':0}
    for module in (worker,repair,retention):monkeypatch.setattr(module,'CONFIG',config)
    source=make_book(tmp_path,[('one.xhtml','<h1>One</h1><p>'+('Reed waited beside the open door. '*20)+'</p>'),
        ('two.xhtml','<h1>Two</h1><p>Reed came home and closed the window.</p>')],
        [('one.xhtml','Chapter 1'),('two.xhtml','Chapter 2')])
    book={'id':1,'title':'Original fixture','author':'Test Author','series':None,'description':''}
    monkeypatch.setattr(catalogue,'source_file',lambda bid:source)
    monkeypatch.setattr(catalogue,'cover_path',lambda bid:None)
    monkeypatch.setattr(catalogue,'get',lambda *a,**kw:book)
    monkeypatch.setattr(worker,'service_preview',lambda:None)
    monkeypatch.setattr(worker.plex_delivery,'scan',lambda relative:None)
    generated=[]
    def generate(text,options,path):
        spoken=pronunciation.apply(text,options.get('pronunciations',[]));generated.append(spoken)
        hz=200+int(hashlib.sha256(spoken.encode()).hexdigest()[:4],16)%300
        n=2400+int(24000*options.get('_pause_seconds',0))
        sf.write(path,.1*np.sin(np.arange(n)*2*np.pi*hz/24000),24000)
        return n/24000,.01
    monkeypatch.setattr(speech,'generate',generate)
    options={'voice':'af_heart','style':'natural','pace':1,'skip_notes':True,'skip_frontmatter':True,
             'pause_mode':'paragraph','paragraph_pause':.3,'chapter_pause':.8,
             'source_sha256':retention.digest(source)}
    options=profiles.resolve(options,book)
    job=store.create_job('book',book,options,speech.cache_key(1,options,options['source_sha256']))
    job=store.claim_next();worker.STOP.clear();worker.process_book(job)
    store.update_job(job['id'],status='ready',plex_key='7')
    job=store.get_job(job['id']);output=Path(job['output_path'])
    manifest=json.loads((output/'booksmith.json').read_text(encoding='utf-8'))
    monkeypatch.setattr(retention.plex_delivery,'indexed_tracks',lambda folder:[(str(i),'7',str(Path(folder)/t['file'])) for i,t in enumerate(manifest['tracks'])])
    assert retention.prune(job['id'])>0
    assert not (work/job['id']/f'audio-v{extract.EXTRACTOR_VERSION}').exists()
    generated.clear()
    return job,manifest,output,generated,work


def begin_repair(parent_edition):
    job,_,_,_,_=parent_edition
    profiles.save_dictionary('book',1,[{'word':'Reed','spoken':'reed ah'}],0)
    request=repair.create(job['id'],1)
    running=store.claim_next();assert running['id']==request['id']
    worker.process_book(running)
    return store.get_job(request['id'])


def test_repair_after_cleanup_renders_only_target_and_waits_for_approval(parent_edition):
    job,manifest,output,generated,work=parent_edition
    original_hashes=[retention.digest(output/t['file']) for t in manifest['tracks']]
    result=begin_repair(parent_edition)
    assert result['status']=='review' and not result['output_path']
    assert generated==['Two','reed ah came home and closed the window.']
    root=work/result['id'];plan,review=repair.review(root)
    assert len(review['tracks'])==2
    unchanged=repair.audio_path(root,plan,review['tracks'][0]['file'])
    assert repair.payload_digest(unchanged)==repair.payload_digest(output/manifest['tracks'][0]['file'])
    assert not plan['chapter_profiles']['0'].get('pronunciations')
    assert plan['chapter_profiles']['1']['pronunciations']==[{'word':'Reed','spoken':'reed ah'}]
    store.recover();assert store.get_job(result['id'])['status']=='review'
    repair.approve(result['id'])
    worker.process_book(store.claim_next())
    finished=store.get_job(result['id'])
    assert finished['status']=='awaiting_plex' and finished['output_path']!=job['output_path']
    assert generated==['Two','reed ah came home and closed the window.']
    assert [retention.digest(output/t['file']) for t in manifest['tracks']]==original_hashes
    new_manifest=json.loads((Path(finished['output_path'])/'booksmith.json').read_text(encoding='utf-8'))
    assert new_manifest['album_title']!=manifest['album_title']
    assert new_manifest['tracks'][0]['copied_audio_sha256']==repair.payload_digest(output/manifest['tracks'][0]['file'])


def test_corrupt_review_is_not_approved_and_original_is_untouched(parent_edition):
    job,manifest,output,_,work=parent_edition
    old_hash=retention.digest(output/manifest['tracks'][0]['file'])
    result=begin_repair(parent_edition);root=work/result['id']
    plan,review=repair.review(root)
    repair.audio_path(root,plan,review['tracks'][0]['file']).write_bytes(b'corrupt')
    with pytest.raises(ValueError,match='changed'):repair.approve(result['id'])
    assert store.get_job(result['id'])['status']=='review'
    assert retention.digest(output/manifest['tracks'][0]['file'])==old_hash


def test_repair_parent_ancestry_can_be_repaired_again(parent_edition):
    result=begin_repair(parent_edition)
    repair.approve(result['id']);worker.process_book(store.claim_next())
    store.update_job(result['id'],status='ready',plex_key='8')
    request=repair.create(result['id'],0)
    worker.process_book(store.claim_next())
    new=store.get_job(request['id'])
    assert new['status']=='review'
    _,manifest=repair.review(parent_edition[-1]/new['id'])
    assert manifest['repair']['parent']==result['id']


def test_post_approval_audio_changes_are_rejected(parent_edition):
    result=begin_repair(parent_edition);root=parent_edition[-1]/result['id']
    plan,manifest=repair.review(root)
    repair.approve(result['id'])
    repair.audio_path(root,plan,manifest['tracks'][0]['file']).write_bytes(b'changed after approval')
    with pytest.raises(ValueError,match='changed'):worker.process_book(store.claim_next())
    assert not (Path(worker.CONFIG['output_root'])/manifest['relative_folder']).exists()


def test_parent_manifest_changes_block_a_queued_repair(parent_edition):
    job,manifest,output,_,_=parent_edition
    request=repair.create(job['id'],0)
    manifest['summary']='Changed after queueing'
    (output/'booksmith.json').write_text(json.dumps(manifest),encoding='utf-8')
    with pytest.raises(ValueError,match='parent changed'):
        worker.process_book(store.claim_next())
    assert not (Path(worker.CONFIG['staging_root'])/f"{request['id']}-v7").exists()


def test_repair_api_requires_csrf_and_rejects_voice_changes(parent_edition,monkeypatch):
    import app
    from access import AccessControl
    from fastapi.testclient import TestClient
    monkeypatch.setattr(app,'AUTH',AccessControl(settings={}))
    monkeypatch.setattr(app.catalogue,'sync',lambda *a,**kw:None)
    monkeypatch.setattr(worker,'start',lambda:None)
    monkeypatch.setattr(worker,'stop',lambda:None)
    monkeypatch.setattr(app,'WORK',parent_edition[-1])
    jid=parent_edition[0]['id']
    with TestClient(app.app,base_url='http://localhost:8787',client=('127.0.0.1',50000)) as c:
        headers={'X-Booksmith-Token':c.get('/api/config').json()['csrf']}
        assert c.get(f'/api/jobs/{jid}/repair').status_code==200
        assert c.post(f'/api/jobs/{jid}/repair',json={'chapter':0}).status_code==403
        assert c.post(f'/api/jobs/{jid}/repair',json={'chapter':0,'voice':'am_eric'},headers=headers).status_code==422
        result=begin_repair(parent_edition)
        audio=c.get(f"/api/jobs/{result['id']}/repair-audio",headers={'Range':'bytes=0-127'})
        assert audio.status_code==206 and len(audio.content)==128
        monkeypatch.setattr(repair,'info',lambda jid:(_ for _ in ()).throw(OSError('private path detail')))
        response=c.get(f'/api/jobs/{jid}/repair')
        assert response.status_code==503 and 'private path' not in response.text
