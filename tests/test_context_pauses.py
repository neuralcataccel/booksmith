import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

import context_pauses as cp
import reading_plan
import speech
import store
from test_repair import parent_edition


def options():
    return cp.attach({'voice':'am_eric','style':'natural','pace':1,'skip_notes':True,'skip_frontmatter':True})


def test_admission_changes_only_new_edition_identity_and_profile():
    old=options();old.pop('_pause_policy');original=speech.cache_key(1,old,'source')
    new=cp.attach(old)
    assert '_pause_policy' not in old
    assert speech.cache_key(1,new,'source')!=original
    assert speech.profile_snapshot(new)['contextual_pauses']==cp.POLICY
    assert 'contextual_pauses' not in speech.profile_snapshot(old)
    assert cp.attach(old,False)==old


def test_sentence_boundaries_keep_names_and_quoted_attribution_together():
    text='Dr. Jones asked, "Ready?" Maya replied. Then she waited.'
    units=[text[a:b].strip() for a,b in cp.sentence_ranges(text)]
    assert units==['Dr. Jones asked, "Ready?" Maya replied.','Then she waited.']
    span={'id':'x','text':text}
    assert all(text[r['punctuation_offset']-2:r['punctuation_offset']+1]!='Dr.' for r in cp.boundaries(span))


def test_unsafe_or_already_sufficient_gaps_collapse_to_one_baseline_plan():
    rows=[{'id':'x'}];d={'x':{'answer':{'choice':'reflect','confidence':1}}}
    for gap in [{'status':'no_safe_quiet_gap','existing_ms':0},{'status':'mapped','existing_ms':2000}]:
        plans=cp.realizable(cp.plans(rows,d),{'x':gap})
        assert len(plans)==1 and next(iter(plans.values()))['x']['target_ms']==0


def test_paragraph_editor_receives_complete_plans_in_state_as_in_approved_audition():
    rows=[{'id':'x','offset':6}];decisions={'x':{'answer':{'choice':'reflect','confidence':1}}}
    gaps={'x':{'status':'mapped','existing_ms':250}}
    payload,additional,mapping=cp.review_payload({'text':'Think. Now go.'},rows,decisions,gaps,'','')
    assert set(payload['state']['paragraph_variants'])=={'unchanged','sentence_tight','sentence_roomier'}
    assert payload['state']['paragraph_variants']['sentence_tight'][0]['target_total_ms']==650
    assert payload['state']['marked_passages']['decision']=='Think.<PAUSE_TARGET/> Now go.'
    assert mapping=={'decision':'x'} and additional=={}


def test_capture_preserves_normal_speech_and_rejects_changed_cached_timing(tmp_path,monkeypatch):
    class Engine:
        def create_timed(self,*args,**kw):
            return np.ones(4800,dtype=np.float32)*.1,24000,[SimpleNamespace(phoneme='.',start=.18,end=.2)]
    monkeypatch.setattr(speech,'load',Engine)
    opts=options();path=tmp_path/'base.wav'
    speech.generate('A statement.',opts,path)
    assert speech.validate_cached('A statement.',opts,path) is None
    row=json.loads(path.with_suffix('.timings.json').read_text())
    assert row['timings'][0]['phoneme']=='.'
    row['input_sha256']='changed';path.with_suffix('.timings.json').write_text(json.dumps(row))
    with pytest.raises(ValueError,match='pause timing'):speech.validate_cached('A statement.',opts,path)


@pytest.fixture
def chapter(tmp_path):
    root=tmp_path/'job'/'audio-v7'/'chapter-0001';root.mkdir(parents=True)
    chunks=['Think about that. Now continue.','A second paragraph follows.']
    blocks=[{'text':text,'kind':'p','locator':{'i':i},'chunk_start':i,'chunk_count':1} for i,text in enumerate(chunks)]
    plan=reading_plan.build({'version':'7','chapters':[{'title':'Test','chunks':chunks,'blocks':blocks}]},'a'*64,{})
    waves=[];index={'chunks':[]}
    for i,text in enumerate(chunks):
        path=root/f'{i:05}.wav';sf.write(path,np.ones(12000,dtype=np.float32)*.1,24000)
        row={'input_sha256':cp.digest({'text':text,'policy':cp.POLICY}),'wave_sha256':cp.file_hash(path),'timings':[]}
        row['sha256']=cp.digest(row);cp.write(path.with_suffix('.timings.json'),row)
        waves.append(path);index['chunks'].append({'start_seconds':i*.5,'duration':.5})
    return plan,waves,index


