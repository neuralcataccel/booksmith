"""Typed hosted decisions with durable reservations and no automatic billing-ambiguous retry."""
import json
import math
from pathlib import Path
import time
import datetime
import urllib.request

from reading_plan import digest
from settings import CONFIG
import store

MODEL = 'typesafe/jev-1.13'
RESERVE = .01
TOTAL_LIMIT = 1.0
OWNER_LIMIT = .25


class Unavailable(ValueError):
    pass


class NotSent(Exception):
    pass


def validate_answer(answer, choices):
    if not isinstance(answer,dict) or answer.get('choice') not in choices:
        raise ValueError('Invalid choice')
    probabilities=answer.get('probabilities',{})
    confidence=answer.get('confidence')
    if (not isinstance(probabilities,dict) or set(probabilities)!=set(choices)
        or type(confidence) not in (float,int) or not math.isfinite(confidence) or not 0<=confidence<=1):
        raise ValueError('Invalid decision distribution')
    if (any(type(p) not in (float,int) or not math.isfinite(p) or not 0<=p<=1 for p in probabilities.values())
        or abs(sum(probabilities.values())-1)>.02):
        raise ValueError('Invalid probabilities')
    return answer


def connection_path():
    return Path(CONFIG.get('jev_connection_file',r'C:\Booksmith\providers\litellm\jev\grok-connection-private.json'))


def available():
    return connection_path().is_file()


def transport(payload):
    try:connection=json.loads(connection_path().read_text(encoding='utf-8-sig'))
    except (OSError,ValueError):raise NotSent() from None
    if not isinstance(connection.get('headers'),dict):raise NotSent()
    request=urllib.request.Request(CONFIG.get('jev_url','http://127.0.0.1:4000/Jev/decide'),
        data=json.dumps(payload).encode(),headers={**connection['headers'],'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=30) as response:
        return json.load(response)


def decide(payload, owner, choices, send=None, additional_choices=None):
    encoded=json.dumps(payload,ensure_ascii=False).encode()
    if len(encoded)>24000:
        raise Unavailable('This passage is too large for a pronunciation check.')
    key=digest({'schema':1,'model':MODEL,'payload':payload})
    now=time.time()
    with store.db() as con:
        con.execute('BEGIN IMMEDIATE')
        row=con.execute('SELECT * FROM jev_calls WHERE request_key=?',(key,)).fetchone()
        if row:
            if row['state']=='complete':return {**json.loads(row['result']),'cached':True,'new_cost':0,'request_key':key}
            raise Unavailable('A previous check is unavailable; its request will not be repeated automatically.')
        if con.execute("SELECT 1 FROM jev_calls WHERE state IN ('sending','unknown') LIMIT 1").fetchone():
            raise Unavailable('Pronunciation suggestions are paused while a previous request is reconciled.')
        monthly=CONFIG.get('jev_monthly_limit_usd')
        if monthly is not None:
            now_utc=datetime.datetime.now(datetime.timezone.utc)
            month=now_utc.replace(day=1,hour=0,minute=0,second=0,microsecond=0).timestamp()
            totals=con.execute('SELECT coalesce(sum(cost+reserved),0) FROM jev_calls WHERE created>=?',(month,)).fetchone()[0]
        else:totals=con.execute('SELECT coalesce(sum(cost),0) FROM jev_calls').fetchone()[0]
        owned=con.execute('SELECT coalesce(sum(cost),0) FROM jev_calls WHERE owner=?',(owner,)).fetchone()[0]
        if totals+RESERVE>(monthly if monthly is not None else TOTAL_LIMIT) or owned+RESERVE>CONFIG.get('jev_owner_limit_usd',OWNER_LIMIT):
            raise Unavailable('The pronunciation-check spending limit has been reached.')
        scope=con.execute("SELECT book_id,json_extract(options,'$.source_sha256') FROM jobs WHERE id=? AND kind='analysis'",(owner,)).fetchone()
        if scope:
            book_spent=con.execute("""SELECT coalesce(sum(c.cost),0) FROM jev_calls c JOIN jobs j ON c.owner=j.id
                WHERE j.kind='analysis' AND j.book_id=? AND json_extract(j.options,'$.source_sha256')=?""",tuple(scope)).fetchone()[0]
            if book_spent+RESERVE>CONFIG.get('jev_owner_limit_usd',OWNER_LIMIT):
                raise Unavailable('This book has reached its pronunciation-check spending limit.')
        con.execute('INSERT INTO jev_calls VALUES (?,?,?,NULL,0,?,?,?)',(key,owner,'sending',RESERVE,now,now))
    known_cost=None;started=time.monotonic()
    try:
        data=(send or transport)(payload)
        usage=data['usage'];cost=usage['cost'];tokens=usage['input_tokens']
        if type(cost) not in (int,float) or not math.isfinite(cost) or cost<0:
            raise ValueError('Invalid billing')
        known_cost=float(cost)
        if type(tokens) is not int or tokens<0 or known_cost>RESERVE:
            raise ValueError('Unexpected usage')
        model=data['model']
        if not isinstance(model,str) or not (model==MODEL or model.startswith(MODEL+'-')):
            raise ValueError('Unexpected decision model')
        answer=validate_answer(data['answers']['decision'],choices)
        additional={key:validate_answer(data['answers'][key],allowed) for key,allowed in (additional_choices or {}).items()}
        unique=data['answers']['unique']['noul']
        if type(unique) not in (int,float) or not math.isfinite(unique) or not 0<=unique<=1:
            raise ValueError('Invalid uniqueness probability')
        result={'answer':answer,'unique_probability':unique,'model':model,'cost':known_cost,'input_tokens':tokens,
                'seconds':time.monotonic()-started}
        if additional_choices:result['additional_answers']=additional
        with store.db() as con:
            con.execute("UPDATE jev_calls SET state='complete',result=?,cost=?,reserved=0,updated=? WHERE request_key=?",
                        (json.dumps(result,allow_nan=False),known_cost,time.time(),key))
        return {**result,'cached':False,'new_cost':known_cost,'request_key':key}
    except Exception as exc:
        # Exception messages, bodies, headers and credentials must never enter the journal.
        with store.db() as con:
            con.execute('UPDATE jev_calls SET state=?,result=?,cost=?,updated=? WHERE request_key=?',
                ('invalid' if isinstance(exc,NotSent) or known_cost is not None and known_cost<=RESERVE else 'unknown',
                 json.dumps({'error':type(exc).__name__}),known_cost or 0,time.time(),key))
        raise Unavailable('This pronunciation check was unavailable. The usual reading is preserved.') from None
