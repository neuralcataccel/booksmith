"""Two-level Jev pause audition. No production editions or narration policy change."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

import numpy as np

import pause_pilot as pilot
from reading_plan import digest
from pause_rules import TIGHT, FLOW, SENTENCE_INSTRUCTIONS, PARAGRAPH_INSTRUCTIONS, plans, paragraph_payload, question, map_boundaries

VERSION = 2


def fixtures():
    # Explicit sentence/paragraph fixtures, not a claimed general ebook sentence parser.
    units = [
        [['Before you answer, think of the last time you changed your mind.', 'What convinced you?',
          'Was it evidence, or was it someone you trusted?', 'Hold that answer for a moment.',
          'Now ask yourself whether the same reason still holds.']],
        [['"Did you tell her?" Maya asked.', '"No," Daniel said.', 'She set the photograph between them.',
          '"Then who did?"', 'Daniel looked toward the door for a long moment.', '"I think you already know."']],
        [['For a moment, the room was quiet.', 'Then Ben laughed.',
          '"You thought I meant the bank by the river?" he said.', '"I meant the one that lost my savings."',
          'Across town, the night guard was already unlocking the vault.']],
    ]
    result=[]
    for old, paragraphs in zip(pilot.passages(), units):
        result.append({**old, 'paragraphs': paragraphs})
    result.extend([
        {'id':'questions', 'title':'Questions that belong together', 'paragraphs':[
            ['What did we learn?', 'What should we change?', 'Those were the headings on the worksheet, not questions for the listener.'],
            ['Choose one habit you would change.', 'Picture tomorrow morning with that habit gone.', 'Keep that picture in mind as we continue.']],
         'targets':['What did we learn?', 'What should we change?', 'not questions for the listener.',
                    'Choose one habit you would change.', 'that habit gone.']},
        {'id':'exchange', 'title':'Keep the exchange moving', 'paragraphs':[
            ['"Ready?" Ana asked.', '"Almost," Ben said.', '"Now?"', '"Now."', 'They ran for the train.'],
            ['Three years later, Ana found the unused tickets.', 'She turned them over.', 'Ben had written one word on the back: home.']],
         'targets':['"Ready?', 'Ana asked.', '"Almost,', '"Now?"', 'They ran for the train.', 'the unused tickets.', 'She turned them over.']},
        {'id':'instruction', 'title':'Mentioned pauses and a delayed reveal', 'paragraphs':[
            ['The sign said, "Pause here."', 'It was an instruction for the recording technician, not a reason to stop the story.',
             'Nora read it aloud and kept walking.'],
            ['At the next door, she heard her own voice from inside.', 'The recording had been made before she was born.',
             'This time, she waited before turning the handle.']],
         'targets':['"Pause here."', 'not a reason to stop the story.', 'kept walking.',
                    'her own voice from inside.', 'before she was born.']},
    ])
    for item in result:
        text='\n\n'.join(' '.join(p) for p in item['paragraphs'])
        if 'text' in item and text!=item['text']:
            raise ValueError('Original comparison source changed.')
        item['text']=text
    return result


def boundaries(item):
    spans=[]; cursor=0
    for pi, paragraph in enumerate(item['paragraphs']):
        for sentence in paragraph:
            start=item['text'].index(sentence,cursor);end=start+len(sentence)
            spans.append((start,end,pi));cursor=end
    rows=[]
    for i,target in enumerate(item['targets']):
        offset=item['text'].index(target)+len(target)
        start,end,pi=next(s for s in spans if s[0]<offset<=s[1])
        rows.append({'id':f"{item['id']}-{i}", 'passage':item['id'], 'paragraph':pi,
                     'offset':offset, 'punctuation_offset':max(m.start() for m in re.finditer(r'[,;:.!?]',item['text'][:offset])),
                     'sentence':item['text'][start:offset]+'<PAUSE_TARGET/>'+item['text'][offset:end]})
    return rows








def ensure_suite(root):
    from settings import CONFIG
    from speech_engines import native_descriptor
    spec={'schema':VERSION,'fixtures':fixtures(),'sentence_prompt':SENTENCE_INSTRUCTIONS,
          'paragraph_prompt':PARAGRAPH_INSTRUCTIONS,'roles':pilot.CHOICES,'tight_ms':TIGHT,'flow_ms':FLOW,
          'plan_builder_version':1,'voice':'am_eric','speed':1,
          'engine':native_descriptor(CONFIG['model_sha256'],CONFIG['voices_sha256'])}
    path=root/'suite.json'
    if path.exists() and json.loads(path.read_text(encoding='utf-8'))!={'sha256':digest(spec),'specification':spec}:
        raise ValueError('Changed hierarchy source/policy. Choose a fresh experiment directory.')
    if not path.exists():pilot.write(path,{'sha256':digest(spec),'specification':spec})


def classify(root):
    import jev_client
    path=root/'decisions.json'
    calls=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    def decide(key,payload,choices):
        if key not in calls:
            calls[key]=jev_client.decide(payload,'pause-hierarchy-20261024',choices)
            pilot.write(path,calls)
            print(json.dumps({'completed':key,'new_cost_usd':calls[key]['new_cost'],'cached':calls[key]['cached'],
                              'original_seconds':round(calls[key]['seconds'],3)}),flush=True)
        return calls[key]
    for item in fixtures():
        all_rows=boundaries(item)
        for row in all_rows:
            decide('sentence:'+row['id'],question({'marked_sentence':row['sentence']},SENTENCE_INSTRUCTIONS,pilot.CHOICES),pilot.CHOICES)
        sentence={r['id']:calls['sentence:'+r['id']] for r in all_rows}
        for pi in sorted({r['paragraph'] for r in all_rows}):
            rows=[r for r in all_rows if r['paragraph']==pi];alternatives=plans(rows,sentence)
            for reverse in [False,True]:
                payload,choices=paragraph_payload(item,pi,rows,sentence,alternatives,reverse)
                decide(f"paragraph:{item['id']}:{pi}:"+('reverse' if reverse else 'primary'),payload,choices)
    selected=[];review=[]
    for item in fixtures():
        all_rows=boundaries(item);sentence={r['id']:calls['sentence:'+r['id']] for r in all_rows}
        for pi in sorted({r['paragraph'] for r in all_rows}):
            rows=[r for r in all_rows if r['paragraph']==pi];alternatives=plans(rows,sentence)
            first=calls[f"paragraph:{item['id']}:{pi}:primary"]['answer']['choice']
            second=calls[f"paragraph:{item['id']}:{pi}:reverse"]['answer']['choice']
            plan=alternatives.get(first,{r['id']:{'role':'keep','target_ms':0} for r in rows})
            review.append({'id':f"{item['id']}:{pi}",'primary':first,'reverse':second,
                           'order_stable':first==second,'unknown':first=='unknown','alternatives':alternatives})
            for row in rows:
                role=sentence[row['id']]['answer']['choice']
                selected.append({**row,'sentence_role':role,'sentence_target_ms':TIGHT.get(role,0),
                                 'paragraph_role':plan[row['id']]['role'],'paragraph_target_ms':plan[row['id']]['target_ms'],
                                 'order_stable':first==second,'selected_plan':first})
    pilot.write(root/'boundary-plan.json',selected);pilot.write(root/'paragraph-review.json',review)
    # Cached results retain their original cost/latency; do not call that fresh usage.
    summary={'decisions':len(calls),'unique_request_keys':len({r['request_key'] for r in calls.values()}),
             'decision_cost_usd':sum(r['cost'] for r in {r['request_key']:r for r in calls.values()}.values()),
             'new_cost_usd':sum(r['new_cost'] for r in calls.values()),
             'cached_decisions':sum(r['cached'] for r in calls.values()),
             'call_seconds_sum':sum(r['seconds'] for r in calls.values()),
             'input_tokens':sum(r['input_tokens'] for r in calls.values()),
             'models':sorted({r['model'] for r in calls.values()}),'boundaries':len(selected),'paragraphs':len(review),
             'order_stable':sum(r['order_stable'] for r in review),
             'unknown_paragraphs':sum(r['unknown'] for r in review),
             'revised_boundaries':sum((r['sentence_role'],r['sentence_target_ms'])!=(r['paragraph_role'],r['paragraph_target_ms']) for r in selected),
             'sentence_roles':dict(Counter(r['sentence_role'] for r in selected)),
             'paragraph_roles':dict(Counter(r['paragraph_role'] for r in selected))}
    normal=[value for key,value in calls.items() if not key.endswith(':reverse')]
    summary['normal_two_pass_cost_usd']=sum(r['cost'] for r in normal)
    summary['normal_two_pass_call_seconds_sum']=sum(r['seconds'] for r in normal)
    summary['normal_two_pass_decisions']=len(normal)
    pilot.write(root/'summary.json',summary);print(json.dumps(summary),flush=True)




def render(root,previous):
    import native_speech,speech,soundfile as sf
    from worker import ffmpeg
    from settings import CONFIG
    from speech_engines import native_descriptor
    rows=json.loads((root/'boundary-plan.json').read_text(encoding='utf-8'))
    reports=[];mapping=[]
    for item in fixtures():
        folder=root/item['id'];folder.mkdir(exist_ok=True);base=folder/'base.npz';timing_file=folder/'timings.json'
        if not base.exists():
            old=previous/item['id']
            if item['id'] in {p['id'] for p in pilot.passages()}:
                old_suite=json.loads((previous/'suite.json').read_text(encoding='utf-8'))['specification']
                expected=next(p['text'] for p in old_suite['passages'] if p['id']==item['id'])
                if (expected!=item['text'] or old_suite['voice']!='am_eric' or old_suite['speed']!=1
                    or old_suite['engine']!=native_descriptor(CONFIG['model_sha256'],CONFIG['voices_sha256'])):
                    raise ValueError('Previous baseline source/engine mismatch.')
                base.write_bytes((old/'base.npz').read_bytes());timing_file.write_bytes((old/'timings.json').read_bytes())
            else:
                if not native_speech.available():raise ValueError('Wait for GPU/camera headroom.')
                audio,rate,timings=native_speech.render(speech.load(),item['text'],{'voice':'am_eric','speed':1,'lang':'en-us'},
                    native_descriptor(CONFIG['model_sha256'],CONFIG['voices_sha256']),return_timings=True)
                np.savez(base,audio=audio,rate=rate)
                pilot.write(timing_file,[{'phoneme':t.phoneme,'start':t.start,'end':t.end} for t in timings])
        data=np.load(base,allow_pickle=False);audio=data['audio'];rate=int(data['rate'])
        local=[r for r in rows if r['passage']==item['id']]
        mapped=map_boundaries(audio,rate,json.loads(timing_file.read_text(encoding='utf-8')),item['text'],local)
        mapping.extend(mapped)
        for policy in ['baseline','sentence','hierarchy']:
            edits=[]
            for row in local:
                gap=next(g for g in mapped if g['id']==row['id'])
                if policy=='baseline' or gap['status']!='mapped':continue
                prefix='sentence' if policy=='sentence' else 'paragraph'
                target=row[prefix+'_target_ms'];extra=max(0,target-gap['existing_ms'])
                if extra:edits.append({'id':row['id'],'role':row[prefix+'_role'],'sample':gap['sample'],
                                      'existing_ms':gap['existing_ms'],'target_total_ms':target,'extra_ms':extra})
            variant,receipt=pilot.insert_pauses(audio,edits,rate);recovered=variant.copy()
            for edit in reversed(receipt):
                at=edit['output_sample'];recovered=np.concatenate([recovered[:at],recovered[at+edit['inserted_samples']:]])
            if not np.array_equal(audio,recovered):raise ValueError('Changed original speech samples.')
            wave=folder/(policy+'.wav');sf.write(wave,variant,rate,subtype='PCM_16')
            ffmpeg(['-i',str(wave),'-c:a','libmp3lame','-b:a','96k',str(folder/(policy+'.mp3'))])
            reports.append({'passage':item['id'],'policy':policy,'audio_seconds':len(variant)/rate,
                            'base_seconds':len(audio)/rate,'source_text_sha256':hashlib.sha256(item['text'].encode()).hexdigest(),
                            'speech_samples_unchanged':True,'edits':receipt})
    pilot.write(root/'render-report.json',reports);pilot.write(root/'mapping-report.json',mapping)
    samples=[{'chapter':i,'chunk':j,'title':item['title']+' / '+policy,'text':item['text'],
              'source_text':item['text'],'category':'pause_hierarchy','audio':item['id']+'/'+policy+'.wav'}
             for i,item in enumerate(fixtures(),1) for j,policy in enumerate(['baseline','sentence','hierarchy'])]
    pilot.write(root/'samples.json',samples)
    print(json.dumps({'clips':len(reports),'mapped':sum(r['status']=='mapped' for r in mapping),'boundaries':len(mapping)}),flush=True)


def publish(root):
    """Publish only verified auditions through the existing protected index."""
    import soundfile as sf
    from settings import RUNTIME
    from worker import ffmpeg
    report=json.loads((root/'render-report.json').read_text(encoding='utf-8'))
    expected={(item['id'],policy) for item in fixtures() for policy in ['baseline','sentence','hierarchy']}
    if {(r['passage'],r['policy']) for r in report}!=expected or not all(r['speech_samples_unchanged'] for r in report):
        raise ValueError('Incomplete or unverified renders.')
    asr=json.loads((root/'asr.json').read_text(encoding='utf-8'))
    expected_audio={item['id']+'/'+policy+'.wav' for item in fixtures() for policy in ['baseline','sentence','hierarchy']}
    if {r['audio'] for r in asr}!=expected_audio or any(r['needs_listening'] for r in asr):
        raise ValueError('Complete local ASR screening must pass before audition publication.')
    folder=RUNTIME/'data'/'auditions';index=folder/'index.json';original=index.read_bytes()
    backup=root/'auditions-index-before.json'
    if not backup.exists():backup.write_bytes(original)
    data=json.loads(original);receipts=[]
    exports=[('baseline','pause-hierarchy-baseline','Current timing — expanded pause test',6),
             ('sentence','pause-hierarchy-sentence','Jev sentence pass — tight timing',6),
             ('hierarchy','pause-hierarchy-two-level','Jev sentence + paragraph — tight timing',6),
             ('hierarchy','pause-hierarchy-short','Jev two levels — original three passages',3)]
    for policy,aid,name,count in exports:
        parts=[]
        for item in fixtures()[:count]:
            audio,rate=sf.read(root/item['id']/(policy+'.wav'),dtype='float32')
            if rate!=24000:raise ValueError('Unexpected audition sample rate.')
            if parts:parts.append(np.zeros(round(.8*rate),dtype=np.float32))
            parts.append(audio)
        audio=np.concatenate(parts);wave=root/(aid+'.wav');sf.write(wave,audio,24000,subtype='PCM_16')
        temporary=folder/(aid+'.tmp.mp3');target=folder/(aid+'.mp3')
        ffmpeg(['-i',str(wave),'-c:a','libmp3lame','-b:a','96k',str(temporary)]);temporary.replace(target)
        item={'id':aid,'file':target.name,'kind':'story','name':name,'speaker':'Eric',
              'engine':'Kokoro native · Jev pause audition','text':'Reflection, dialogue, scene change'+(' and paragraph controls' if count==6 else ''),
              'description':'Same Eric takes; '+('unchanged engine timing.' if policy=='baseline' else
                  '150/300 ms brief, 650 ms reflection and 1.1 s scene total-gap targets. '+
                  ('Sentence decisions only.' if policy=='sentence' else 'Paragraph review selects a complete plan after sentence proposals.'))+
                  ' Listening experiment; automatic book timing is not enabled.',
              'audio_seconds':len(audio)/24000,'audio_sha256':hashlib.sha256(target.read_bytes()).hexdigest()}
        data['items']=[x for x in data['items'] if x['id']!=aid]+[item];receipts.append(item)
    if index.read_bytes()!=original:raise ValueError('Audition index changed during preparation; rerun to preserve newer entries.')
    pilot.write(index,data);pilot.write(root/'published.json',receipts)
    print(json.dumps([{'id':r['id'],'seconds':r['audio_seconds']} for r in receipts]),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['classify','render','publish'])
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--previous',type=Path,default=Path('C:/Booksmith/data/pause-pilot-20261024'))
    args=parser.parse_args();args.root.mkdir(parents=True,exist_ok=True);ensure_suite(args.root)
    if args.command=='classify':classify(args.root)
    elif args.command=='render':render(args.root,args.previous)
    else:publish(args.root)
