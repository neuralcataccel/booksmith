"""Operator-only billing reconciliation; never resends or invents a model answer."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import time

import store


def status():
    with store.db() as c:
        rows=c.execute('SELECT state,count(*) n,sum(cost) cost,sum(reserved) reserved FROM jev_calls GROUP BY state').fetchall()
        blocked=c.execute("SELECT request_key,owner,state FROM jev_calls WHERE state IN ('sending','unknown') ORDER BY created").fetchall()
    return {'known_cost_usd':sum(r['cost'] for r in rows),'states':{r['state']:r['n'] for r in rows},
            'paused':bool(blocked),'unreconciled':[dict(r) for r in blocked]}


def reconcile(receipt):
    """Apply an explicitly supplied provider-usage receipt, retaining its hash/history."""
    receipt=Path(receipt)
    try:
        if receipt.stat().st_size>65536:raise ValueError('Billing receipt is too large.')
        raw=receipt.read_bytes();evidence=json.loads(raw)
    except (OSError,UnicodeError,json.JSONDecodeError):raise ValueError('Billing receipt is unreadable or invalid.') from None
    if (not isinstance(evidence,dict) or set(evidence)!={'schema','request_key','actual_cost_usd','provider_reference'}
        or type(evidence['schema']) is not int or evidence['schema']!=1):
        raise ValueError('Use the documented billing receipt schema.')
    key=evidence['request_key'];cost=evidence['actual_cost_usd'];reference=evidence['provider_reference']
    if not isinstance(key,str) or not re.fullmatch(r'[0-9a-f]{64}',key):raise ValueError('Invalid request identity.')
    if type(cost) not in (float,int) or not math.isfinite(cost) or cost<0:raise ValueError('The verified cost must be finite and nonnegative.')
    if not isinstance(reference,str) or not reference.strip() or len(reference)>200 or any(ord(ch)<32 for ch in reference):
        raise ValueError('A provider usage reference is required.')
    now=time.time()
    with store.db() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT * FROM jev_calls WHERE request_key=?',(key,)).fetchone()
        if not row or row['state'] not in {'sending','unknown'}:
            raise ValueError('Only an unreconciled request can be reconciled; completed history is immutable.')
        active=c.execute("SELECT 1 FROM jobs WHERE id=? AND status='running'",(row['owner'],)).fetchone()
        if active or row['state']=='sending' and now-row['updated']<60:
            raise ValueError('This request may still be running. Let its owner stop before reconciliation.')
        audit={'schema':1,'request_key':key,'previous_state':row['state'],'previous_cost_usd':row['cost'],
               'actual_cost_usd':float(cost),'previous_reserved_usd':row['reserved'],
               'provider_reference':reference,'evidence_sha256':hashlib.sha256(raw).hexdigest(),'created':now}
        c.execute('INSERT INTO meta(key,value) VALUES (?,?)',('jev_reconciliation/'+key,json.dumps(audit,allow_nan=False)))
        c.execute("UPDATE jev_calls SET state='reconciled',cost=?,reserved=0,updated=? WHERE request_key=?",(cost,now,key))
    return {'request_key':key,'state':'reconciled','actual_cost_usd':float(cost),'resend_allowed':False}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);commands=p.add_subparsers(dest='command',required=True)
    commands.add_parser('status');command=commands.add_parser('reconcile');command.add_argument('receipt',type=Path)
    args=p.parse_args()
    try:result=status() if args.command=='status' else reconcile(args.receipt)
    except ValueError as exc:p.exit(1,str(exc)+'\n')
    print(json.dumps(result,indent=2,allow_nan=False))
