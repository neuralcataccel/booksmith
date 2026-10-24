import json
import re

import pytest

import occurrence_phones as phones
import pronunciation
import reading_plan


def occurrence(text, word, choice, nth=0):
    match=list(re.finditer(r'\b'+word+r'\b',text,re.I))[nth]
    return {'id':str(nth)+word,'word':word,'choice':choice,'start':match.start(),'end':match.end()}


@pytest.fixture
def tokenizer():
    from kokoro_onnx.tokenizer import Tokenizer
    return Tokenizer()


def test_two_senses_of_same_word_are_aligned_to_distinct_source_occurrences(tokenizer):
    text='The lead will lead us home.'
    value,outcomes=phones.align(text,[occurrence(text,'lead','metal'),occurrence(text,'lead','guide',1)],tokenizer)
    assert value.count('lˈɛd')==1 and value.count('lˈiːd')==1
    assert [o['status'] for o in outcomes]==['applied','already_correct']
    assert text=='The lead will lead us home.'


def test_repeated_word_only_selected_occurrence_changes(tokenizer):
    text='Yesterday she read it, and tomorrow she will read it again.'
    value,outcomes=phones.align(text,[occurrence(text,'read','past')],tokenizer)
    assert value.count('ɹˈɛd')==1 and value.count('ɹˈiːd')==1
    assert outcomes[0]['status']=='applied'


def test_unrelated_merged_words_do_not_shift_the_selected_occurrence(tokenizer):
    text='The bass swam under the boat. She played the bass in the orchestra.'
    # eSpeak merges words near "the orchestra": source count differs from phone groups.
    _,groups=phones.marked(text,tokenizer,'en-us')
    assert len(groups)!=len(list(phones.WORD.finditer(text)))
    value,outcomes=phones.align(text,[occurrence(text,'bass','fish'),occurrence(text,'bass','music',1)],tokenizer)
    assert value.count('bˈæs')==1 and value.count('bˈeɪs')==1
    assert [o['status'] for o in outcomes]==['applied','already_correct']


def test_numbers_abstain_instead_of_zipping_expanded_word_counts(tokenizer):
    text='She read 1984.'
    value,outcomes=phones.align(text,[occurrence(text,'read','past')],tokenizer)
    assert value is None and outcomes[0]['status']=='number_alignment'


def test_phonemizer_probe_must_not_change_neighbouring_phones(tokenizer):
    text='The shield was made of lead.'
    def changed_neighbour(text,k,lang):
        raw,units=phones.marked(text,k,lang)
        if 'zebra' in text:units[0]='wrong'
        return raw,units
    value,outcomes=phones.align(text,[occurrence(text,'lead','metal')],tokenizer,marker=changed_neighbour)
    assert value is None and outcomes[0]['status']=='unaligned'


def test_dictionary_remaps_offsets_and_has_precedence():
    text='Reed read the letter, then read the note.'
    occurrences=[occurrence(text,'read','past'),occurrence(text,'read','present',1)]
    spoken,mapped,skipped=pronunciation.remap(text,[{'word':'Reed','spoken':'Reed Alexander'}],occurrences)
    assert not skipped and all(spoken[o['start']:o['end']]=='read' for o in mapped)
    spoken,mapped,skipped=pronunciation.remap(text,[{'word':'read','spoken':'red'}],occurrences)
    assert not mapped and len(skipped)==2 and all(o['status']=='dictionary_override' for o in skipped)


def test_contractions_and_quote_punctuation_survive_alignment(tokenizer):
    text='“Don\'t close the door,” she said.'
    value,outcomes=phones.align(text,[occurrence(text,'close','shut')],tokenizer)
    assert outcomes[0]['status'] in {'applied','already_correct'}
    baseline=tokenizer.phonemize(text,lang='en-us')
    assert value is None or value==baseline.replace('klˈoʊs','klˈoʊz')


