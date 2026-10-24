"""Source-bound Jev pause plans applied once, after ordinary chapter pauses."""
import html
import json
from pathlib import Path
import re

import numpy as np
import soundfile as sf

import jev_client
from pause_rules import CHOICES, BASE_INSTRUCTIONS, TIGHT, FLOW, SENTENCE_INSTRUCTIONS, PARAGRAPH_INSTRUCTIONS, plans, question, map_boundaries, insert_pauses, write
from reading_plan import digest

REVIEW_INSTRUCTIONS = BASE_INSTRUCTIONS + (
    ' This is paragraph review. Compare the supplied complete paragraph variants and sentence proposals. '
    'Decide this one boundary in the rhythm of the whole paragraph and neighboring paragraphs. '
    'Remove redundant sentence-level holds; identify genuine scene changes missed in a single sentence. '
    'The final plan permits at most one reflection/scene hold per paragraph. '
    'Judge only the marked passage assigned to THIS question; other marked passages refer to other boundaries.')

POLICY = {'schema':4,'name':'jev-two-level-tight','rules':digest([CHOICES,TIGHT,FLOW,SENTENCE_INSTRUCTIONS,PARAGRAPH_INSTRUCTIONS,REVIEW_INSTRUCTIONS]),
          'paragraph_review_contract':2,'model':jev_client.MODEL,'targets_ms':TIGHT,'max_paragraph_characters':4000,'max_boundaries':18}


def attach(options, enabled=True):
    if not enabled:return options
    return {**options,'_pause_policy':json.loads(json.dumps(POLICY))}


def sentence_ranges(text):
    """Conservative punctuation segmentation; keep common abbreviations/attributions together."""
    start=0;result=[]
    for mark in re.finditer(r'[.!?]["\u201d\u2019\)]*(?=\s|$)',text):
        at=mark.start();tail=text[mark.end():]
        word=re.search(r'([\w.]+)$',text[:at])
        if text[at]=='.' and word and (word[1].lower() in {'mr','mrs','ms','dr','prof','sr','jr','st','vs','etc'}
                                                     or len(word[1])==1 or '.' in word[1]):continue
        if re.match(r'\s+(?:(?:[A-Z][\w-]*|he|she|they)\s+){1,3}(?:said|asked|replied|whispered|shouted)\b',tail):continue
        result.append((start,mark.end()));start=mark.end()
    if text[start:].strip():result.append((start,len(text)))
    return result


def boundaries(span, chapter_last=False):
    text=span['text'];sentences=sentence_ranges(text);rows=[]
    for mark in re.finditer(r'[,;:.!?]["\u201d\u2019\)]*(?=\s|$)',text):
        at=mark.start();end=mark.end()
        if chapter_last and not text[end:].strip():continue
        if text[at]=='.' and (at and text[at-1]=='.' or at+1<len(text) and text[at+1]=='.'):continue
        a,b=next(((a,b) for a,b in sentences if a<=at<b),(0,len(text)))
        # Exclude periods inside a retained abbreviation, not its eventual sentence end.
        if text[at]=='.' and end<b and re.search(r'\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|vs|etc|[A-Z])\.$',text[:at+1]):continue
        rows.append({'id':f'b{at}','offset':end,'punctuation_offset':at,
                     'sentence':html.escape(text[a:end],quote=False)+'<PAUSE_TARGET/>'+html.escape(text[end:b],quote=False)})
    return rows


def realizable(alternatives,gaps):
    """Deduplicate plans by their actual edits; unknown/unsafe gaps retain baseline."""
    result={}
    for plan in alternatives.values():
        actual={}
        for key,value in plan.items():
            gap=gaps[key]
            actual[key]=(dict(value) if gap['status']=='mapped' and value['target_ms']>gap['existing_ms']
                         else {'role':'keep','target_ms':0})
        result.setdefault('p'+digest(actual)[:12],actual)
    return result


def validate_policy(options):
    if options.get('_pause_policy')!=POLICY:raise ValueError('This book requires its saved contextual pause policy.')




