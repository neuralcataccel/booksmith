"""Bounded, resumable hosted Jev context experiment. Outputs stay outside Git."""
import argparse
import json
import math
import re
import statistics
import time
from pathlib import Path
import urllib.error
import urllib.request

from jev_cases import TASKS, cases
from reading_plan import digest


from jev_client import validate_answer


def baseline(task, state):
    # Conservative rules-only reference. No regex tries to impersonate contextual understanding.
    import re
    if task == 'number':
        if re.search(r'\b\d+\.\d+\b',state): return 'decimal'
        if re.search(r'\broom \d+',state,re.I): return 'identifier'
        if re.search(r'\bin (?:18|19|20)\d{2}\b',state): return 'year'
    if task == 'chapter' and state.strip().lower() == 'chapter ii': return 'boundary'
    if task == 'speaker':
        if re.search(r'said Alice',state):return 'alice'
        if re.search(r'said Bob',state):return 'bob'
    return 'unknown'


def summarize(rows):
    groups = {}
    for row in rows:
        key = (row['task'],row['tier'],row['split'])
        groups.setdefault(key, []).append(row)
    report=[]
    for (task,tier,split), group in sorted(groups.items()):
        valid=[r for r in group if 'answer' in r]
        accepted=[r for r in valid if r['answer']['choice']!='unknown']
        report.append({'task':task,'tier':tier,'split':split,'n':len(group),
            'valid':len(valid),'correct':sum(r['answer']['choice']==r['expected'] for r in valid),
            'baseline_correct':sum(r['baseline']==r['expected'] for r in group),
            'resolved':len(accepted),'wrong_changes':sum(r['answer']['choice']!=r['expected'] for r in accepted),
            'correct_abstentions':sum(r['answer']['choice']=='unknown' and r['expected']=='unknown' for r in valid),
            'cost':sum(r.get('cost',0) for r in group),
            'input_tokens':sum(r.get('input_tokens',0) for r in group),
            'median_seconds':statistics.median(r['seconds'] for r in group)})
    return {'schema':1,'cases':len(rows),'billed_cost':sum(r.get('cost',0) for r in rows),
            'errors':sum('error' in r for r in rows),'groups':report,
            'limitations':['Small exploratory, original synthetic cases; not a production accuracy estimate.',
                          'Gold labels depend on available context; correct abstention is distinct from useful resolution.',
                          'The simple rules baseline is intentionally narrow; test stronger deterministic rules before promotion.',
                          'Different senses of a target word may occur across development/holdout; no thresholds are tuned in this runner.']}