def test_chapter_topup_applies_once_updates_offsets_and_resumes_without_new_calls(chapter,monkeypatch):
    plan,waves,index=chapter;calls=[]
    def mapping(audio,rate,timings,text,rows):
        return [{'id':r['id'],'status':'mapped','sample':4000+i*2000,'existing_ms':250} for i,r in enumerate(rows)]
    def decide(payload,owner,choices,**kwargs):
        calls.append(payload)
        choice='reflect' if 'reflect' in choices else next(k for k,v in choices.items() if k!='unknown' and '650' in v)
        return {'answer':{'choice':choice,'confidence':.9},'request_key':cp.digest(payload),'cost':.0001,'model':'test',
                'additional_answers':{k:{'choice':choice,'confidence':.9} for k in kwargs.get('additional_choices',{})}}
    monkeypatch.setattr(cp,'map_boundaries',mapping);monkeypatch.setattr(cp.jev_client,'decide',decide)
    before=[cp.file_hash(p) for p in waves]
    out,result=cp.chapter({'id':'job'},plan,0,waves,index,options())
    assert result['inserted_samples']>0
    assert index['chunks'][1]['start_seconds']==index['chunks'][0]['duration']
    assert sum(c['duration'] for c in index['chunks'])==pytest.approx(result['audio_seconds'])
    assert [cp.file_hash(p) for p in waves]==before
    hashes=[cp.file_hash(p) for p in out];count=len(calls)
    out,again=cp.chapter({'id':'job'},plan,0,waves,index,options())
    assert len(calls)==count and result==again and hashes==[cp.file_hash(p) for p in out]
    assert (waves[0].parents[2]/'pause-plans'/'chapter-0001.json').is_file()


def test_outage_is_a_retained_explicit_baseline_not_a_failed_or_rebilled_book(chapter,monkeypatch):
    plan,waves,index=chapter;calls=[]
    monkeypatch.setattr(cp,'map_boundaries',lambda a,r,t,text,rows:[{'id':x['id'],'status':'no_safe_quiet_gap','existing_ms':0} for x in rows])
    def unavailable(*args):
        calls.append(1);raise cp.jev_client.Unavailable('unavailable')
    monkeypatch.setattr(cp.jev_client,'decide',unavailable)
    out,result=cp.chapter({'id':'job'},plan,0,waves,index,options())
    assert result['inserted_samples']==0 and any(p['status']=='annotation_unavailable_keep_baseline' for p in result['paragraphs'])
    assert [sf.read(p)[0].tolist() for p in out]==[sf.read(p)[0].tolist() for p in waves]
    count=len(calls);cp.chapter({'id':'job'},plan,0,waves,index,options());assert len(calls)==count


def test_monthly_budget_counts_current_month_and_retains_legacy_limits(tmp_path,monkeypatch):
    import time
    import jev_client
    monkeypatch.setattr(store,'DB_PATH',tmp_path/'store.sqlite');monkeypatch.setattr(store,'prepare_dirs',lambda:None);store.init()
    with store.db() as con:
        con.execute("INSERT INTO jev_calls VALUES ('old','old','complete','{}',9,0,0,0)")
    monkeypatch.setitem(jev_client.CONFIG,'jev_monthly_limit_usd',.02)
    monkeypatch.setitem(jev_client.CONFIG,'jev_owner_limit_usd',1.25)
    def response(payload):
        return {'model':jev_client.MODEL,'usage':{'cost':.015,'input_tokens':1},'answers':{}}
    # Billing above the conservative .01 reservation is invalid but retained; no retry.
    with pytest.raises(jev_client.Unavailable):jev_client.decide({'a':1},'new',{},send=response)
    with store.db() as con:con.execute("UPDATE jev_calls SET state='complete' WHERE owner='new'")
    with pytest.raises(jev_client.Unavailable,match='spending'):jev_client.decide({'a':2},'new',{},send=lambda _:pytest.fail('budget must prevent sending'))


