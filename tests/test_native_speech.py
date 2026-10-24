import base64
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

import native_speech
import speech
from speech_engines import native_descriptor
import store
import worker


def options(native=False):
    result={'voice':'am_eric','style':'natural','pace':1,'skip_notes':True,'skip_frontmatter':True}
    if native:result['_engine']=native_descriptor(speech.CONFIG['model_sha256'],speech.CONFIG['voices_sha256'])
    return result


def test_existing_cpu_edition_key_and_profile_survive_gpu_default(monkeypatch):
    opts=options()
    old={'book':7,'options':opts,'engine':speech.ENGINE_VERSION,'model':speech.CONFIG['model_sha256'],
         'voices':speech.CONFIG['voices_sha256'],'source':'source','extractor':speech.EXTRACTOR_VERSION}
    expected=hashlib.sha256(json.dumps(old,sort_keys=True).encode()).hexdigest()
    original=speech.profile_snapshot(opts)
    monkeypatch.setitem(speech.CONFIG,'speech_backend','native_cuda')
    monkeypatch.setattr(native_speech,'available',lambda:True)
    assert speech.cache_key(7,opts,'source')==expected
    assert speech.profile_snapshot(opts)==original
    assert speech.default_engine()==options(True)['_engine']
    assert speech.cache_key(7,options(True),'source')!=expected


def test_only_new_jobs_fall_back_to_cpu_at_admission(monkeypatch):
    monkeypatch.setitem(speech.CONFIG,'speech_backend','native_cuda')
    monkeypatch.setattr(native_speech,'available',lambda:False)
    assert speech.default_engine()==speech.cpu_descriptor()
    selected=options(True)
    assert speech.engine_descriptor(selected)==selected['_engine']
    selected['_engine']['precision']='fp16'
    with pytest.raises(ValueError,match='original speech engine'):speech.engine_descriptor(selected)


def test_native_profile_records_model_precision_and_frontend():
    opts=options(True);profile=speech.profile_snapshot(opts)
    assert profile['renderer']==opts['_engine']
    assert profile['model_sha256']==opts['_engine']['model']
    assert profile['renderer']['frontend']==speech.ENGINE_VERSION
    assert profile['renderer']['precision']=='fp32-tf32'
    assert profile['engine']['direction'] is False


def test_cpu_and_gpu_editions_do_not_merge_into_one_plex_album():
    job={'title':'Story','author':'Author','edition_key':'a'*64}
    original,_=worker.output_names(job,options())
    native,_=worker.output_names({**job,'edition_key':'b'*64},options(True))
    assert original!=native and 'bbbbbb' in native
    assert 'aaaaaa' not in original


def test_gpu_path_never_calls_cpu_inference_and_validates_resume(tmp_path,monkeypatch):
    class Frontend:
        def create(self,*a,**kw):pytest.fail('CPU inference would mix this GPU edition.')
    seen=[]
    def render(frontend,text,args,engine):
        seen.append((text,args,engine));return np.ones(4800,dtype=np.float32)*.1,24000
    monkeypatch.setattr(speech,'load',Frontend)
    monkeypatch.setattr(native_speech,'render',render)
    opts=options(True);opts['_pause_seconds']=.35
    path=tmp_path/'sample.wav'
    duration,_=speech.generate('An unchanged passage.',opts,path)
    assert duration==pytest.approx(.55)
    assert seen[0][0]=='An unchanged passage.'
    assert speech.validate_cached('An unchanged passage.',opts,path) is None
    with pytest.raises(ValueError,match='changed'):speech.validate_cached('Different passage.',opts,path)
    with pytest.raises(ValueError,match='changed'):speech.validate_cached('An unchanged passage.',{**opts,'_pause_seconds':0},path)
    sf.write(path,np.ones(13200,dtype=np.float32)*.2,24000)
    with pytest.raises(ValueError,match='changed'):speech.validate_cached('An unchanged passage.',opts,path)


def test_missing_native_receipt_cannot_reuse_cpu_audio(tmp_path):
    path=tmp_path/'sample.wav';sf.write(path,np.ones(4800)*.1,24000)
    with pytest.raises(FileNotFoundError):speech.validate_cached('An unchanged passage.',options(True),path)
    assert speech.validate_cached('An unchanged passage.',options(),path) is None


def test_native_outage_cannot_fall_through_to_cpu(tmp_path,monkeypatch):
    monkeypatch.setattr(speech,'load',lambda:object())
    def busy(*args):raise native_speech.NativeUnavailable('GPU busy')
    monkeypatch.setattr(native_speech,'render',busy)
    path=tmp_path/'sample.wav'
    with pytest.raises(native_speech.NativeUnavailable):speech.generate('Keep this edition consistent.',options(True),path)
    assert not path.exists()


