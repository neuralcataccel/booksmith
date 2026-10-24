"""Small contextual-pause experiment. No production jobs or existing books change."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import time

import numpy as np

from reading_plan import digest
from pause_rules import CHOICES, BASE_INSTRUCTIONS as INSTRUCTIONS, insert_pauses, write

TARGET_MS={'breath':200,'turn':450,'reflect':1800,'scene':2200}
MICRO_EXTRA_MS={'breath':20,'turn':60}


def passages():
    return [
        {'id':'reflection','title':'A question worth sitting with',
         'text':'Before you answer, think of the last time you changed your mind. What convinced you? Was it evidence, or was it someone you trusted? Hold that answer for a moment. Now ask yourself whether the same reason still holds.',
         'targets':['Before you answer,','changed your mind.','What convinced you?','Hold that answer for a moment.']},
        {'id':'dialogue','title':'The photograph',
         'text':'"Did you tell her?" Maya asked. "No," Daniel said. She set the photograph between them. "Then who did?" Daniel looked toward the door for a long moment. "I think you already know."',
         'targets':['"Did you tell her?','Maya asked.','"No,','a long moment.']},
        {'id':'scene','title':'The wrong bank',
         'text':'For a moment, the room was quiet. Then Ben laughed. "You thought I meant the bank by the river?" he said. "I meant the one that lost my savings." Across town, the night guard was already unlocking the vault.',
         'targets':['the room was quiet.','Then Ben laughed.','the bank by the river?','lost my savings."']}
    ]


def candidates():
    rows=[]
    for passage in passages():
        for i,target in enumerate(passage['targets']):
            start=passage['text'].index(target);offset=start+len(target)
            # A closing quotation mark belongs to the source but not its phoneme punctuation.
            punctuation=max(m.start() for m in re.finditer(r'[,;:.!?]',passage['text'][:offset]))
            rows.append({'id':passage['id']+'-'+str(i),'passage':passage['id'],'text':passage['text'],
                         'offset':offset,'punctuation_offset':punctuation,'target':target})
    return rows


def marked(row,tier):
    if tier=='phrase':return row['target']+'<PAUSE_TARGET/>'
    text=row['text'];offset=row['offset']
    if tier=='local':
        left=max(0,offset-65);right=min(len(text),offset+65)
        text=text[left:right];offset-=left
    return text[:offset]+'<PAUSE_TARGET/>'+text[offset:]




def ensure_suite(root):
    from settings import CONFIG
    from speech_engines import native_descriptor
    specification={'schema':1,'passages':passages(),'choices':CHOICES,'instructions':INSTRUCTIONS,
                   'voice':'am_eric','speed':1,'targets_ms':TARGET_MS,'micro_extra_ms':MICRO_EXTRA_MS,
                   'engine':native_descriptor(CONFIG['model_sha256'],CONFIG['voices_sha256'])}
    expected=digest(specification);path=root/'suite.json'
    if path.exists():
        saved=json.loads(path.read_text(encoding='utf-8'))
        if saved.get('sha256')!=expected or digest(saved.get('specification'))!=expected:
            raise ValueError('The pilot source/prompt/voice/policy changed. Choose a fresh experiment directory.')
    else:write(path,{'sha256':expected,'specification':specification})


def classify(root):
    import jev_client
    root.mkdir(parents=True,exist_ok=True)
    path=root/'decisions.json'
    rows=json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
    for row in candidates():
        for tier in ['phrase','local','passage']:
            if any(r['id']==row['id'] and r['tier']==tier for r in rows):continue
            payload={'state':{'marked_passage':marked(row,tier)},'questions':{
                'decision':{'type':'choice','instructions':INSTRUCTIONS,'criteria':CHOICES},
                'unique':{'type':'noul','instructions':'Does the marked boundary clearly justify exactly one timing role from '+json.dumps(CHOICES)+'?',
                          'criteria':{'true':'One role is clearly justified by the supplied context.','false':'Missing/ambiguous/conflicting evidence; do not guess.'}}}}
            answer=jev_client.decide(payload,'pause-pilot-20261024',CHOICES)
            rows.append({'id':row['id'],'tier':tier,**answer});write(path,rows)
    print(json.dumps({'jev_decisions':len(rows),'new_cost':sum(r['new_cost'] for r in rows)}),flush=True)


def review(root):
    """One bounded stronger-model comparison through the user's existing LiteLLM route."""
    import requests
    path=root/'sonnet-review.json'
    if path.exists():return
    marker=root/'sonnet-sending.json'
    if marker.exists():raise ValueError('Reconcile the possibly billed Sonnet request before retrying.')
    env={}
    for line in Path(r'C:\Booksmith\providers\.env').read_text(encoding='utf-8-sig').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key,value=line.split('=',1);env[key.strip()]=value.strip().strip('"').strip("'")
    content={'candidates':[{'id':r['id'],'passage':marked(r,'passage')} for r in candidates()],'choices':CHOICES}
    payload={'model':'sonnet','temperature':0,'max_tokens':1600,'messages':[
        {'role':'system','content':INSTRUCTIONS+' Return one JSON object mapping every candidate id to exactly {"choice": one supplied role, "reason": a short explanation}. Do not return rewritten passages or markdown.'},
        {'role':'user','content':json.dumps(content)}]}
    write(marker,{'request_sha256':digest(payload),'started':time.time(),'model_alias':'sonnet','max_output_tokens':1600})
    started=time.monotonic()
    with requests.Session() as session:
        session.trust_env=False
        response=session.post('http://127.0.0.1:4000/v1/chat/completions',json=payload,
            headers={'Authorization':'Bearer '+env['LITELLM_MASTER_KEY']},timeout=(3,90))
    if response.status_code!=200:raise ValueError('Sonnet comparison failed; reconcile its marker before retrying.')
    data=response.json();text=data['choices'][0]['message']['content'].strip()
    if text.startswith('```'):text=re.sub(r'^```(?:json)?\s*|\s*```$','',text)
    answers=json.loads(text)
    if set(answers)!={r['id'] for r in candidates()} or any(not isinstance(v,dict) or v.get('choice') not in CHOICES for v in answers.values()):
        raise ValueError('Invalid Sonnet boundary ids/choices; reconcile the sending marker.')
    cost=response.headers.get('x-litellm-response-cost')
    write(path,{'answers':answers,'model':data.get('model'),'model_alias':'sonnet','usage':data.get('usage'),
                'cost_usd':float(cost) if cost is not None else None,'request_id':data.get('id'),
                'seconds':time.monotonic()-started})
    marker.unlink()
    print(json.dumps({'sonnet_model':data.get('model'),'cost_usd':float(cost) if cost else None}),flush=True)


