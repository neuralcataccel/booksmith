"""Compare the controlled Jev suite with the installed English-US phonemizer."""
import argparse
import json
from pathlib import Path

from pronunciation_cases import cases


def normalized_phone(value):
    return value.replace('ˈ','').replace('ˌ','').strip('.,!?;:()[]{}“”"')


def analyze(root):
    from kokoro_onnx.tokenizer import Tokenizer
    definitions={c['id']:c for c in cases()}
    rows=[json.loads(s) for s in (root/'calls.jsonl').read_text(encoding='utf-8').splitlines()]
    tokenizer=Tokenizer();results=[]
    for row in rows:
        c=definitions[row['case']]
        phones=tokenizer.phonemize(c['contexts'][row['tier']],lang='en-us')
        tokens={normalized_phone(x) for x in phones.split()}
        matches=[label for label,phone in c['candidate_phonemes'].items() if normalized_phone(phone) in tokens]
        results.append({**row,'phonemizer':matches[0] if len(matches)==1 else 'unknown','phonemizer_output':phones})
    summary=[]
    for tier in ['token','phrase','sentence','paragraph','scene']:
        selected=[r for r in results if r['tier']==tier]
        if not selected:continue
        summary.append({'context':tier,'n':len(selected),
            'jev_correct':sum(r['answer']['choice']==r['expected'] for r in selected),
            'phonemizer_correct':sum(r['phonemizer']==r['expected'] for r in selected),
            'jev_wrong_changes':sum(r['answer']['choice']!='unknown' and r['answer']['choice']!=r['expected'] for r in selected),
            'cost':sum(r['cost'] for r in selected)})
    report={'summary':summary,'rows':results,
        'limitations':['English-US candidate-phone token matching, not an acoustic pronunciation score.',
                       'Small original controlled cases; separate unambiguous and underdetermined examples.',
                       'Candidate matching can miss dialectal/reduced phone variants; inspect unmatched results.']}
    (root/'phonemizer-comparison.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(summary,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('root',type=Path)
    analyze(p.parse_args().root)
