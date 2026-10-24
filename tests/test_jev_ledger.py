import json
import time

import pytest

import jev_client
import jev_ledger
import annotations
import store
from reading_plan import digest
from test_annotations import library, response


def receipt(tmp_path,key,cost=.002):
    path=tmp_path/'receipt.json'
    path.write_text(json.dumps({'schema':1,'request_key':key,'actual_cost_usd':cost,'provider_reference':'test-provider-usage-entry'}))
    return path


def test_reconciliation_records_actual_cost_and_never_reissues_the_unknown_request(library,tmp_path):
    request={'questions':{'decision':{'criteria':{'a':'A','unknown':'Unknown'}}}}
    def timeout(p):raise TimeoutError('fake timeout')
    with pytest.raises(jev_client.Unavailable):jev_client.decide(request,'test-owner',{'a','unknown'},send=timeout)
    key=digest({'schema':1,'model':jev_client.MODEL,'payload':request})
    assert jev_ledger.status()['paused']
    result=jev_ledger.reconcile(receipt(tmp_path,key))
    assert result['actual_cost_usd']==.002 and not result['resend_allowed']
    assert not jev_ledger.status()['paused'] and jev_ledger.status()['known_cost_usd']==.002
    with pytest.raises(jev_client.Unavailable):jev_client.decide(request,'test-owner',{'a','unknown'},send=lambda p:1/0)
    changed={**request,'different':True}
    assert jev_client.decide(changed,'test-owner',{'a','unknown'},send=response)['new_cost']==.00001
    with store.db() as c:
        audit=json.loads(c.execute('SELECT value FROM meta WHERE key=?',('jev_reconciliation/'+key,)).fetchone()[0])
        assert audit['evidence_sha256'] and audit['previous_state']=='unknown'
        assert json.loads(c.execute('SELECT result FROM jev_calls WHERE request_key=?',(key,)).fetchone()[0])=={'error':'TimeoutError'}
    with pytest.raises(ValueError,match='immutable'):jev_ledger.reconcile(receipt(tmp_path,key,0))


def test_receipt_validation_and_live_reservations_cannot_be_bypassed(library,tmp_path):
    key='a'*64
    with store.db() as c:c.execute('INSERT INTO jev_calls VALUES (?,?,?,NULL,0,.01,?,?)',(key,'test-owner','sending',time.time(),time.time()))
    with pytest.raises(ValueError,match='still be running'):jev_ledger.reconcile(receipt(tmp_path,key))
    for bad in [float('nan'),-1,True]:
        with pytest.raises(ValueError,match='finite'):jev_ledger.reconcile(receipt(tmp_path,key,bad))
    with store.db() as c:
        c.execute('UPDATE jev_calls SET updated=0')
        c.execute("INSERT INTO jobs(id,kind,title,author,options,edition_key,status,created,updated) VALUES ('test-owner','analysis','Test','Author','{}','test','running',0,0)")
    with pytest.raises(ValueError,match='still be running'):jev_ledger.reconcile(receipt(tmp_path,key))
    assert jev_ledger.status()['states']=={'sending':1}


def test_reconciled_actual_charge_still_counts_against_budget(library,tmp_path):
    key='b'*64
    with store.db() as c:c.execute('INSERT INTO jev_calls VALUES (?,?,?,NULL,0,.01,0,0)',(key,'owner','unknown'))
    jev_ledger.reconcile(receipt(tmp_path,key,1.01))
    assert not jev_ledger.status()['paused']
    with pytest.raises(jev_client.Unavailable,match='spending'):jev_client.decide({'new':1},'owner',{},send=lambda p:1/0)


def test_review_shows_verified_billing_without_rewriting_original_report(library,tmp_path,monkeypatch):
    book,options,_=library
    def timeout(p):raise TimeoutError('fake timeout')
    monkeypatch.setattr(jev_client,'transport',timeout)
    task=annotations.create(book,options,0);annotations.process(store.claim_next(),lambda jid:None)
    report=annotations.WORK/task['id']/'analysis.json';original=report.read_bytes()
    with store.db() as c:key=c.execute("SELECT request_key FROM jev_calls WHERE state='unknown'").fetchone()[0]
    jev_ledger.reconcile(receipt(tmp_path,key,.003))
    coverage=annotations.listing(book['id'])['coverage']
    assert coverage['billing_state']=='reconciled' and coverage['new_cost']==.003
    assert coverage['billing_snapshot_state']=='unreconciled' and report.read_bytes()==original
    assert all(r['suggestion'] is None for r in annotations.listing(book['id'])['items'])