def test_invalid_offsets_candidates_and_overlap_fail(tokenizer):
    text='She read the letter.'
    o=occurrence(text,'read','past')
    with pytest.raises(ValueError,match='offsets'):phones.align(text,[{**o,'start':0}],tokenizer)
    with pytest.raises(ValueError,match='candidate'):phones.align(text,[{**o,'choice':'arbitrary IPA'}],tokenizer)
    with pytest.raises(ValueError,match='overlap'):phones.align(text,[o,o],tokenizer)


def test_british_voice_is_explicitly_unsupported_for_initial_us_lexicon(tokenizer):
    text='She read the letter.'
    value,outcomes=phones.align(text,[occurrence(text,'read','past')],tokenizer,lang='en-gb')
    assert value is None and outcomes[0]['status']=='unsupported_language'


def test_source_block_identity_survives_anthology_selection_and_resegmentation():
    text='Yesterday she read the letter. Tomorrow she will read it again.'
    block={'text':text,'kind':'p','locator':{'document':'story.xhtml','block':8},'chunk_start':0,'chunk_count':1}
    chapter={'title':'Story','chunks':[text],'blocks':[block]}
    plan=reading_plan.build({'version':'7','chapters':[chapter]},'a'*64,{})
    selected=phones.find(plan,12,0)
    span=plan['chapters'][0]['spans'][0]
    selected[0]['choice']='past'
    part=phones.chunk_occurrences(plan,span,span['pieces'][0],selected[:1])
    assert part[0]['start']==text.index('read')
    changed=json.loads(json.dumps(plan));changed['chapters'][0]['spans'][0]['id']='different-selected-chapter-id'
    assert phones.block_key(plan['source_sha256'],'7',span)==phones.block_key(plan['source_sha256'],'7',changed['chapters'][0]['spans'][0])
    with pytest.raises(ValueError,match='lexicon'):
        phones.chunk_occurrences(plan,span,span['pieces'][0],[{**selected[0],'lexicon_sha256':'stale'}])


def test_renderer_records_the_actual_phone_input_and_rejects_changed_cached_audio(tmp_path,monkeypatch,tokenizer):
    import numpy as np
    import soundfile as sf
    import speech
    seen=[]
    class Engine:
        def __init__(self):self.tokenizer=tokenizer
        def create(self,text,**kwargs):
            seen.append((text,kwargs))
            return np.ones(4800,dtype=np.float32)*.1,24000
    monkeypatch.setattr(speech,'load',Engine)
    text='The lead will lead us home.'
    options={'voice':'am_eric','style':'natural','pace':1,'_pause_seconds':0,'_context_renderer':1,
             '_chunk_occurrences':[occurrence(text,'lead','metal')]}
    path=tmp_path/'sample.wav'
    speech.generate(text,options,path)
    assert seen[0][1]['is_phonemes'] and seen[0][0].count('lˈɛd')==1 and seen[0][0].count('lˈiːd')==1
    receipt=speech.context_receipt(text,options,path)
    assert receipt['outcomes'][0]['status']=='applied'
    sf.write(path,np.ones(4800)*.2,24000)
    with pytest.raises(ValueError,match='changed'):speech.context_receipt(text,options,path)


def test_renderer_keeps_baseline_and_records_an_unapplied_number_context(tmp_path,monkeypatch,tokenizer):
    import numpy as np
    import speech
    seen=[]
    class Engine:
        def __init__(self):self.tokenizer=tokenizer
        def create(self,text,**kwargs):
            seen.append((text,kwargs));return np.ones(4800,dtype=np.float32)*.1,24000
    monkeypatch.setattr(speech,'load',Engine)
    text='She read 1984.'
    options={'voice':'am_eric','style':'natural','pace':1,'_context_renderer':1,
             '_chunk_occurrences':[occurrence(text,'read','past')]}
    path=tmp_path/'sample.wav';speech.generate(text,options,path)
    assert seen[0][0]==text and not seen[0][1].get('is_phonemes',False)
    assert speech.context_receipt(text,options,path)['outcomes'][0]['status']=='number_alignment'