def test_typed_paragraph_batch_validates_and_caches_every_answer(tmp_path,monkeypatch):
    import jev_client
    monkeypatch.setattr(store,'DB_PATH',tmp_path/'store.sqlite');monkeypatch.setattr(store,'prepare_dirs',lambda:None);store.init()
    calls=[]
    def send(payload):
        calls.append(1)
        def answer(choice):return {'choice':choice,'confidence':.9,'probabilities':{k:float(k==choice) for k in cp.CHOICES}}
        return {'model':jev_client.MODEL,'usage':{'cost':.0001,'input_tokens':100},
                'answers':{'decision':answer('keep'),'boundary_1':answer('scene'),'unique':{'noul':.9}}}
    first=jev_client.decide({'test':'batch'},'owner',cp.CHOICES,send=send,additional_choices={'boundary_1':cp.CHOICES})
    second=jev_client.decide({'test':'batch'},'owner',cp.CHOICES,send=send,additional_choices={'boundary_1':cp.CHOICES})
    assert first['additional_answers']['boundary_1']['choice']=='scene'
    assert second['additional_answers']==first['additional_answers'] and second['new_cost']==0 and len(calls)==1


def test_existing_worker_publishes_contextual_edition_and_retains_plans(parent_edition,monkeypatch):
    import worker
    import retention
    parent,_,output,generated,work=parent_edition
    old=retention.digest(output/'booksmith.json')
    original=speech.generate
    def timed(text,opts,path):
        result=original(text,opts,path)
        receipt={'input_sha256':cp.digest({'text':text,'policy':cp.POLICY}),
                 'wave_sha256':cp.file_hash(path),'timings':[]}
        receipt['sha256']=cp.digest(receipt);cp.write(path.with_suffix('.timings.json'),receipt)
        return result
    monkeypatch.setattr(speech,'generate',timed)
    monkeypatch.setattr(cp,'map_boundaries',lambda a,r,t,text,rows:[{'id':x['id'],'status':'no_safe_quiet_gap','existing_ms':0} for x in rows])
    def keep(payload,owner,choices):
        key='keep' if 'keep' in choices else next(k for k,v in choices.items() if k!='unknown' and 'Add no silence' in v)
        return {'answer':{'choice':key,'confidence':1},'model':'test','cost':0,'request_key':cp.digest(payload)}
    monkeypatch.setattr(cp.jev_client,'decide',keep)
    assemble=cp.chapter
    def with_pause(*args,**kwargs):
        paths,receipt=assemble(*args,**kwargs)
        data,rate=sf.read(paths[-1],dtype='float32')
        sf.write(paths[-1],np.concatenate([data,np.zeros(12000,dtype=np.float32)]),rate,subtype='PCM_16')
        args[4]['chunks'][-1]['duration']+=.5
        receipt['audio_seconds']+=.5;receipt['inserted_samples']+=12000
        return paths,receipt
    monkeypatch.setattr(cp,'chapter',with_pause)
    opts=cp.attach(json.loads(parent['options']))
    request=store.create_job('book',{'id':1,'title':parent['title'],'author':parent['author']},opts,speech.cache_key(1,opts,opts['source_sha256']))
    worker.process_book(store.claim_next())
    result=store.get_job(request['id']);assert result['status']=='awaiting_plex'
    manifest=json.loads((Path(result['output_path'])/'booksmith.json').read_text(encoding='utf-8'))
    assert manifest['contextual_pauses']==cp.POLICY
    assert result['audio_seconds']==pytest.approx(manifest['audio_seconds'])
    assert len(manifest['render_index']['chapters'])==2
    assert all('contextual_pauses' in c for c in manifest['render_index']['chapters'])
    assert len(list((work/request['id']/'pause-plans').glob('chapter-*.json')))==2
    assert retention.digest(output/'booksmith.json')==old