def test_native_resource_pause_returns_same_job_to_queue(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DB_PATH',tmp_path/'queue.sqlite')
    monkeypatch.setattr(store,'prepare_dirs',lambda:None);store.init()
    queued=store.create_job('book',{'id':7,'title':'Story','author':'Author'},options(True),'native-key')
    job=store.claim_next();waits=[]
    def busy(_):raise native_speech.NativeUnavailable('Waiting for GPU resources')
    monkeypatch.setattr(worker,'process_book',busy)
    monkeypatch.setattr(native_speech,'available',lambda:True)
    monkeypatch.setattr(worker.STOP,'wait',waits.append)
    worker.STOP.clear();worker.run_job(job)
    after=store.get_job(queued['id'])
    assert after['status']=='queued' and 'GPU' in after['message']
    assert after['options']==job['options'] and after['edition_key']=='native-key'
    assert waits==[10]


def test_stopped_gpu_waits_before_rebuilding_cached_chapters(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DB_PATH',tmp_path/'queue.sqlite')
    monkeypatch.setattr(store,'prepare_dirs',lambda:None);store.init()
    job=store.create_job('book',{'id':7,'title':'Story','author':'Author'},options(True),'native-key')
    store.update_job(job['id'],done_chunks=12,done_chapters=1)
    job=store.claim_next();waits=[]
    monkeypatch.setattr(native_speech,'available',lambda:False)
    monkeypatch.setattr(worker,'process_book',lambda _:pytest.fail('Paused GPU work must not re-encode cached chapters.'))
    monkeypatch.setattr(worker.STOP,'wait',waits.append)
    worker.STOP.clear();worker.run_job(job)
    after=store.get_job(job['id'])
    assert after['status']=='queued' and after['done_chunks']==12 and after['done_chapters']==1
    assert after['options']==job['options'] and waits==[10]


def test_phone_adapter_retains_exact_batches_and_timing_based_pauses(monkeypatch):
    from kokoro_onnx.trim import trim
    from kokoro_onnx.sliding import token_edges,timings
    from kokoro_onnx.pauses import insert
    phones='a.'
    waveform=np.concatenate([np.zeros(1200),np.sin(np.arange(7200)*.03)*.1,np.zeros(2400)]).astype(np.float32)
    durations=[2,7,7,2]
    engine=options(True)['_engine']
    class Frontend:
        has_timings=True
        class Tokenizer:vocab={'a':1,'.':2}
        tokenizer=Tokenizer()
        def _prepare(self,*args):return None,phones,[(phones,.2),(phones,0)]
    captured=[]
    def call(endpoint,payload):
        captured.append(payload)
        return {'engine':engine,'sample_rate':24000,'rows':[
            {'audio':base64.b64encode(waveform.astype('<f4').tobytes()).decode(),'duration':durations} for _ in range(2)]}
    monkeypatch.setattr(native_speech,'frigate_available',lambda:True)
    monkeypatch.setattr(native_speech,'call',call)
    actual,rate=native_speech.render(Frontend(),'original',{'voice':'am_eric','speed':1,'lang':'en-us'},engine)
    parts=[];spoken=[];offset=0
    for pause in [.2,0]:
        edges=token_edges(np.array(durations),len(waveform))[1:]
        audio,(head,_)=trim(waveform.copy());edges=np.clip(edges-head,0,len(audio))
        if pause:audio=np.concatenate([audio,np.zeros(int(pause*24000),dtype=np.float32)])
        parts.append(audio);spoken+=timings(phones,edges+offset,24000);offset+=len(audio)
    expected,_=insert(np.concatenate(parts),spoken,24000,.25,.1)
    np.testing.assert_array_equal(actual,expected)
    assert rate==24000 and captured[0]['batches']==[phones,phones]
    assert 'original' not in json.dumps(captured)
    timed_audio,timed_rate,spoken=native_speech.render(Frontend(),'original',{'voice':'am_eric','speed':1,'lang':'en-us'},engine,return_timings=True)
    np.testing.assert_array_equal(timed_audio,actual)
    assert timed_rate==rate and [t.phoneme for t in spoken]==list(phones+phones)


@pytest.mark.parametrize('change',[{'engine':{}},{'sample_rate':22050},{'rows':[]}])
def test_phone_adapter_rejects_response_identity_and_missing_batches(monkeypatch,change):
    class Frontend:
        has_timings=True
        class Tokenizer:vocab={'a':1}
        tokenizer=Tokenizer()
        def _prepare(self,*args):return None,'a',[('a',0)]
    engine=options(True)['_engine']
    response={'engine':engine,'sample_rate':24000,'rows':[{}],**change}
    monkeypatch.setattr(native_speech,'frigate_available',lambda:True)
    monkeypatch.setattr(native_speech,'call',lambda *a:response)
    with pytest.raises(ValueError,match='identity or batch'):native_speech.render(Frontend(),'a',{'voice':'am_eric','speed':1,'lang':'en-us'},engine)


def test_connection_rejects_nonlocal_speech_destination(tmp_path,monkeypatch):
    path=tmp_path/'client.json';path.write_text(json.dumps({'url':'https://example.org','token':'unused'}))
    monkeypatch.setitem(native_speech.CONFIG,'native_connection_file',str(path))
    with pytest.raises(ValueError,match='loopback'):native_speech.connection()


@pytest.mark.parametrize('status,expected',[(503,native_speech.NativeUnavailable),(401,native_speech.NativeUnavailable),(500,native_speech.NativeUnavailable),(422,ValueError)])
def test_transport_distinguishes_busy_resources_from_invalid_input(monkeypatch,status,expected):
    class Response:status_code=status
    class Session:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def request(self,*args,**kwargs):return Response()
    monkeypatch.setattr(native_speech,'connection',lambda:{'url':'http://127.0.0.1:18790','token':'test-only'})
    monkeypatch.setattr(native_speech.requests,'Session',Session)
    with pytest.raises(expected):native_speech.call('/render',{})
