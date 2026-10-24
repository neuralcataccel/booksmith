"""Isolated CPU-only Qwen direction audition. Never changes Booksmith's renderer."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

TEXT = ('When she reached the cottage, Red Riding Hood stopped. '
        '"Grandmother, what a strange voice you have," she said. The room was very still.')
DIRECTIONS = {
    'neutral': 'Read clearly in a neutral, natural audiobook narration voice.',
    'restrained': 'Read as a restrained audiobook narrator. Keep the voice consistent. Convey quiet concern and curiosity subtly, without theatrical acting or whispering.'}
UPSTREAM_SHA = '022e286b98fbec7e1e916cb940cdf532cd9f488e'
MODEL_REVISION = '0c0e3051f131929182e2c023b9537f8b1c68adfe'


def part_identity(text, direction, index):
    return hashlib.sha256(json.dumps([text, direction, index, 42 + index, MODEL_REVISION]).encode()).hexdigest()


def child(args):
    os.environ['HF_HUB_OFFLINE']='1'
    os.environ['TRANSFORMERS_OFFLINE']='1'
    os.environ['OMP_NUM_THREADS']=str(args.threads)
    sys.path.insert(0,str(args.upstream))
    import torch
    import numpy as np
    import soundfile as sf
    from qwen_tts import Qwen3TTSModel
    torch.set_num_threads(args.threads)
    torch.set_num_interop_threads(1)
    torch.manual_seed(42)
    aid='qwen-red-riding' if args.chapter_plan else 'qwen-'+args.mode
    texts=[TEXT]
    source_plan=None
    if args.chapter_plan:
        source_plan=json.loads(args.chapter_plan.read_text(encoding='utf-8'))
        # The CLI reads only a previously verified Booksmith plan, never a model-written script.
        from reading_plan import validate
        validate(source_plan)
        if len(source_plan['chapters'])!=1:raise ValueError('Choose a single-story plan for this audition.')
        texts=[p['text'] for s in source_plan['chapters'][0]['spans'] for p in s['pieces']]
    t=time.perf_counter()
    model=Qwen3TTSModel.from_pretrained(str(args.model),device_map='cpu',dtype=torch.bfloat16,
                                      attn_implementation='sdpa',local_files_only=True)
    loaded=time.perf_counter()-t
    args.output.mkdir(parents=True,exist_ok=True)
    parts=args.output/(aid+'-parts');parts.mkdir(exist_ok=True)
    arrays=[];elapsed=0
    for i,text in enumerate(texts):
        identity=part_identity(text, DIRECTIONS[args.mode], i)
        wav=parts/f'{i:04}.wav';receipt=parts/f'{i:04}.json'
        if args.chapter_plan and wav.is_file() and receipt.is_file():
            prior=json.loads(receipt.read_text(encoding='utf-8'))
            if prior['identity']!=identity:raise ValueError('Audition input changed; use a new output directory.')
            array,rate=sf.read(wav,dtype='float32');seconds=prior['seconds']
        else:
            torch.manual_seed(42+i);t=time.perf_counter()
            wavs,rate=model.generate_custom_voice(text=text,language='English',speaker='Ryan',
                instruct=DIRECTIONS[args.mode],non_streaming_mode=True,max_new_tokens=512,do_sample=False)
            seconds=time.perf_counter()-t;array=np.asarray(wavs[0])
            if not len(array) or not np.isfinite(array).all() or np.max(np.abs(array))<.0001:raise ValueError('Invalid audition part')
            temp=wav.with_suffix('.tmp.wav');sf.write(temp,array,rate);temp.replace(wav)
            receipt.write_text(json.dumps({'identity':identity,'seconds':seconds,'audio_seconds':len(array)/rate}),encoding='utf-8')
        arrays.append(array);elapsed+=seconds
        if args.chapter_plan:print(json.dumps({'completed':i+1,'total':len(texts),'audio_seconds':sum(len(a) for a in arrays)/rate}),flush=True)
    audio=np.concatenate(arrays)
    if not len(audio) or not np.isfinite(audio).all() or np.max(np.abs(audio))<.0001:
        raise ValueError('Invalid audition audio')
    args.output.mkdir(parents=True,exist_ok=True)
    target=args.output/(aid+'.wav')
    temporary=target.with_suffix('.tmp.wav');sf.write(temporary,audio,rate);temporary.replace(target)
    report={'id':aid,'text':TEXT if not source_plan else source_plan['chapters'][0]['title'],'direction':DIRECTIONS[args.mode],
            'speaker':'Ryan','engine':'Qwen3-TTS 1.7B CustomVoice','device':'cpu','dtype':'bfloat16',
            'threads':args.threads,'seed':42,'do_sample':False,'max_new_tokens':512,
            'sample_rate':rate,'audio_seconds':len(audio)/rate,'load_seconds':loaded,
            'synthesis_seconds':elapsed,'audio_per_wall_second':len(audio)/rate/elapsed}
    if source_plan:report.update(source_plan_sha256=source_plan['sha256'],source_sha256=source_plan['source_sha256'],chunks=len(texts),kind='story')
    (args.output/(aid+'.json')).write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report),flush=True)


def run(args):
    # This script runs in the separate expressive environment (torch CPU wheel + psutil).
    import psutil
    args.output.mkdir(parents=True,exist_ok=True)
    source=json.loads((args.model/'booksmith-model-source.json').read_text(encoding='utf-8'))
    if source['revision'] != MODEL_REVISION:
        raise ValueError('Unexpected model revision')
    sha=subprocess.check_output(['git','-C',str(args.upstream),'rev-parse','HEAD'],text=True).strip()
    if sha != UPSTREAM_SHA:
        raise ValueError('Unexpected Qwen source revision')
    provenance={'upstream_sha':sha,'model_revision':source['revision'],'model_license':source['license']}
    # Recheck pinned large model bytes once before any import/inference.
    for row in source['files']:
        expected=row.get('lfs',{}).get('sha256')
        if expected:
            with (args.model/row['rfilename']).open('rb') as stream:
                if hashlib.file_digest(stream,'sha256').hexdigest() != expected:
                    raise ValueError('Pinned model hash mismatch')
    results=[]
    for mode in (['restrained'] if args.chapter_plan else DIRECTIONS):
        aid='qwen-red-riding' if args.chapter_plan else 'qwen-'+mode
        if psutil.virtual_memory().available<12*1024**3:
            results.append({'mode':mode,'state':'insufficient_system_ram'});break
        command=[sys.executable,str(Path(__file__).resolve()),'--child','--mode',mode,
                 '--model',str(args.model),'--upstream',str(args.upstream),'--output',str(args.output),
                 '--threads',str(args.threads)]
        if args.chapter_plan:command+=['--chapter-plan',str(args.chapter_plan)]
        started=time.monotonic();peak=0;failure=None
        with (args.output/(aid+'.log')).open('w',encoding='utf-8') as log:
            process=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,
                                     creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            child_process=psutil.Process(process.pid)
            while process.poll() is None:
                try:
                    # Windows venv python.exe can be a small launcher with a real child.
                    family=[child_process,*child_process.children(recursive=True)]
                    rss=sum(p.memory_info().rss for p in family if p.is_running())
                    peak=max(peak,rss)
                except psutil.NoSuchProcess:break
                if time.monotonic()-started>args.deadline:failure='deadline'
                elif psutil.virtual_memory().available<6*1024**3:failure='system_ram_guard'
                elif peak>16*1024**3:failure='process_ram_guard'
                if failure:
                    for descendant in reversed(child_process.children(recursive=True)):
                        try:descendant.kill()
                        except psutil.NoSuchProcess:pass
                    process.kill();process.wait();break
                time.sleep(1)
        report_path=args.output/(aid+'.json')
        if not failure and process.returncode==0 and report_path.exists():
            report=json.loads(report_path.read_text(encoding='utf-8'))
            target=args.output/(aid+'.mp3');encoded=target.with_suffix('.tmp.mp3')
            subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y','-i',str(args.output/(aid+'.wav')),'-c:a','libmp3lame','-b:a','128k',str(encoded)],check=True,timeout=60)
            encoded.replace(target)
            report.update(provenance,peak_rss_bytes=peak,state='rendered',file=target.name)
            report_path.write_text(json.dumps(report,indent=2),encoding='utf-8')
        else:
            report={**provenance,'id':aid,'state':failure or 'failed','exit_code':process.returncode,'peak_rss_bytes':peak}
        results.append(report)
        (args.output/('qwen-story-report.json' if args.chapter_plan else 'qwen-audition-report.json')).write_text(json.dumps(results,indent=2),encoding='utf-8')
        print(json.dumps(report),flush=True)
        if failure:break


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--model',type=Path,default=Path(r'C:\Booksmith\models\qwen3-1.7b-customvoice'))
    p.add_argument('--upstream',type=Path,default=Path(r'C:\Booksmith\references\QwenLM__Qwen3-TTS'))
    p.add_argument('--output',type=Path,default=Path(r'C:\Booksmith\data\auditions'))
    p.add_argument('--threads',type=int,default=4,choices=range(1,9))
    p.add_argument('--deadline',type=int,default=360)
    p.add_argument('--child',action='store_true')
    p.add_argument('--mode',choices=list(DIRECTIONS),default='neutral')
    p.add_argument('--chapter-plan',type=Path)
    args=p.parse_args()
    child(args) if args.child else run(args)
