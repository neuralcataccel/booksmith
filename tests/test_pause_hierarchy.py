import json

import numpy as np
import pytest

import pause_hierarchy as h


def decisions(rows,role='reflect'):
    return {r['id']:{'answer':{'choice':role,'confidence':.8}} for r in rows}


def test_sentence_pass_contains_complete_sentence_not_the_paragraph():
    items=h.fixtures()
    assert len(items)==6
    for item in items:
        for row in h.boundaries(item):
            sentence=row['sentence'].replace('<PAUSE_TARGET/>','')
            assert sentence in item['paragraphs'][row['paragraph']]
            assert row['sentence'].count('<PAUSE_TARGET/>')==1
            assert item['text'][row['punctuation_offset']] in ',;:.!?'
    assert items[0]['text']==h.pilot.passages()[0]['text']


def test_paragraph_editor_can_veto_clustered_holds_and_select_a_previously_missed_beat():
    rows=h.boundaries(h.fixtures()[0]);variants=h.plans(rows,decisions(rows))
    assert any(all(v['target_ms']==0 for v in p.values()) for p in variants.values())
    assert any(sum(v['role']=='reflect' for v in p.values())==1 for p in variants.values())
    # Even an all-keep sentence pass can be revised in the paragraph pass.
    variants=h.plans(rows,decisions(rows,'keep'))
    assert any(any(v['role']=='scene' for v in p.values()) for p in variants.values())
    assert all(set(p)=={r['id'] for r in rows} for p in variants.values())
    assert max(v['target_ms'] for p in variants.values() for v in p.values())<=1200


def test_order_probe_keeps_sources_choices_and_plans_identical():
    item=h.fixtures()[3];rows=[r for r in h.boundaries(item) if r['paragraph']==0]
    first=decisions(rows);alternatives=h.plans(rows,first)
    a,ca=h.paragraph_payload(item,0,rows,first,alternatives)
    b,cb=h.paragraph_payload(item,0,rows,first,alternatives,True)
    assert ca==cb and list(ca)[:-1]==list(reversed(list(cb)[:-1]))
    assert a['state']['target_paragraph']==b['state']['target_paragraph']
    assert h.digest(a)!=h.digest(b)  # Durable cache canonicalizes mapping order.
    assert all(a['state']['target_paragraph'].count(r['id'])==1 for r in rows)
    assert a['state']['next_paragraph']==' '.join(item['paragraphs'][1])


def test_suite_change_cannot_reuse_saved_decisions(tmp_path,monkeypatch):
    h.ensure_suite(tmp_path);h.ensure_suite(tmp_path)
    monkeypatch.setattr(h,'FLOW',{**h.FLOW,'reflect':750})
    with pytest.raises(ValueError,match='fresh experiment'):h.ensure_suite(tmp_path)


def test_mapping_fails_on_punctuation_mismatch_and_does_not_cut_speech():
    audio=np.ones(24000,dtype=np.float32)
    rows=[{'id':'x','punctuation_offset':1}]
    with pytest.raises(ValueError,match='punctuation'):
        h.map_boundaries(audio,24000,[{'phoneme':'?','end':.5}],'a. b',rows)
    result=h.map_boundaries(audio,24000,[{'phoneme':'.','end':.5}],'a. b',rows)
    assert result[0]['status']=='no_safe_quiet_gap' and 'sample' not in result[0]


def test_same_quiet_gap_cannot_receive_two_independent_topups():
    audio=np.ones(24000,dtype=np.float32);audio[10000:14000]=0
    rows=[{'id':'x','punctuation_offset':1},{'id':'y','punctuation_offset':3}]
    result=h.map_boundaries(audio,24000,[{'phoneme':'.','end':.49},{'phoneme':'.','end':.5}],'a. . b',rows)
    assert result[0]['status']=='mapped'
    assert result[1]['status']=='shared_gap_already_mapped'


def test_two_pass_journal_is_resumable_and_unknown_preserves_engine(tmp_path,monkeypatch):
    import jev_client
    sent=[]
    def fake(payload,owner,choices):
        sent.append(payload)
        choice='keep' if 'keep' in choices else 'unknown'
        return {'answer':{'choice':choice,'confidence':.9},'cost':.0001,'new_cost':.0001,
                'request_key':h.digest(payload),'cached':False,
                'seconds':.01,'input_tokens':100,'model':'test-jev'}
    monkeypatch.setattr(jev_client,'decide',fake)
    h.classify(tmp_path);count=len(sent);h.classify(tmp_path)
    assert len(sent)==count
    plans=json.loads((tmp_path/'boundary-plan.json').read_text())
    assert all(r['paragraph_target_ms']==0 for r in plans)
    summary=json.loads((tmp_path/'summary.json').read_text())
    assert summary['unknown_paragraphs']==summary['paragraphs']
    assert count==summary['boundaries']+2*summary['paragraphs']


def test_publication_requires_all_passages_to_have_asr_screening(tmp_path):
    rows=[{'passage':item['id'],'policy':policy,'speech_samples_unchanged':True}
          for item in h.fixtures() for policy in ['baseline','sentence','hierarchy']]
    (tmp_path/'render-report.json').write_text(json.dumps(rows))
    (tmp_path/'asr.json').write_text('[]')
    with pytest.raises(ValueError,match='Complete local ASR'):h.publish(tmp_path)