def run(args):
    args.output.mkdir(parents=True, exist_ok=True)
    ledger = args.output/'calls.jsonl'
    rows=[json.loads(line) for line in ledger.read_text(encoding='utf-8').splitlines()] if ledger.exists() else []
    if any('error' in row for row in rows):
        raise ValueError('Reconcile the previous failed/possibly billed call before resuming this ledger.')
    if not math.isfinite(args.budget) or not 0 < args.budget <= 1:
        raise ValueError('Pilot budget must be positive and no greater than $1.')
    completed={r['request_sha256'] for r in rows}
    spent=sum(r.get('cost',0) for r in rows)
    connection=json.loads(args.connection.read_text(encoding='utf-8-sig'))
    count=0
    suite=cases()
    if args.suite=='pronunciation':
        from pronunciation_cases import cases as pronunciation_cases
        suite=pronunciation_cases()
    elif args.suite=='pronunciation-final':
        from pronunciation_cases import final_holdout
        suite=final_holdout()
    for c in suite:
        instruction, default_choices=TASKS[c['task']]
        choices=c.get('choices') or default_choices
        for tier,state in c['contexts'].items():
            if args.marked_targets and c.get('word'):
                matches=list(re.finditer(r'(?<!\w)'+re.escape(c['word'])+r'(?!\w)',state,re.IGNORECASE))
                if not matches:raise ValueError('The marked target is absent from the context.')
                match=matches[-1]
                state={'text':state,'target':{'word':match.group(),'start':match.start(),'end':match.end()}}
                instruction=TASKS[c['task']][0]+' Decide only the reading of the marked target occurrence, using text as context. Do not classify the entire passage.'
            payload={'state':state,'questions':{'decision':{'type':'choice','instructions':instruction,'criteria':choices}}}
            if args.independent_gates:
                payload['questions']['unique']={'type':'noul',
                    'instructions':'The candidate readings are '+json.dumps({k:v for k,v in choices.items() if k!='unknown'})+'. Does the supplied state establish exactly one of these readings, without relying on a common default or guessing? Consider whether another candidate would still be grammatically and semantically possible.',
                    'criteria':{'true':'The explicit available context distinguishes exactly one supplied candidate reading.',
                                'false':'Multiple readings remain possible, the context is missing, or the evidence conflicts.'}}
            request_hash=digest({'schema':1,'payload':payload,'case':c['id'],'tier':tier})
            if request_hash in completed:continue
            # Max $0.01 in-flight exposure is deliberately far above observed calls.
            # An ambiguous timeout stops the run; do not retry a potentially billed call.
            if spent + .01 > args.budget or (args.limit and count>=args.limit):break
            row={'case':c['id'],'task':c['task'],'tier':tier,'split':c['split'],
                 'expected':c['expected'][tier],'baseline':baseline(c['task'],state),'request_sha256':request_hash}
            t=time.monotonic()
            try:
                req=urllib.request.Request(args.url,data=json.dumps(payload).encode(),headers={**connection['headers'],'Content-Type':'application/json'})
                with urllib.request.urlopen(req,timeout=40) as response:
                    data=json.load(response)
                    usage=data.get('usage',{})
                    cost=float(usage['cost'])
                    if not math.isfinite(cost) or cost<0:raise ValueError('Invalid billing')
                    row.update(cost=cost,input_tokens=int(usage['input_tokens']),output_tokens=int(usage['output_tokens']),model=data.get('model'))
                    row['answer']=validate_answer(data['answers']['decision'],choices)
                    if args.independent_gates:
                        probability=data['answers']['unique']['noul']
                        if not isinstance(probability,(float,int)) or not math.isfinite(probability) or not 0<=probability<=1:
                            raise ValueError('Invalid uniqueness probability')
                        row['unique_probability']=probability
                    spent+=cost
            except Exception as exc:
                # Never print headers, connection files or diagnostic request bodies.
                row['error']=type(exc).__name__
            row['seconds']=time.monotonic()-t
            rows.append(row);count+=1
            with ledger.open('a',encoding='utf-8') as f:f.write(json.dumps(row)+'\n')
            if 'error' in row:
                print('Stopped after ambiguous/failed request:',row['case'],tier,row['error'],flush=True)
                (args.output/'summary.json').write_text(json.dumps(summarize(rows),indent=2),encoding='utf-8')
                return
            if count%20==0:print(f'{len(rows)} decisions, billed ${spent:.6f}',flush=True)
        if spent+.01>args.budget or (args.limit and count>=args.limit):break
    (args.output/'summary.json').write_text(json.dumps(summarize(rows),indent=2),encoding='utf-8')
    print(f'Completed {len(rows)} decisions; billed ${spent:.6f}; output {args.output}',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--connection',type=Path,default=Path(r'C:\Booksmith\providers\litellm\jev\grok-connection-private.json'))
    parser.add_argument('--url',default='http://127.0.0.1:4000/Jev/decide')
    parser.add_argument('--budget',type=float,default=.50)
    parser.add_argument('--limit',type=int,default=0)
    parser.add_argument('--suite',choices=['all','pronunciation','pronunciation-final'],default='all')
    parser.add_argument('--independent-gates',action='store_true')
    parser.add_argument('--marked-targets',action='store_true')
    run(parser.parse_args())
