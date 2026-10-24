import json
from pathlib import Path

import pytest

import annotations
import catalogue
import extract
import jev_client
import occurrence_phones
import retention
import speech
import store
import worker
from test_chapters import make_book


@pytest.fixture
def library(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DB_PATH',tmp_path/'jobs.sqlite')
    monkeypatch.setattr(store,'prepare_dirs',lambda:None)
    store.init()
    root=tmp_path/'work';root.mkdir()
    monkeypatch.setattr(annotations,'WORK',root)
    monkeypatch.setattr(extract,'WORK',root)
    monkeypatch.setattr(worker,'WORK',root)
    monkeypatch.setattr(store,'WORK',root)
    source=make_book(tmp_path,[('one.xhtml','<h1>One</h1><p>Yesterday she read the letter. Tomorrow she will read it again.</p>')],[('one.xhtml','Chapter One')])
    monkeypatch.setattr(catalogue,'source_file',lambda bid:source)
    book={'id':7,'title':'An original test','author':'Test Author'}
    options={'voice':'am_eric','style':'natural','pace':1,'skip_notes':True,'skip_frontmatter':True,'source_sha256':retention.digest(source)}
    return book,options,source


def response(payload):
    choices=payload['questions']['decision']['criteria'];first=next(iter(choices))
    return {'model':'typesafe/jev-1.13-test','usage':{'cost':.00001,'input_tokens':200},'answers':{
        'decision':{'choice':first,'confidence':.9,'probabilities':{k:1 if k==first else 0 for k in choices}},
        'unique':{'noul':.91}}}


def test_real_store_durable_decision_cache_is_shared_without_duplicate_calls(library):
    payload={'questions':{'decision':{'criteria':{'a':'A','unknown':'Unknown'}}}}
    calls=[]
    def send(p):calls.append(p);return response(p)
    first=jev_client.decide(payload,'owner-a',{'a':'A','unknown':'Unknown'},send=send)
    second=jev_client.decide(payload,'owner-b',{'a':'A','unknown':'Unknown'},send=send)
    assert len(calls)==1 and first['new_cost']==.00001
    assert second['cached'] and second['new_cost']==0
    with store.db() as con:assert con.execute('SELECT sum(cost) FROM jev_calls').fetchone()[0]==.00001


def test_hosted_prompt_marks_repeated_target_without_numeric_offset_counting():
    text='They have read it, and they will read it again.'
    first=text.index('read');second=text.index('read',first+1)
    states=[]
    for start in (first,second):
        state=annotations.payload({'word':'read','text':text,'start':start,'end':start+4})['state']
        assert state['marked_passage'].replace('<TARGET>','').replace('</TARGET>','')==text
        assert state['marked_passage'].count('<TARGET>read</TARGET>')==1
        states.append(state)
    assert states[0]['marked_passage'].startswith('They have <TARGET>read</TARGET>')
    assert states[1]['marked_passage'].startswith('They have read it, and they will <TARGET>read</TARGET>')
    assert states[0]!=states[1]


def test_narrow_context_keeps_target_and_escapes_source_target_markers():
    text='A different sentence. Yesterday I read it, and tomorrow I will read it. <TARGET>live</TARGET>'
    start=text.index('read');o={'word':'read','text':text,'start':start,'end':start+4}
    narrow=annotations.payload(o,narrow=True)['state']['marked_passage']
    assert narrow==' Yesterday I <TARGET>read</TARGET> it'
    wide=annotations.payload(o)['state']['marked_passage']
    assert wide.count('<TARGET>')==1 and '&lt;TARGET&gt;live&lt;/TARGET&gt;' in wide


def test_model_disagreement_has_no_recommendation_and_low_uniqueness_is_retained(library,monkeypatch):
    text='We have read it, and will read it again.';start=text.index('read')
    o={'word':'read','text':text,'start':start,'end':start+4}
    def answer(payload):
        r=response(payload)
        if payload['state']['marked_passage'].endswith(' it'):
            r['answers']['decision'].update(choice='present',probabilities={'past':0,'present':1,'unknown':0})
        return r
    monkeypatch.setattr(jev_client,'transport',answer)
    result=annotations.propose(o,'test-owner')
    assert result['recommendation']['choice']=='unknown' and not result['recommendation']['contexts_agree']
    assert result['answer']['choice']=='past' and result['confirmation']['answer']['choice']=='present'
    assert result['new_cost']==.00002
    with store.db() as c:c.execute('DELETE FROM jev_calls')
    def uncertain(payload):
        r=response(payload);r['answers']['unique']['noul']=.89;return r
    monkeypatch.setattr(jev_client,'transport',uncertain)
    result=annotations.propose(o,'test-owner')
    assert result['recommendation']['contexts_agree'] and result['recommendation']['choice']=='past'
    assert not result['recommendation']['display_qualified']


def test_identical_contexts_use_one_request_and_cache_reuse_has_zero_new_cost(library,monkeypatch):
    calls=[]
    def counted(payload):calls.append(payload);return response(payload)
    monkeypatch.setattr(jev_client,'transport',counted)
    o={'word':'read','text':'She will read tomorrow','start':9,'end':13}
    first=annotations.propose(o,'owner-one');second=annotations.propose(o,'owner-two')
    assert len(calls)==1 and first['new_cost']==.00001 and second['new_cost']==0
    assert first['request_key']==first['confirmation']['request_key']


def test_confirmation_failure_retains_billed_primary_without_inventing_a_suggestion(library,monkeypatch):
    book,options,_=library;calls=[]
    def fail_second(payload):
        calls.append(payload)
        if len(calls)==2:raise TimeoutError('fake timeout')
        return response(payload)
    monkeypatch.setattr(jev_client,'transport',fail_second)
    annotations.create(book,options,0);annotations.process(store.claim_next(),lambda jid:None)
    listing=annotations.listing(7)
    assert len(calls)==2 and listing['coverage']['billing_state']=='unreconciled'
    assert listing['coverage']['new_cost']==.00001 and listing['coverage']['checked']==0
    assert all(r['suggestion'] is None and r['state']=='pending' for r in listing['items'])
    assert '_occurrences' not in annotations.attach(options,book)


def test_uncertain_billing_blocks_resend_and_further_network_calls(library):
    def timeout(p):raise TimeoutError('deliberate fake error containing a secret that must not be saved')
    with pytest.raises(jev_client.Unavailable):jev_client.decide({'x':1},'owner',{},send=timeout)
    def forbidden(p):raise AssertionError('must not call the provider')
    for payload in ({'x':1},{'x':2}):
        with pytest.raises(jev_client.Unavailable):jev_client.decide(payload,'owner',{},send=forbidden)
    with store.db() as con:
        row=con.execute('SELECT * FROM jev_calls').fetchone()
    assert row['state']=='unknown' and 'secret' not in row['result']


def test_crashed_reservation_and_budget_ceiling_do_not_reissue(library):
    with store.db() as con:
        con.execute("INSERT INTO jev_calls VALUES ('pending','owner','sending',NULL,0,.01,0,0)")
    with pytest.raises(jev_client.Unavailable):jev_client.decide({'new':1},'owner',{},send=lambda p:1/0)
    with store.db() as con:
        con.execute('DELETE FROM jev_calls')
        con.execute("INSERT INTO jev_calls VALUES ('used','owner','complete','{}',.995,0,0,0)")
    with pytest.raises(jev_client.Unavailable,match='spending'):jev_client.decide({'new':1},'owner',{},send=lambda p:1/0)


def test_multiple_chapter_checks_share_the_book_budget(library):
    book,options,_=library
    first=store.create_job('analysis',book,options,'analysis-one')
    second=store.create_job('analysis',book,options,'analysis-two')
    with store.db() as con:
        con.execute("INSERT INTO jev_calls VALUES ('earlier',?,'complete','{}',.245,0,0,0)",(first['id'],))
    with pytest.raises(jev_client.Unavailable,match='book'):
        jev_client.decide({'new':1},second['id'],{},send=lambda p:1/0)


def test_review_never_autoapproves_and_queue_keeps_approved_snapshot(library,monkeypatch):
    book,options,source=library
    monkeypatch.setattr(jev_client,'transport',response)
    request=annotations.create(book,options,0)
    assert annotations.create(book,options,0)['id']==request['id']
    annotations.process(store.claim_next(),lambda jid:None)
    rows=annotations.listing(book['id'])['items']
    assert len(rows)==2 and all(r['state']=='pending' for r in rows)
    assert '_occurrences' not in annotations.attach(options,book)
    first=rows[0]
    approved=annotations.review(first['id'],first['revision'],'past')
    snapshot=annotations.attach(options,book)
    assert len(snapshot['_occurrences'])==1
    queued=store.create_job('book',book,snapshot,speech.cache_key(7,snapshot,options['source_sha256']))
    annotations.review(first['id'],approved['revision'],None)
    assert '_occurrences' not in annotations.attach(options,book)
    assert store.get_job(queued['id'],public=True)['options']['_occurrences'][0]['choice']=='past'
    with pytest.raises(ValueError,match='changed'):annotations.review(first['id'],first['revision'],'present')
    with store.db() as con:assert con.execute('SELECT count(*) FROM pronunciation_review_events').fetchone()[0]==2


def test_changed_source_and_arbitrary_phone_choice_are_rejected(library,monkeypatch):
    book,options,source=library
    monkeypatch.setattr(jev_client,'transport',response)
    annotations.create(book,options,0);annotations.process(store.claim_next(),lambda jid:None)
    row=annotations.listing(7)['items'][0]
    with pytest.raises(ValueError,match='candidate'):annotations.review(row['id'],row['revision'],'random IPA')
    source.write_bytes(source.read_bytes()+b'changed')
    with pytest.raises(ValueError,match='changed'):annotations.review(row['id'],row['revision'],'past')


def test_parser_choices_have_separate_review_scope(library,monkeypatch):
    book,options,_=library
    monkeypatch.setattr(jev_client,'transport',response)
    annotations.create(book,options,0);annotations.process(store.claim_next(),lambda jid:None)
    row=annotations.listing(7)['items'][0];annotations.review(row['id'],row['revision'],'past')
    changed={**options,'skip_notes':False}
    scoped=annotations.listing(7,options=changed)
    assert not scoped['items'] and scoped['analysis'] is None and scoped['coverage'] is None
    assert '_occurrences' not in annotations.attach(changed,book)
    assert annotations.attach(options,book)['_occurrences']


def test_chapter_batches_resume_without_repeating_calls_or_overwriting_approvals(library,monkeypatch):
    book,options,_=library
    monkeypatch.setattr(annotations,'MAX_CHECKS',1)
    calls=[]
    def counted(payload):calls.append(payload);return response(payload)
    monkeypatch.setattr(jev_client,'transport',counted)
    first=annotations.create(book,options,0)
    annotations.process(store.claim_next(),lambda jid:None)
    listing=annotations.listing(7,0)
    assert listing['coverage']['found']==2 and listing['coverage']['next_offset']==1
    row=listing['items'][0];annotations.review(row['id'],row['revision'],'past')
    second=annotations.create(book,options,0,offset=1)
    assert first['id']!=second['id']
    annotations.process(store.claim_next(),lambda jid:None)
    listing=annotations.listing(7,0)
    assert len(listing['items'])==2 and listing['items'][0]['state']=='approved'
    assert listing['coverage']['offset']==1 and listing['coverage']['next_offset'] is None
    assert len(calls)==4
    assert annotations.create(book,options,0,1)['id']==second['id']
    assert store.claim_next() is None
    assert len(annotations.attach(options,book)['_occurrences'])==1
    with pytest.raises(ValueError,match='offset'):annotations.create(book,options,0,-1)
    beyond=annotations.create(book,options,0,2)
    with pytest.raises(ValueError,match='no further'):annotations.process(store.claim_next(),lambda jid:None)
    assert len(calls)==4


def test_parser_upgrade_excludes_old_reviews_and_cannot_reuse_analysis_identity(library,monkeypatch):
    book,options,_=library
    monkeypatch.setattr(jev_client,'transport',response)
    previous=annotations.create(book,options,0)
    annotations.process(store.claim_next(),lambda jid:None)
    row=annotations.listing(7)['items'][0]
    monkeypatch.setattr(annotations,'EXTRACTOR_VERSION',str(int(annotations.EXTRACTOR_VERSION)+1))
    listing=annotations.listing(7)
    assert listing['items']==[] and listing['analysis'] is None and listing['coverage'] is None
    with pytest.raises(ValueError,match='parser changed'):annotations.review(row['id'],row['revision'],'past')
    assert annotations.create(book,options,0)['id']!=previous['id']


def test_legacy_analysis_requires_retained_matching_parser_plan(library,monkeypatch):
    book,options,_=library
    monkeypatch.setattr(jev_client,'transport',response)
    previous=annotations.create(book,options,0)
    annotations.process(store.claim_next(),lambda jid:None)
    old=json.loads(store.get_job(previous['id'])['options']);old['_analysis'].pop('extractor')
    with store.db() as c:c.execute('UPDATE jobs SET options=? WHERE id=?',(json.dumps(old),previous['id']))
    assert annotations.listing(7)['analysis']['id']==previous['id']
    path=annotations.WORK/previous['id']/'reading-plan.json'
    plan=json.loads(path.read_text());plan['extractor']=str(int(plan['extractor'])-1);path.write_text(json.dumps(plan))
    assert annotations.listing(7)['analysis'] is None


def test_provider_failure_returns_partial_review_without_blocking_conversions(library,monkeypatch):
    book,options,_=library
    def fail(p):raise TimeoutError()
    monkeypatch.setattr(jev_client,'transport',fail)
    request=annotations.create(book,options,0)
    annotations.process(store.claim_next(),lambda jid:None)
    result=annotations.listing(7)
    assert result['analysis']['status']=='ready'
    assert result['coverage']['billing_state']=='unreconciled'
    assert len(result['items'])==2 and all(r['suggestion'] is None for r in result['items'])
    assert '_occurrences' not in annotations.attach(options,book)
    assert store.create_job('book',book,options,'baseline')['status']=='queued'


def test_analysis_is_served_between_chunks_of_a_running_book(library,monkeypatch):
    book,options,_=library
    monkeypatch.setattr(jev_client,'transport',response)
    parent=store.create_job('book',book,options,'book-running');store.claim_next()
    request=annotations.create(book,options,0)
    worker.STOP.clear();worker.service_preview()
    assert store.get_job(request['id'])['status']=='ready'
    assert store.get_job(parent['id'])['status']=='running'


def test_book_worker_keeps_approved_occurrence_provenance_in_plan_and_delivery(library,monkeypatch,tmp_path):
    import shutil
    import numpy as np
    from kokoro_onnx.tokenizer import Tokenizer
    if not shutil.which('ffmpeg'):pytest.skip('real MP3 encoding required')
    book,options,source=library
    monkeypatch.setattr(jev_client,'transport',response)
    annotations.create(book,options,0);annotations.process(store.claim_next(),lambda jid:None)
    row=annotations.listing(7)['items'][0];annotations.review(row['id'],row['revision'],'past')
    snapshot=annotations.attach(options,book)
    config={**worker.CONFIG,'output_root':str(tmp_path/'final'),'staging_root':str(tmp_path/'stage')}
    monkeypatch.setattr(worker,'CONFIG',config)
    monkeypatch.setattr(catalogue,'get',lambda *a,**k:book)
    monkeypatch.setattr(catalogue,'cover_path',lambda bid:None)
    monkeypatch.setattr(worker.plex_delivery,'scan',lambda relative:None)
    monkeypatch.setattr(worker,'service_preview',lambda:None)
    seen=[]
    class Engine:
        def __init__(self):self.tokenizer=Tokenizer()
        def create(self,text,**kwargs):
            seen.append((text,kwargs));return np.ones(4800,dtype=np.float32)*.1,24000
    monkeypatch.setattr(speech,'load',Engine)
    request=store.create_job('book',book,snapshot,speech.cache_key(7,snapshot,options['source_sha256']))
    worker.STOP.clear();worker.process_book(store.claim_next())
    finished=store.get_job(request['id']);assert finished['status']=='awaiting_plex'
    plan=json.loads((annotations.WORK/request['id']/'reading-plan.json').read_text(encoding='utf-8'))
    manifest=json.loads((Path(finished['output_path'])/'booksmith.json').read_text(encoding='utf-8'))
    assert plan['profile']['context_pronunciations']['occurrences'][0]['choice']=='past'
    outcomes=[o for c in manifest['render_index']['chapters'] for p in c['chunks'] for o in p.get('pronunciations',[])]
    assert outcomes==[{'id':row['id'],'status':'applied'}]
    assert any(kwargs.get('is_phonemes') for _,kwargs in seen)
    assert retention.digest(source)==options['source_sha256']


def test_review_api_requires_auth_csrf_and_preserves_explicit_approval_boundary(library,monkeypatch):
    import app
    from access import AccessControl
    from fastapi.testclient import TestClient
    book,options,_=library
    monkeypatch.setattr(catalogue,'get',lambda *a,**k:book)
    monkeypatch.setattr(catalogue,'sync',lambda *a,**k:None)
    monkeypatch.setattr(worker,'start',lambda:None);monkeypatch.setattr(worker,'stop',lambda:None)
    monkeypatch.setattr(jev_client,'available',lambda:True);monkeypatch.setattr(jev_client,'transport',response)
    monkeypatch.setattr(app,'AUTH',AccessControl(settings={'username':'family','salt':'aa'*16,'iterations':1000,'password_hash':'ff'*32,'gateway_token':'test'}))
    with TestClient(app.app,base_url='http://localhost:8787',client=('127.0.0.1',50000)) as client:
        assert client.get('/api/books/7/context-pronunciations').status_code==401
        monkeypatch.setattr(app,'AUTH',AccessControl(settings={}))
        headers={'X-Booksmith-Token':client.get('/api/config').json()['csrf']}
        assert client.post('/api/books/7/context-pronunciations',json={}).status_code==403
        assert client.post('/api/books/7/context-pronunciations',json={'voice':'am_eric'},headers=headers).status_code==202
        worker.run_job(store.claim_next())
        row=client.get('/api/books/7/context-pronunciations').json()['items'][0]
        assert row['state']=='pending'
        body={'expected_revision':row['revision'],'choice':'past'}
        assert client.post(f"/api/pronunciation-reviews/{row['id']}/decision",json=body).status_code==403
        approved=client.post(f"/api/pronunciation-reviews/{row['id']}/decision",json=body,headers=headers)
        assert approved.status_code==200
        request=client.post('/api/jobs',json={'book_id':7},headers=headers)
        assert request.status_code==202 and request.json()['options']['_occurrences'][0]['choice']=='past'
        baseline=client.post('/api/jobs',json={'book_id':7,'use_reviewed_pronunciations':False},headers=headers)
        assert baseline.status_code==202 and '_occurrences' not in baseline.json()['options']
        assert client.post('/api/jobs',json={'book_id':7,'_occurrences':[{'choice':'evil'}]},headers=headers).status_code==422
        preview=client.post(f"/api/pronunciation-reviews/{row['id']}/preview",json={
            'expected_revision':approved.json()['revision'],'choice':'past','voice':'am_eric'},headers=headers)
        assert preview.status_code==202 and preview.json()['options']['_occurrence_preview']['choice']=='past'
