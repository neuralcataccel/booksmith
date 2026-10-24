"""Isolated native Kokoro benchmark. Never writes jobs, Calibre, NAS or Plex.

Prepare/cpu/watch run on the host; acquire/native run in the dedicated container.
Inputs, downloaded weights, transcripts and audio stay in the private runtime root.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time


def digest_file(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for part in iter(lambda:f.read(1024*1024),b''):h.update(part)
    return h.hexdigest()


def write(path,value):
    path=Path(path);tmp=path.with_suffix('.tmp.json')
    tmp.write_text(json.dumps(value,indent=2,allow_nan=False),encoding='utf-8');tmp.replace(path)


def prepare(root,jid,chapter):
    import numpy as np
    import speech,reading_plan,store,retention,catalogue,occurrence_phones,pronunciation
    from settings import WORK
    job=store.get_job(jid);options=json.loads(job['options'])
    assert job['kind']=='book' and options['voice']=='am_eric'
    plan=reading_plan.validate(json.loads((WORK/jid/'reading-plan.json').read_text(encoding='utf-8')))
    assert retention.digest(catalogue.source_file(job['book_id']))==plan['source_sha256']
    k=speech.load()
    from settings import MODELS
    assert digest_file(MODELS/'kokoro-v1.0.onnx')==speech.CONFIG['model_sha256']
    assert digest_file(MODELS/'voices-v1.0.bin')==speech.CONFIG['voices_sha256']
    np.save(root/'eric.npy',k.get_voice_style(options['voice']))
    import kokoro_onnx.trim
    shutil.copyfile(kokoro_onnx.trim.__file__,root/'trim_reference.py')  # Original license preserved verbatim.
    reference=root/'bench_frontend';reference.mkdir(exist_ok=True)
    (reference/'__init__.py').write_text('')
    installed=Path(kokoro_onnx.trim.__file__).parent
    for name in ('trim.py','sliding.py','pauses.py','chunker.py','config.py','config.json'):
        shutil.copyfile(installed/name,reference/name)
    write(root/'reference-checksums.json',{p.name:digest_file(p) for p in reference.iterdir() if p.is_file()})
    rows=[];selected=plan['chapters'][chapter]
    for span in selected['spans']:
        for pi,piece in enumerate(span['pieces']):
            text=piece['text'];occ=occurrence_phones.chunk_occurrences(plan,span,piece,options.get('_occurrences',[]))
            spoken,mapped,skipped=pronunciation.remap(text,options.get('pronunciations',[]),occ)
            phones,outcomes=occurrence_phones.align(spoken,mapped,k.tokenizer) if mapped else (None,[])
            phones=' '.join((phones or k.tokenizer.phonemize(spoken,lang='en-us')).split())
            _,_,batches=k._prepare(phones,'am_eric',speech.effective_speed(options),'en-us',True,.25,.1)
            pause=speech.style_info(options['style'])['pause']
            if options.get('pause_mode')=='paragraph':pause=options['paragraph_pause'] if pi==len(span['pieces'])-1 else 0
            rows.append({'index':len(rows),'text':text,'spoken':spoken,'phones':phones,'batches':batches,
                         'span_id':span['id'],'piece':pi,'pause':pause,'pronunciations':skipped+outcomes,'category':'chapter'})
    if options.get('pause_mode')=='paragraph':rows[-1]['pause']=options['chapter_pause']
    fixtures=[('The lead will lead us home.','lead','metal',0),
              ('Yesterday she read it, and tomorrow she will read it again.','read','past',0),
              ('The bass swam under the boat. She played the bass in the orchestra.','bass','fish',0)]
    import re
    for text,word,choice,nth in fixtures:
        m=list(re.finditer(r'\b'+word+r'\b',text))[nth]
        phones,outcomes=occurrence_phones.align(text,[{'id':word,'word':word,'choice':choice,'start':m.start(),'end':m.end()}],k.tokenizer)
        assert outcomes[0]['status'] in {'applied','already_correct'}
        phones=phones or k.tokenizer.phonemize(text,lang='en-us')
        _,_,batches=k._prepare(phones,'am_eric',1,'en-us',True,.25,.1)
        rows.append({'index':len(rows),'text':text,'spoken':text,'phones':phones,'batches':batches,'pause':.22,
                     'pronunciations':outcomes,'category':'pronunciation_fixture'})
    definition={'schema':1,'job':jid,'chapter':chapter,'title':selected['title'],'source_sha256':plan['source_sha256'],
                'plan_sha256':plan['sha256'],'voice':'am_eric','speed':speech.effective_speed(options),'rows':rows,
                'onnx_model_sha256':speech.CONFIG['model_sha256'],'voices_file_sha256':speech.CONFIG['voices_sha256'],
                'eric_sha256':digest_file(root/'eric.npy'),'vocab':k.tokenizer.vocab,
                'has_timings':k.has_timings,'reference_modules':{p.name:digest_file(p) for p in reference.glob('*.py')},
                'trim_reference_sha256':digest_file(root/'trim_reference.py'),'options_sha256':hashlib.sha256(job['options'].encode()).hexdigest()}
    write(root/'input.json',definition)
    print(json.dumps({'chapter':chapter,'title':selected['title'],'chapter_pieces':len(rows)-len(fixtures),
                      'fixture_pieces':len(fixtures),'maximum_batch_phones':max(len(b[0]) for r in rows for b in r['batches']),
                      'voice_shape':list(np.load(root/'eric.npy').shape),'source_unchanged':True}),flush=True)


def acquire(root):
    from huggingface_hub import hf_hub_download
    acquisition=json.loads((root/'acquisition.json').read_text())
    files={}
    for name in ('config.json','kokoro-v1_0.pth'):
        path=Path(hf_hub_download(repo_id=acquisition['hf_repo'],filename=name,revision=acquisition['hf_revision']))
        files[name]={'path':str(path.relative_to(root)),'sha256':digest_file(path),'bytes':path.stat().st_size}
    write(root/'native-files.json',files)
    result=subprocess.run([sys.executable,'-m','pip','freeze'],capture_output=True,text=True,check=True)
    (root/'native-requirements.txt').write_text(result.stdout)
    print(json.dumps(files),flush=True)


def pack(root,name,rows):
    folder=root/name
    concat=folder/'concat.txt'
    concat.write_text('\n'.join("file '"+(folder/f"{r['index']:04}.wav").as_posix()+"'" for r in rows if r['category']=='chapter'))
    start=time.perf_counter()
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','concat','-safe','0','-i',str(concat),
                    '-c:a','libmp3lame','-b:a','96k','-ar','24000','-ac','1',str(folder/'chapter.mp3')],
                   check=True,capture_output=True,timeout=180,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    return time.perf_counter()-start


def cpu(root):
    import speech,numpy as np,soundfile as sf
    data=json.loads((root/'input.json').read_text());folder=root/'cpu';folder.mkdir(exist_ok=True)
    started=time.perf_counter();k=speech.load();load=time.perf_counter()-started
    assert k.tokenizer.vocab==data['vocab']
    # Same precomputed phoneme input; frontend preparation is excluded on both sides.
    k.create(data['rows'][1]['phones'],voice='am_eric',speed=data['speed'],is_phonemes=True)
    results=[];wall=time.perf_counter()
    for row in data['rows']:
        begin=time.perf_counter();audio,rate=k.create(row['phones'],voice='am_eric',speed=data['speed'],is_phonemes=True)
        audio=np.concatenate([audio,np.zeros(int(row['pause']*rate),dtype=np.float32)])
        sf.write(str(folder/f"{row['index']:04}.wav"),audio,rate,subtype='PCM_16')
        results.append({'index':row['index'],'category':row['category'],'seconds':time.perf_counter()-begin,'audio_seconds':len(audio)/rate})
        write(root/'cpu-progress.json',{'done':len(results),'total':len(data['rows'])})
    elapsed=time.perf_counter()-wall;encoding=pack(root,'cpu',data['rows'])
    chapter=[r for r in results if r['category']=='chapter']
    write(root/'cpu.json',{'state':'complete','load_seconds':load,'wall_seconds':elapsed,'encode_seconds':encoding,'rows':results,
                          'chapter_seconds':sum(r['seconds'] for r in chapter),'chapter_audio_seconds':sum(r['audio_seconds'] for r in chapter),
                          'input_sha256':digest_file(root/'input.json'),'provider':'CPUExecutionProvider','cpu_threads':speech.CONFIG['cpu_threads']})
    print('CPU chapter benchmark complete.',flush=True)


def native(root,mode,label=''):
    import torch,numpy as np,soundfile as sf
    from kokoro import KModel
    from loguru import logger
    logger.remove();assert mode in {'fp32','fp16'}
    torch.set_num_threads(4);torch.set_num_interop_threads(1)
    assert torch.cuda.is_available();torch.cuda.set_per_process_memory_fraction(.4)
    torch.backends.cuda.matmul.allow_tf32=True;torch.backends.cudnn.allow_tf32=True
    torch.backends.cudnn.benchmark=False
    data=json.loads((root/'input.json').read_text());files=json.loads((root/'native-files.json').read_text())
    for item in files.values():assert digest_file(root/item['path'])==item['sha256']
    assert digest_file(root/'eric.npy')==data['eric_sha256']
    assert digest_file(root/'trim_reference.py')==data['trim_reference_sha256']
    spec=importlib.util.spec_from_file_location('trim_reference',root/'trim_reference.py');trimmer=importlib.util.module_from_spec(spec);spec.loader.exec_module(trimmer)
    sys.path.insert(0,str(root))
    for name,expected in json.loads((root/'reference-checksums.json').read_text()).items():assert digest_file(root/'bench_frontend'/name)==expected
    from bench_frontend.sliding import token_edges,timings
    from bench_frontend.pauses import insert as insert_pauses
    for name,expected in data['reference_modules'].items():assert digest_file(root/'bench_frontend'/name)==expected
    start=time.perf_counter();model=KModel(repo_id='hexgrad/Kokoro-82M',config=str(root/files['config.json']['path']),model=str(root/files['kokoro-v1_0.pth']['path'])).eval().to('cuda')
    voice=torch.from_numpy(np.load(root/'eric.npy')).to('cuda');torch.cuda.synchronize();load=time.perf_counter()-start
    assert model.vocab==data['vocab'],'Native and ONNX vocabularies differ.'
    for row in data['rows']:
        for phones,_ in row['batches']:
            assert all(c in model.vocab for c in phones),'A source phone would be dropped.'
            assert len(phones)+2<=model.context_length
    name='native-'+mode+('-'+label if label else '');folder=root/name;folder.mkdir(exist_ok=True)
    def render(row):
        parts=[];spoken=[];offset=0
        for phones,pause in row['batches']:
            context=torch.autocast('cuda',dtype=torch.float16,enabled=mode=='fp16')
            with torch.inference_mode(),context:
                output=model(phones,voice[len(phones)-1],speed=data['speed'],return_output=True)
                a=output.audio.float().numpy()
            edges=token_edges(output.pred_dur.numpy(),len(a))[1:] if data['has_timings'] else None
            a,(head,_)=trimmer.trim(a)
            if edges is not None:edges=np.clip(edges-head,0,len(a))
            if pause:a=np.concatenate([a,np.zeros(int(pause*24000),dtype=np.float32)])
            parts.append(a)
            if edges is not None:spoken+=timings(phones,edges+offset,24000)
            offset+=len(a)
        audio,_=insert_pauses(np.concatenate(parts),spoken,24000,.25,.1)
        audio=np.concatenate([audio,np.zeros(int(row['pause']*24000),dtype=np.float32)])
        assert np.isfinite(audio).all() and np.max(np.abs(audio))>.0001
        return audio
    warm=time.perf_counter();render(data['rows'][1]);torch.cuda.synchronize();warm=time.perf_counter()-warm
    results=[];wall=time.perf_counter()
    for row in data['rows']:
        begin=time.perf_counter();audio=render(row);torch.cuda.synchronize()
        sf.write(str(folder/f"{row['index']:04}.wav"),audio,24000,subtype='PCM_16')
        results.append({'index':row['index'],'category':row['category'],'seconds':time.perf_counter()-begin,'audio_seconds':len(audio)/24000})
        write(root/(name+'-progress.json'),{'done':len(results),'total':len(data['rows'])})
    elapsed=time.perf_counter()-wall
    chapter=[r for r in results if r['category']=='chapter']
    write(root/(name+'.json'),{'state':'complete','mode':mode,'load_seconds':load,'warmup_seconds':warm,'wall_seconds':elapsed,
            'chapter_seconds':sum(r['seconds'] for r in chapter),'chapter_audio_seconds':sum(r['audio_seconds'] for r in chapter),
            'peak_allocated_mib':torch.cuda.max_memory_allocated()/2**20,'peak_reserved_mib':torch.cuda.max_memory_reserved()/2**20,
            'torch':torch.__version__,'cuda':torch.version.cuda,'rows':results,'input_sha256':digest_file(root/'input.json'),
            'tf32_enabled':True,'cudnn_benchmark':False,'cpu_threads':4,'allocator_fraction':.4,
            'same_vocab_and_eric':True,'production_changed':False})
    print(json.dumps({'state':'complete','mode':mode,'chapter_seconds':sum(r['seconds'] for r in chapter),'audio_seconds':sum(r['audio_seconds'] for r in chapter)}),flush=True)


def watch(root,container,mode,label=''):
    from benchmark_kokoro import gpu_state,frigate_state
    import store
    telemetry=[]
    def observe(stage):
        row={'time':time.time(),'stage':stage,'gpu':gpu_state(),'frigate':frigate_state()}
        with store.db() as c:row['active_books']=[dict(r) for r in c.execute("SELECT id,done_chunks,synthesis_seconds FROM jobs WHERE kind='book' AND status='running'")]
        telemetry.append(row);write(root/('telemetry-'+mode+('-'+label if label else '')+'.json'),telemetry);return row
    for _ in range(5):observe('before');time.sleep(2)
    if mode!='cpu':assert gpu_state()['free_mib']>=5000,'Insufficient GPU headroom; benchmark not started.'
    command=([sys.executable,str(Path(__file__).resolve()),'cpu','--root',str(root)] if mode=='cpu' else
             ['docker','exec',container,'python','/bench/benchmark_native_kokoro.py','native','--root','/bench','--mode',mode]+(['--label',label] if label else []))
    name='cpu' if mode=='cpu' else 'native-'+mode+('-'+label if label else '')
    with (root/(name+'.log')).open('w',encoding='utf-8') as log:
        process_start=time.perf_counter()
        child=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        deadline=time.monotonic()+600
        try:
            while child.poll() is None:
                row=observe(mode)
                if (mode!='cpu' and row['gpu']['free_mib']<1400) or row['frigate']['skipped_fps']>2:
                    raise RuntimeError('Resource guard stopped the isolated benchmark.')
                if time.monotonic()>deadline:raise TimeoutError('Isolated benchmark exceeded ten minutes.')
                time.sleep(1)
        except BaseException:
            if mode!='cpu':subprocess.run(['docker','stop','--time','3',container],capture_output=True,timeout=20)
            else:
                from quality import stop_tree
                stop_tree(child)
            raise
        finally:
            if child.poll() is None:child.terminate()
    assert child.returncode==0,'Native child failed; inspect isolated log.'
    process_seconds=time.perf_counter()-process_start
    for _ in range(5):observe('after');time.sleep(2)
    data=json.loads((root/'input.json').read_text());native_report=json.loads((root/(name+'.json')).read_text())
    native_report['child_process_seconds']=process_seconds
    if mode!='cpu':native_report['encode_seconds']=pack(root,name,data['rows'])
    write(root/(name+'.json'),native_report)
    print(json.dumps({'mode':mode,'chapter_seconds':native_report['chapter_seconds'],
                      'audio_per_second':native_report['chapter_audio_seconds']/native_report['chapter_seconds'],
                      'min_gpu_free_mib':min(r['gpu']['free_mib'] for r in telemetry),
                      'max_frigate_skipped_fps':max(r['frigate']['skipped_fps'] for r in telemetry)}),flush=True)


def asr_prepare(root,mode,label=''):
    data=json.loads((root/'input.json').read_text());name='cpu' if mode=='cpu' else 'native-'+mode+('-'+label if label else '')
    samples=[{'chapter':data['chapter']+1,'chunk':row['index'],'title':data['title'],'text':row['spoken'],
              'source_text':row['text'],'category':row['category'],'audio':f"{row['index']:04}.wav"} for row in data['rows']]
    assert all((root/name/r['audio']).is_file() for r in samples)
    write(root/name/'samples.json',samples)
    print(json.dumps({'mode':mode,'samples':len(samples)}),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['prepare','acquire','cpu','native','watch','asr-prepare'])
    p.add_argument('--root',type=Path,required=True);p.add_argument('--job');p.add_argument('--chapter',type=int,default=26)
    p.add_argument('--mode',choices=['cpu','fp32','fp16'],default='fp32');p.add_argument('--container',default='booksmith-native-kokoro-bench')
    p.add_argument('--label',choices=['repeat'],default='')
    a=p.parse_args();a.root.mkdir(exist_ok=True)
    if a.command=='prepare':prepare(a.root,a.job,a.chapter)
    elif a.command=='acquire':acquire(a.root)
    elif a.command=='cpu':cpu(a.root)
    elif a.command=='native':native(a.root,a.mode,a.label)
    elif a.command=='asr-prepare':asr_prepare(a.root,a.mode,a.label)
    else:watch(a.root,a.container,a.mode,a.label)