def review_payload(span,rows,decisions,gaps,previous,following,all_rows=None):
    variants={}
    for name,targets in [('unchanged',{}),('sentence_tight',TIGHT),('sentence_roomier',FLOW)]:
        variants[name]=[{"boundary":r['id'],"role":decisions[r['id']]['answer']['choice'],
                         "target_total_ms":targets.get(decisions[r['id']]['answer']['choice'],0)} for r in (all_rows or rows)]
    state={'previous_paragraph':previous,'next_paragraph':following,'paragraph_variants':variants,
           'existing_gaps':{key:{k:v for k,v in value.items() if k in {'status','existing_ms'}} for key,value in gaps.items()},
           'marked_passages':{}}
    payload=question(state,REVIEW_INSTRUCTIONS,CHOICES);mapping={};additional={}
    for i,row in enumerate(rows):
        name='decision' if i==0 else 'boundary_'+str(i)
        at=row['offset']
        state['marked_passages'][name]=html.escape(span['text'][:at],quote=False)+'<PAUSE_TARGET/>'+html.escape(span['text'][at:],quote=False)
        payload['questions'][name]={'type':'choice','instructions':REVIEW_INSTRUCTIONS+' Use ONLY state.marked_passages.'+name+'.','criteria':CHOICES}
        mapping[name]=row['id']
        if i:additional[name]=CHOICES
    return payload,additional,mapping


