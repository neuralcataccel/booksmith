"""Controlled A/B rendering from existing Jev decisions; no model requests here."""
import argparse
import json
from pathlib import Path
import subprocess
import time

from analyze_pronunciation import normalized_phone
from pronunciation_cases import WORDS


def render(ledger,root):
    import numpy as np
    import soundfile as sf
    import speech
    from kokoro_onnx.tokenizer import Tokenizer
    records=[json.loads(s) for s in ledger.read_text(encoding='utf-8').splitlines()]
    definitions={w[0]:w for w in WORDS}
    selected=[('lead','a'),('bass','a'),('read','a'),('read','b')]
    tokenizer=Tokenizer();model=speech.load();arrays={'default':[],'context':[]}
    timing={'default':0.,'context':0.};details=[]
    root.mkdir(parents=True,exist_ok=True)
    for word,label in selected:
        definition=definitions[word]
        text=definition[7] if label=='a' else definition[8]
        row=next(r for r in records if r['case']=='pronunciation-'+word+'-'+label and r['tier']=='sentence')
        choice=row['answer']['choice']
        if choice!=label:raise ValueError('The controlled choice disagrees with the authored case; inspect it before auditioning.')
        desired=definition[3] if choice=='a' else definition[4]
        original=tokenizer.phonemize(text,lang='en-us');tokens=original.split(' ')
        possible={normalized_phone(definition[3]),normalized_phone(definition[4])}
        matches=[i for i,t in enumerate(tokens) if normalized_phone(t) in possible]
        if len(matches)!=1:raise ValueError('This controlled example does not have one uniquely aligned target phone token.')
        index=matches[0];token=tokens[index]
        tail=token[len(token.rstrip('.,!?;:')):]
        tokens[index]=desired+tail;modified=' '.join(tokens)
        details.append({'text':text,'word':word,'choice':choice,'confidence':row['answer']['confidence'],
                        'original_phonemes':original,'selected_phonemes':modified,'request_sha256':row['request_sha256']})
        for mode,value in [('default',text),('context',modified)]:
            start=time.perf_counter()
            audio,rate=model.create(value,voice='af_heart',speed=1,lang='en-us',is_phonemes=mode=='context')
            timing[mode]+=time.perf_counter()-start
            if len(audio)<2400 or not np.isfinite(audio).all():raise ValueError('Invalid audition audio')
            arrays[mode]+=[audio,np.zeros(int(rate*.22),dtype=np.float32)]
    text=' '.join(d['text'] for d in details);items=[]
    for mode,parts in arrays.items():
        aid='pronunciation-'+mode;audio=np.concatenate(parts);wav=root/(aid+'.wav')
        temporary=wav.with_suffix('.tmp.wav');sf.write(temporary,audio,rate);temporary.replace(wav)
        target=wav.with_suffix('.mp3')
        encoded=target.with_suffix('.tmp.mp3')
        subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-y','-i',str(wav),'-c:a','libmp3lame','-b:a','128k',str(encoded)],check=True,timeout=60)
        encoded.replace(target)
        item={'id':aid,'kind':'pronunciation','name':'Default pronunciation' if mode=='default' else 'Context-guided pronunciation',
              'engine':'Kokoro ONNX','speaker':'Heart','text':text,'file':target.name,'audio_seconds':len(audio)/rate,
              'synthesis_seconds':timing[mode],
              'description':'Listen especially to lead, bass, and the two readings of read. '+('Existing phonemizer, unchanged text.' if mode=='default' else 'Jev selected among known readings in this controlled experiment; source words are unchanged. Not enabled for book conversions.'),
              'decisions':details if mode=='context' else []}
        (root/(aid+'.json')).write_text(json.dumps(item,indent=2),encoding='utf-8');items.append(item)
    index_path=root/'index.json'
    index=json.loads(index_path.read_text(encoding='utf-8')) if index_path.is_file() else {'schema':1,'items':[]}
    index['items']=[x for x in index['items'] if not x['id'].startswith('pronunciation-')]+items
    temporary=root/'index.tmp.json';temporary.write_text(json.dumps(index,indent=2),encoding='utf-8');temporary.replace(index_path)
    print(json.dumps([{'id':x['id'],'audio_seconds':x['audio_seconds']} for x in items]))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ledger',type=Path,default=Path(r'C:\Booksmith\data\jev-pronunciation-2026-10-24\calls.jsonl'))
    p.add_argument('--output',type=Path,default=Path(r'C:\Booksmith\data\auditions'))
    args=p.parse_args();render(args.ledger,args.output)
