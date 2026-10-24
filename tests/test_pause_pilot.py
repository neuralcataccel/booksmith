import json

import numpy as np
import pytest

import pause_pilot as pilot


def test_inserted_silence_preserves_every_original_sample_and_records_shifted_positions():
    audio=np.arange(100,dtype=np.float32)/100
    result,receipt=pilot.insert_pauses(audio,[{'sample':20,'extra_ms':20},{'sample':70,'extra_ms':60}],rate=1000)
    assert len(result)==180 and [r['output_sample'] for r in receipt]==[20,90]
    np.testing.assert_array_equal(result[20:40],np.zeros(20))
    np.testing.assert_array_equal(result[90:150],np.zeros(60))
    np.testing.assert_array_equal(np.concatenate([result[:20],result[40:90],result[150:]]),audio)


@pytest.mark.parametrize('edit',[{'sample':-1,'extra_ms':20},{'sample':101,'extra_ms':20},
    {'sample':10.5,'extra_ms':20},{'sample':10,'extra_ms':-1},{'sample':10,'extra_ms':4001},
    {'sample':10,'extra_ms':float('nan')}])
def test_bad_pause_edits_are_rejected_before_producing_audio(edit):
    with pytest.raises(ValueError):pilot.insert_pauses(np.ones(100,dtype=np.float32),[edit])


def test_no_pause_plan_is_bit_identical():
    audio=np.random.default_rng(1).standard_normal(1000).astype(np.float32)
    result,receipt=pilot.insert_pauses(audio,[])
    np.testing.assert_array_equal(result,audio);assert receipt==[]


def test_contexts_refer_to_the_same_original_boundary_without_rewriting_source():
    rows=pilot.candidates()
    assert len(rows)==12 and len({r['id'] for r in rows})==12
    for row in rows:
        original=row['text']
        assert pilot.marked(row,'passage').replace('<PAUSE_TARGET/>','')==original
        assert pilot.marked(row,'local').count('<PAUSE_TARGET/>')==1
        assert len(pilot.marked(row,'local'))<=130+len('<PAUSE_TARGET/>')
        assert original[row['punctuation_offset']] in ',;:.!?'
        assert row['text']==original


def test_model_disagreement_cannot_insert_a_long_pause(tmp_path):
    answers=[];review={}
    for row in pilot.candidates():
        for tier in ['local','passage']:answers.append({'id':row['id'],'tier':tier,'answer':{'choice':'reflect'}})
        review[row['id']]={'choice':'keep','reason':'No justified thinking pause.'}
    (tmp_path/'decisions.json').write_text(json.dumps(answers))
    (tmp_path/'sonnet-review.json').write_text(json.dumps({'answers':review}))
    assert all(row['selected']=='keep' for row in pilot.selected(tmp_path))


def test_pilot_cache_cannot_silently_reuse_audio_after_prompt_or_policy_change(tmp_path,monkeypatch):
    pilot.ensure_suite(tmp_path);pilot.ensure_suite(tmp_path)
    monkeypatch.setattr(pilot,'INSTRUCTIONS',pilot.INSTRUCTIONS+' A different policy.')
    with pytest.raises(ValueError,match='fresh experiment directory'):pilot.ensure_suite(tmp_path)