def chapter(job,plan,ci,wavs,index,options,check_cancel=lambda:None):
    """Keep base WAVs immutable; write adjusted copies and update retained timing offsets."""
    if not options.get('_pause_policy'):return wavs,None
    validate_policy(options)
    root=Path(wavs[0]).parent;retained=root.parent.parent/'pause-plans';retained.mkdir(exist_ok=True)
    pcm=[];timings=[];piece_meta=[];position=0
    spans=plan['chapters'][ci]['spans'];pieces=[(s,p) for s in spans for p in s['pieces']]
    if len(pieces)!=len(wavs):raise ValueError('Pause source/chunk coverage mismatch.')
    for path,(span,piece) in zip(wavs,pieces):
        data,rate=sf.read(path,dtype='float32')
        if rate!=24000 or data.ndim!=1:raise ValueError('Pause editing requires mono 24 kHz PCM.')
        row=json.loads(path.with_suffix('.timings.json').read_text(encoding='utf-8'))
        if (row.get('sha256')!=digest({k:v for k,v in row.items() if k!='sha256'})
            or row['input_sha256']!=digest({'text':piece['text'],'policy':POLICY}) or row['wave_sha256']!=file_hash(path)):
            raise ValueError('Changed pause timing input/audio.')
        pcm.append(data);piece_meta.append({'path':path,'span':span,'piece':piece,'start':position,'samples':len(data)})
        timings.append(row['timings']);position+=len(data)
    audio=np.concatenate(pcm);edits=[];paragraphs=[]
    for si,span in enumerate(spans):
        check_cancel()
        if span['kind'].startswith('h') or span['kind']=='legacy_chunk':
            paragraphs.append({'span':span['id'],'status':'structural_or_legacy'});continue
        rows=boundaries(span,ci==len(plan['chapters'])-1 and si==len(spans)-1)
        if len(span['text'])>POLICY['max_paragraph_characters'] or len(rows)>POLICY['max_boundaries']:
            paragraphs.append({'span':span['id'],'status':'paragraph_limit_keep_baseline'});continue
        if not rows:
            paragraphs.append({'span':span['id'],'status':'no_candidates'});continue
        # Map each piece's complete punctuation sequence before any model can request an edit.
        gaps={}
        for pi,meta in enumerate(piece_meta):
            if meta['span']['id']!=span['id']:continue
            local=[{**r,'punctuation_offset':r['punctuation_offset']-meta['piece']['start']}
                   for r in rows if meta['piece']['start']<=r['punctuation_offset']<meta['piece']['end']]
            try:
                # Include following audio to measure the entire existing boundary gap.
                start=meta['start'];end=start+meta['samples']
                mapped=map_boundaries(audio[start:min(len(audio),end+24000)],24000,timings[pi],meta['piece']['text'],local)
                for gap in mapped:
                    if gap['status']=='mapped':gap['sample']+=start
                    gaps[gap['id']]=gap
            except (ValueError,StopIteration):
                gaps.update({r['id']:{'id':r['id'],'status':'punctuation_alignment_unavailable','existing_ms':0} for r in local})
        if set(gaps)!={r['id'] for r in rows}:raise ValueError('Pause boundaries do not cover source pieces.')
        request_identity=digest({'policy':POLICY,'source':plan['sha256'],'span':span['id'],'rows':rows,'gaps':gaps,
                                 'previous':spans[si-1]['text'] if si else '',
                                 'next':spans[si+1]['text'] if si+1<len(spans) else ''})
        saved=retained/f'{ci+1:04}-{si:05}.json'
        if saved.exists():
            receipt=json.loads(saved.read_text(encoding='utf-8'))
            if receipt['identity']!=request_identity or receipt['sha256']!=digest({k:v for k,v in receipt.items() if k!='sha256'}):
                raise ValueError('The saved paragraph pause plan changed.')
        else:
            decisions={};calls=[];status='selected';selected={r['id']:{'role':'keep','target_ms':0} for r in rows}
            try:
                for row in rows:
                    check_cancel()
                    answer=jev_client.decide(question({'marked_sentence':row['sentence']},SENTENCE_INSTRUCTIONS,CHOICES),job['id'],CHOICES)
                    decisions[row['id']]=answer;calls.append(answer)
                editable=[r for r in rows if gaps[r['id']]['status']=='mapped']
                reviewed={}
                # A few full-paragraph target views fit each typed request. This is
                # one paragraph-level review, batched to bound request size/latency.
                cursor=0
                while cursor<len(editable):
                    batch=editable[cursor:cursor+4]
                    while True:
                        payload,additional,mapping=review_payload(span,batch,decisions,gaps,
                            html.escape(spans[si-1]['text'][-1600:],quote=False) if si else '',
                            html.escape(spans[si+1]['text'][:1600],quote=False) if si+1<len(spans) else '',all_rows=rows)
                        if len(json.dumps(payload,ensure_ascii=False).encode())<=24000 or len(batch)==1:break
                        batch=batch[:-1]
                    check_cancel()
                    answer=jev_client.decide(payload,job['id'],CHOICES,additional_choices=additional);calls.append(answer)
                    answers={'decision':answer['answer'],**answer.get('additional_answers',{})}
                    if set(answers)!=set(mapping):raise ValueError('Incomplete typed paragraph review.')
                    for name,key in mapping.items():reviewed[key]=answers[name]
                    cursor+=len(batch)
                long=[key for key,answer in reviewed.items() if answer['choice'] in {'reflect','scene'}]
                strongest=max(long,key=lambda key:(reviewed[key]['confidence'],reviewed[key]['choice']=='scene')) if long else None
                for key,answer in reviewed.items():
                    role=answer['choice'];target=TIGHT.get(role,0)
                    if role in {'reflect','scene'} and key!=strongest:role='keep';target=0
                    if target>gaps[key]['existing_ms']:selected[key]={'role':role,'target_ms':target}
                if not editable:status='no_editable_gaps_keep_baseline'
                elif all(a['choice']=='unknown' for a in reviewed.values()):status='unknown_keep_baseline'
            except jev_client.Unavailable:
                status='annotation_unavailable_keep_baseline'
            receipt={'identity':request_identity,'span':span['id'],'status':status,'selected':selected,'gaps':gaps,
                     'calls':calls,'policy':POLICY,'source_plan_sha256':plan['sha256']}
            receipt['sha256']=digest(receipt);write(saved,receipt)
        paragraphs.append({'span':span['id'],'status':receipt['status'],'sha256':receipt['sha256'],
                           'unapplied':sum(g['status']!='mapped' for g in gaps.values())})
        for key,value in receipt['selected'].items():
            gap=gaps[key];extra=max(0,value['target_ms']-gap['existing_ms'])
            if gap['status']=='mapped' and extra:
                edits.append({'id':key,'sample':gap['sample'],'extra_ms':extra,'role':value['role'],
                              'existing_ms':gap['existing_ms'],'target_total_ms':value['target_ms']})
    # Multiple source marks pointing at one acoustic gap receive one maximum top-up.
    unique={}
    for edit in edits:
        at=edit['sample']
        if at not in unique or edit['extra_ms']>unique[at]['extra_ms']:unique[at]=edit
    outputs=[];offset=0
    for meta,chunk in zip(piece_meta,index['chunks']):
        data=audio[meta['start']:meta['start']+meta['samples']]
        local=[{**e,'sample':e['sample']-meta['start']} for e in unique.values() if meta['start']<=e['sample']<meta['start']+meta['samples']]
        adjusted,inserted=insert_pauses(data,local,24000)
        target=meta['path'].with_suffix('.paused.wav');temp=target.with_suffix('.tmp.wav')
        sf.write(temp,adjusted,24000,subtype='PCM_16');temp.replace(target);outputs.append(target)
        chunk.update(start_seconds=offset,duration=len(adjusted)/24000)
        if inserted:chunk['pauses']=inserted
        offset+=len(adjusted)/24000
    result={'schema':1,'policy':POLICY,'source_plan_sha256':plan['sha256'],'paragraphs':paragraphs,
            'inserted_samples':sum(round(e['extra_ms']*24) for e in unique.values()),'audio_seconds':offset,
            'plan_sha256':digest(paragraphs)}
    write(retained/f'chapter-{ci+1:04}.json',result)
    index['contextual_pauses']=result
    return outputs,result


def file_hash(path):
    import hashlib
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
