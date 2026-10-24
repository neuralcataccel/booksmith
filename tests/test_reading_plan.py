import copy
import hashlib
import json

import pytest

import extract
import reading_plan
import speech
from test_chapters import make_book


def structured_fixture(tmp_path):
    repeated = 'The door stood open.'
    long = 'A single paragraph keeps going. ' * 25
    path = make_book(tmp_path, [
        ('one.xhtml', f'<h1 id="one">One</h1><p>{repeated}</p><p>{repeated}</p><p>{long}</p>'),
        ('continuation.xhtml', '<p>Still the first chapter.</p>'),
        ('two.xhtml', '<h1 id="two">Two</h1><p>“Come back,” she said.</p>')],
        [('one.xhtml#one', 'Chapter 1'), ('two.xhtml#two', 'Chapter 2')])
    old = extract.read_epub(path)
    rich = extract.read_epub(path, include_structure=True)
    assert [{k: c[k] for k in ('title', 'chunks')} for c in rich] == old
    return {'version': extract.EXTRACTOR_VERSION, 'chapters': rich}, hashlib.sha256(path.read_bytes()).hexdigest()


def test_source_spans_cover_repeated_paragraphs_and_continuations(tmp_path):
    extracted, source_hash = structured_fixture(tmp_path)
    plan = reading_plan.build(extracted, source_hash, {'voice': 'test'})
    spans = [s for c in plan['chapters'] for s in c['spans']]
    repeats = [s for s in spans if s['text'] == 'The door stood open.']
    assert len(repeats) == 2 and repeats[0]['id'] != repeats[1]['id']
    assert len(next(s for s in spans if s['text'].startswith('A single'))['pieces']) > 1
    assert plan['chapters'][0]['spans'][-1]['locator']['document'] == 'continuation.xhtml'
    assert reading_plan.validate(plan, extracted) is plan
    assert reading_plan.build(extracted, source_hash, {'voice': 'test'}) == plan


@pytest.mark.parametrize('change', ['drop', 'repeat', 'reorder', 'rewrite', 'truncate'])
def test_coverage_gate_rejects_bad_plans_even_with_recomputed_hash(tmp_path, change):
    extracted, source_hash = structured_fixture(tmp_path)
    plan = reading_plan.build(extracted, source_hash, {})
    spans = plan['chapters'][0]['spans']
    if change == 'drop':
        del spans[1]
    elif change == 'repeat':
        spans.insert(1, copy.deepcopy(spans[0]))
    elif change == 'reorder':
        spans[0], spans[1] = spans[1], spans[0]
    elif change == 'rewrite':
        spans[1]['pieces'][0]['text'] = 'Different text.'
    else:
        spans[-1]['pieces'].pop()
    plan['sha256'] = reading_plan.fingerprint(plan)
    with pytest.raises(ValueError):
        reading_plan.validate(plan, extracted)


def test_profile_snapshot_cannot_mutate_or_replace_existing_job(tmp_path):
    extracted, source_hash = structured_fixture(tmp_path)
    profile = {'settings': {'voice': 'first'}}
    plan = reading_plan.build(extracted, source_hash, profile)
    profile['settings']['voice'] = 'second'
    assert plan['profile']['settings']['voice'] == 'first'
    path = tmp_path / 'reading-plan.json'
    reading_plan.save(path, plan)
    stamp = path.stat().st_mtime_ns
    reading_plan.save(path, json.loads(path.read_text(encoding='utf-8')))
    assert path.stat().st_mtime_ns == stamp
    replacement = reading_plan.build(extracted, source_hash, profile)
    with pytest.raises(ValueError, match='different reading plan'):
        reading_plan.save(path, replacement)


def test_legacy_chunks_are_not_misrepresented_as_paragraphs():
    extracted = {'chapters': [{'title': 'One', 'chunks': ['Same.', 'Same.']}]}
    plan = reading_plan.build(extracted, 'a' * 64, {})
    spans = plan['chapters'][0]['spans']
    assert [s['kind'] for s in spans] == ['legacy_chunk', 'legacy_chunk']
    assert spans[0]['id'] != spans[1]['id']


def test_long_unbroken_word_has_exact_character_coverage(tmp_path, monkeypatch):
    monkeypatch.setattr(extract, 'WORK', tmp_path)
    path = tmp_path / 'long.txt'
    path.write_text('x' * 800 + '\n\nEnd.', encoding='utf-8')
    opts = {'skip_notes': True, 'skip_frontmatter': True}
    ex = extract.extract_book(path, opts, include_structure=True)
    plan = reading_plan.build(ex, hashlib.sha256(path.read_bytes()).hexdigest(), {})
    assert ''.join(p['text'] for p in plan['chapters'][0]['spans'][0]['pieces']) == 'x' * 800


def test_structure_cache_is_separate_without_invalidating_old_text_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(extract, 'WORK', tmp_path)
    path = tmp_path / 'source.txt'
    path.write_text('One.\n\nTwo.', encoding='utf-8')
    opts = {'skip_notes': True, 'skip_frontmatter': True}
    old = extract.extract_book(path, opts)
    rich = extract.extract_book(path, opts, include_structure=True)
    assert old['key'] != rich['key']
    assert old['chapters'][0]['chunks'] == rich['chapters'][0]['chunks']
    assert 'blocks' not in old['chapters'][0]
    assert len(rich['chapters'][0]['blocks']) == 2
    assert extract.extract_book(path, opts) == old


def test_engine_descriptor_and_legacy_profile_do_not_claim_expression():
    options = {'voice': 'af_heart', 'style': 'natural', 'pace': 1,
               'skip_notes': True, 'skip_frontmatter': True}
    profile = speech.profile_snapshot(options)
    assert profile['engine']['direction'] is False
    assert profile['pause_policy'] == 'legacy_chunk'
    assert profile['effective_speed'] == 1