def selected(root):
    rows=json.loads((root/'decisions.json').read_text(encoding='utf-8'))
    reviewer=json.loads((root/'sonnet-review.json').read_text(encoding='utf-8'))['answers']
    result=[]
    for row in candidates():
        local=next(r for r in rows if r['id']==row['id'] and r['tier']=='local')
        wide=next(r for r in rows if r['id']==row['id'] and r['tier']=='passage')
        first=wide['answer']['choice'];second=reviewer[row['id']]['choice']
        # Keep an agreement-only candidate alongside the separate model proposals.
        # Agreement is not a claim of correctness or a hidden duration average.
        choice=first if first==second and first!='unknown' else 'keep'
        result.append({**row,'local':local['answer']['choice'],'jev':first,'sonnet':second,
                       'selected':choice,'reason':reviewer[row['id']].get('reason','')})
    return result




def render(root):
    import native_speech,speech,soundfile as sf
    from kokoro_onnx.pauses import _quiet_frames,_run_around
    from worker import ffmpeg
    chosen=selected(root);write(root/'boundary-plan.json',chosen)
    reports=[];mapping=[]
    for passage in passages():
        folder=root/passage['id'];folder.mkdir(exist_ok=True)
        base=folder/'base.npz'
        if not base.exists():
            if not native_speech.available():raise ValueError('Wait for GPU/camera headroom before this isolated audition.')
            k=speech.load()
            engine=native_speech.native_descriptor(speech.CONFIG['model_sha256'],speech.CONFIG['voices_sha256'])
            audio,rate,timings=native_speech.render(k,passage['text'],{'voice':'am_eric','speed':1,'lang':'en-us'},engine,return_timings=True)
            np.savez(base,audio=audio,rate=rate)
            write(folder/'timings.json',[{'phoneme':t.phoneme,'start':t.start,'end':t.end} for t in timings])
        loaded=np.load(base,allow_pickle=False);audio=loaded['audio'];rate=int(loaded['rate'])
        timings=json.loads((folder/'timings.json').read_text(encoding='utf-8'))
        source_marks=list(re.finditer(r'[,;:.!?]',passage['text']))
        spoken_marks=[t for t in timings if t['phoneme'] in ',;:.!?']
        if [m.group() for m in source_marks]!=[t['phoneme'] for t in spoken_marks]:
            raise ValueError('Source punctuation cannot be mapped exactly; no audio was edited.')
        frame=round(rate*.01);quiet=_quiet_frames(audio,frame)
        plans={name:[] for name in ['baseline','jev','sonnet','sonnet-micro','agreement']}
        for row in (r for r in chosen if r['passage']==passage['id']):
            index=next(i for i,m in enumerate(source_marks) if m.start()==row['punctuation_offset'])
            boundary=spoken_marks[index]
            start,length=_run_around(quiet,round(boundary['end']*rate)//frame,15)
            if length<3:
                mapping.append({'id':row['id'],'status':'no_safe_quiet_gap','existing_ms':length*10})
                continue  # Never create a cut in voiced audio or a tiny stop consonant.
            center=(start+length//2)*frame
            left=max(start*frame,center-frame);right=min((start+length)*frame,center+frame,len(audio))
            at=int(left+np.argmin(np.abs(audio[left:right])))
            current=length*10
            mapping.append({'id':row['id'],'status':'mapped_existing_quiet_gap','existing_ms':current})
            for policy in ['jev','sonnet','sonnet-micro','agreement']:
                role=row['jev'] if policy=='jev' else row['selected'] if policy=='agreement' else row['sonnet']
                if role in {'keep','unknown'}:continue
                target=TARGET_MS[role]
                extra=(MICRO_EXTRA_MS.get(role,max(0,target-current)) if policy=='sonnet-micro' else max(0,target-current))
                plans[policy].append({'id':row['id'],'role':role,'sample':at,'existing_ms':current,
                                      'extra_ms':extra,'target_total_ms':current+extra})
        for policy,edits in plans.items():
            variant,receipt=insert_pauses(audio,edits,rate)
            # Removing exactly the newly inserted samples must recover the original take bit-for-bit.
            recovered=variant.copy()
            for edit in reversed(receipt):
                at=edit['output_sample'];recovered=np.concatenate([recovered[:at],recovered[at+edit['inserted_samples']:]])
            if not np.array_equal(audio,recovered):raise ValueError('Pause insertion changed source speech samples.')
            wave=folder/(policy+'.wav');mp3=folder/(policy+'.mp3')
            sf.write(wave,variant,rate,subtype='PCM_16')
            ffmpeg(['-i',str(wave),'-c:a','libmp3lame','-b:a','96k',str(mp3)])
            reports.append({'passage':passage['id'],'policy':policy,'audio_seconds':len(variant)/rate,
                            'base_seconds':len(audio)/rate,'source_speech_samples_unchanged':True,
                            'source_text_sha256':hashlib.sha256(passage['text'].encode()).hexdigest(),'edits':receipt})
    write(root/'render-report.json',reports)
    write(root/'mapping-report.json',mapping)
    print(json.dumps({'clips':len(reports),'rendered':True}),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['classify','review','render'])
    parser.add_argument('--root',type=Path,required=True)
    args=parser.parse_args();args.root.mkdir(parents=True,exist_ok=True)
    ensure_suite(args.root)
    {'classify':classify,'review':review,'render':render}[args.command](args.root)
