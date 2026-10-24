import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

import quality
from qa_novel import audio_screen, compare_transcript
import store
import worker
from test_repair import parent_edition


@pytest.fixture
def edition(parent_edition,monkeypatch):
    monkeypatch.setattr(quality,'WORK',parent_edition[-1])
    monkeypatch.setattr(quality,'CONFIG',worker.CONFIG)
    return parent_edition


def test_quality_request_deduplicates_and_does_not_change_ready_parent(edition):
    job,_,_,_,_=edition
    first=quality.request(job['id']);second=quality.request(job['id'])
    assert first['id']==second['id'] and first['kind']=='quality'
    assert store.get_job(job['id'])['status']=='ready'
    assert quality.summary(job['id'])['state']=='queued'
    # Real conversions take precedence over queued local QA.
    request=store.create_job('book',{'id':2,'title':'Another book','author':'Author'},{},'new-book')
    assert store.claim_next()['id']==request['id']
    assert all(j['kind'] in {'book','preview'} for j in store.jobs())


def test_quality_failure_is_unchecked_and_preserves_original_audio(edition,monkeypatch,tmp_path):
    job,manifest,output,_,_=edition
    hashes=[quality.retention.digest(output/t['file']) for t in manifest['tracks']]
    monkeypatch.setattr(quality,'CONFIG',{**worker.CONFIG,'qa_python':str(tmp_path/'missing.exe')})
    quality.request(job['id']);worker.STOP.clear();worker.run_job(store.claim_next())
    summary=quality.summary(job['id'])
    assert summary['state']=='failed' and summary['checked_passages']==0
    assert store.get_job(job['id'])['status']=='ready'
    assert [quality.retention.digest(output/t['file']) for t in manifest['tracks']]==hashes


def test_quality_summarizes_exact_sample_coverage_and_unapplied_corrections(edition):
    job,plan,manifest,_,_=quality.parent(edition[0]['id'])
    manifest=json.loads(json.dumps(manifest))
    manifest['render_index']['chapters'][0]['chunks'][0]['pronunciations']=[{'id':'review-id','status':'unaligned'}]
    # Fixture audio is intentionally tiny: the independent density screen must also flag it.
    text='The girl followed the path through the forest and arrived before the evening bells began to ring beside the river.'
    sample={'chapter':1,'chunk':1,'title':'Chapter 1','text':text,'transcript':text,
            **compare_transcript(text,text),'audio_screen':{'checked':True,'flags':[]}}
    report=quality.summarize(job['id'],plan,manifest,{'source_unchanged':True},[sample])
    assert report['checked_passages']==1 and report['total_passages']>1
    assert report['flagged_passages']>=1 and report['corrections'][0]['status']=='unaligned'
    assert report['samples'][0]['chapter']==0 and report['samples'][0]['start_seconds']>=0
    assert report['checks']['all_tracks_indexed'] is None


def test_silence_repetition_and_high_similarity_truncation_are_distinct_flags(tmp_path):
    wav=tmp_path/'silence.wav';sf.write(wav,np.zeros(24000),24000,subtype='PCM_16')
    assert audio_screen(wav)['flags']==['silent_audio']
    text='The girl crossed the bridge and followed the path through the field before she reached the house where her grandmother waited by the window.'
    repeated=compare_transcript(text,text+' '+text)
    assert repeated['needs_listening'] and 'low_transcript_agreement' in repeated['listening_reasons']
    truncated=compare_transcript(text,' '.join(text.split()[:-6]))
    assert truncated['sequence_similarity']>.8 and 'possible_missing_ending' in truncated['listening_reasons']


def test_quality_audio_route_is_scoped_and_rechecks_final_hash(edition,monkeypatch):
    import app
    from access import AccessControl
    from fastapi.testclient import TestClient
    monkeypatch.setattr(app,'AUTH',AccessControl(settings={}))
    monkeypatch.setattr(app.catalogue,'sync',lambda *a,**k:None)
    monkeypatch.setattr(worker,'start',lambda:None)
    monkeypatch.setattr(worker,'stop',lambda:None)
    jid=edition[0]['id']
    with TestClient(app.app,base_url='http://localhost:8787',client=('127.0.0.1',50000)) as client:
        token=client.get('/api/config').json()['csrf'];headers={'X-Booksmith-Token':token}
        assert client.post(f'/api/jobs/{jid}/quality').status_code==403
        assert client.post(f'/api/jobs/{jid}/quality',headers=headers).status_code==202
        response=client.get(f'/api/jobs/{jid}/quality-audio?chapter=0',headers={'Range':'bytes=100-199'})
        assert response.status_code==206 and len(response.content)==100
        assert client.get(f'/api/jobs/{jid}/quality-audio?chapter=99').status_code==409
        path=quality.audio_path(jid,0);path.write_bytes(b'corrupt')
        assert client.get(f'/api/jobs/{jid}/quality-audio?chapter=0').status_code==409


def test_publishing_or_discarding_review_aborts_qa_without_blocking_the_worker(edition,monkeypatch):
    import sys
    jid=edition[0]['id'];request=quality.request(jid);task=store.claim_next()
    monkeypatch.setattr(quality,'CONFIG',{**worker.CONFIG,'qa_python':sys.executable})
    stopped=[]
    class Child:
        def poll(self):return None
    monkeypatch.setattr(quality.subprocess,'Popen',lambda *a,**k:Child())
    monkeypatch.setattr(quality,'stop_tree',lambda process:stopped.append(True))
    def publish_requested(task_id):store.update_job(jid,status='queued')
    with pytest.raises(ValueError,match='superseded'):quality.process(task,publish_requested)
    assert stopped==[True] and store.get_job(jid)['status']=='queued'
