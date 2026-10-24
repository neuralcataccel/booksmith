import math

import pytest

from evaluate_jev import summarize, validate_answer
from jev_cases import TASKS, cases


def test_all_four_contexts_and_both_splits_exist_for_every_opportunity():
    rows = cases()
    assert len(TASKS) == 14
    assert len({c['id'] for c in rows}) == len(rows)
    for task in TASKS:
        selected=[c for c in rows if c['task']==task]
        assert len(selected)>=5
        assert {c['split'] for c in selected}=={'development','holdout'}
        for case in selected:
            assert set(case['contexts'])==set(case['expected'])=={'token','sentence','paragraph','scene'}
            choices=case['choices'] or TASKS[task][1]
            assert set(case['expected'].values())<=set(choices)
            assert 'unknown' in choices
    # Same book/case context variants must not cross the split boundary.
    groups={}
    for c in rows:groups.setdefault(c['group'],set()).add(c['split'])
    assert all(len(s)==1 for s in groups.values())


@pytest.mark.parametrize('change',['missing','extra','negative','nan','sum','choice'])
def test_provider_response_validation(change):
    answer={'choice':'a','confidence':1,'probabilities':{'a':1,'unknown':0}}
    if change=='missing':del answer['probabilities']['unknown']
    elif change=='extra':answer['probabilities']['other']=0
    elif change=='negative':answer['probabilities']={'a':1.1,'unknown':-.1}
    elif change=='nan':answer['confidence']=math.nan
    elif change=='sum':answer['probabilities']={'a':.2,'unknown':.2}
    else:answer['choice']='invented'
    with pytest.raises(ValueError):validate_answer(answer,{'a':'A','unknown':'Unknown'})


def test_summary_distinguishes_abstention_wrong_changes_and_provider_failure():
    def row(expected,chosen=None):
        r={'task':'pronunciation','tier':'token','split':'holdout','expected':expected,
           'baseline':'unknown','seconds':.2,'cost':.001}
        if chosen is not None:r['answer']={'choice':chosen}
        else:r['error']='TimeoutError'
        return r
    result=summarize([row('unknown','unknown'),row('a','a'),row('unknown','a'),row('a')])
    group=result['groups'][0]
    assert group['n']==4 and group['valid']==3 and group['correct']==2
    assert group['resolved']==2 and group['wrong_changes']==1 and group['correct_abstentions']==1
    assert result['errors']==1 and result['billed_cost']==.004


def test_pronunciation_contexts_and_final_word_groups_are_separate():
    from pronunciation_cases import cases as pronunciation_cases, final_holdout
    development=pronunciation_cases();final=final_holdout()
    assert len(development)==28 and len(final)==28
    assert {c['word'] for c in development}.isdisjoint({c['word'] for c in final})
    assert all(set(c['contexts'])==set(c['expected'])=={'token','phrase','sentence','paragraph','scene'} for c in development)
    assert all(set(c['contexts'])==set(c['expected'])=={'paragraph'} for c in final)
    groups={}
    for c in development:
        groups.setdefault(c['group'],set()).add(c['split'])
        assert set(c['expected'].values())<=set(c['choices'])
        assert set(c['candidate_phonemes'])=={'a','b'}
    assert all(len(split)==1 for split in groups.values())
