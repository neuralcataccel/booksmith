from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from xml.etree.ElementTree import fromstring

import pytest

import plex_delivery
from qa_novel import choose_samples, compare_transcript, verify_final, words


def test_audit_samples_cover_each_chapter_and_numbers_without_duplicate_chunks():
    prose = 'This original passage contains enough words to make a useful narration check across several complete sentences and a clearly marked final phrase.'
    chapters = [{'title':'Dedication','chunks':[prose]},
                {'title':'Chapter 1','chunks':[prose, '“' + prose + '”']},
                {'title':'Chapter 2','chunks':['42 ' + prose, '“' + prose + '”']}]
    samples = choose_samples(chapters)
    assert {s['chapter'] for s in samples} == {2, 3}
    assert len({(s['chapter'], s['chunk']) for s in samples}) == len(samples)
    assert sum(s['category'] == 'dialogue' for s in samples) == 2
    assert any(s['category'] == 'numbers' for s in samples)


def test_asr_comparison_ignores_case_and_punctuation_but_keeps_words():
    assert words('“She’s HERE,” he said. 42!') == ["she's", 'here', 'he', 'said', '42']
    assert words('') == []


def test_tail_omission_is_flagged_even_when_aggregate_agreement_passes():
    prefix = 'The girl crossed the garden and opened the gate before she took the long path through the trees toward the cottage beside the river'
    reference = prefix + ' where her grandmother waited by the window'
    result = compare_transcript(reference, prefix)
    assert result['sequence_similarity'] > .8
    assert result['unmatched_reference_tail_words'] == 7
    assert 'possible_missing_ending' in result['listening_reasons']
    assert result['needs_listening']
    complete = compare_transcript(reference, reference.upper() + '.')
    assert complete['sequence_similarity'] == 1 and not complete['needs_listening']


def test_small_asr_ending_variation_does_not_imply_a_missing_passage():
    reference = 'The girl followed the narrow path across the garden and closed the gate very softly'
    result = compare_transcript(reference, reference.replace('very softly', 'quietly'))
    assert not result['needs_listening']


def test_story_titles_can_be_sampled_without_a_chapter_prefix():
    text='The girl opened the door and asked her grandmother why the room seemed so very quiet and dark that morning.'
    assert not choose_samples([{'title':'Little Red Riding Hood','chunks':[text]}])
    assert len(choose_samples([{'title':'Little Red Riding Hood','chunks':[text]}],all_chapters=True))==1


def test_short_story_paragraphs_can_be_checked_without_eighteen_word_chunks():
    text='Yesterday she read the letter before breakfast.'
    assert choose_samples([{'title':'Short story','chunks':['Title',text]}],all_chapters=True)[0]['text']==text


@pytest.mark.parametrize('strip_metadata', [True, False])
def test_plex_audio_audit_compares_payload_when_server_strips_id3(tmp_path, monkeypatch, strip_metadata):
    import mutagen
    import requests
    header = b'ID3 test header'
    payload = bytes(range(256)) * 400
    local = header + payload
    (tmp_path / '001.mp3').write_bytes(local)
    manifest = {'relative_folder':'Author/Novel', 'album_title':'Novel', 'audio_seconds':12,
                'tracks':[{'file':'001.mp3','title':'Chapter 1','number':1}]}
    part = SimpleNamespace(file=str(Path(plex_delivery.plex_path(Path('Author/Novel'))) / '001.mp3'), key='/part')
    track = SimpleNamespace(index=1, title='Chapter 1', media=[SimpleNamespace(parts=[part])])
    server = Mock(_token='test-token')
    server.fetchItem.return_value.tracks.return_value = [track]
    server.query.return_value = fromstring('<MediaContainer><Setting id="enableTrackOffsets" value="true"/></MediaContainer>')
    monkeypatch.setattr(plex_delivery, 'connect', lambda: server)
    audio = Mock(info=SimpleNamespace(length=12, frame_offset=len(header)))
    audio.get.side_effect = {'title':['Chapter 1'], 'album':['Novel'], 'tracknumber':['1/1']}.get
    monkeypatch.setattr(mutagen, 'File', lambda *a, **kw: audio)
    def response(url, headers, **kw):
        start, end = map(int, headers['Range'].removeprefix('bytes=').split('-'))
        body = payload if strip_metadata else local
        return SimpleNamespace(status_code=206, content=body[start:end+1],
                               headers={'Content-Range':f'bytes {start}-{end}/{len(body)}'})
    monkeypatch.setattr(requests, 'get', response)
    report = verify_final({'plex_key':'7'}, manifest, tmp_path)
    assert report['range_delivery_passed'] and report['embedded_tags_correct']
    assert report['range_delivery'][0]['metadata_stripped'] == strip_metadata
    server.fetchItem.return_value.tracks.return_value = [track, track]
    assert not verify_final({'plex_key':'7'}, manifest, tmp_path)['plex_track_order_correct']
