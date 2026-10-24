"""Prepare a bounded capped-passage retry and assemble a separate Qwen audition.

Operator CLI only. Does not change the family queue, index auditions or publish to Plex.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

import reading_plan
from extract import split_text
from audition_qwen import part_identity


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def prepare(plan_path, base, retry):
    plan = reading_plan.validate(read(plan_path))
    if len(plan['chapters']) != 1:
        raise ValueError('Choose one original story.')
    report = read(base / 'qwen-red-riding.json')
    if report.get('state') != 'rendered' or report.get('source_plan_sha256') != plan['sha256']:
        raise ValueError('Original audition does not match the supplied plan.')
    texts = [p['text'] for s in plan['chapters'][0]['spans'] for p in s['pieces']]
    blocks, chunks, mapping = [], [], []
    for index, text in enumerate(texts):
        receipt = read(base / 'qwen-red-riding-parts' / f'{index:04}.json')
        if receipt['identity'] != part_identity(text, report['direction'], index):
            raise ValueError('Original part does not match the source text/settings.')
        # 512 tokens at this pinned codec's 12.5 Hz yielded 40.88s capped parts.
        # Duration identifies retry candidates, not a proof that their text was cut.
        if receipt['audio_seconds'] < 40.8:
            continue
        pieces = split_text(text, max_chars=160)
        if ' '.join(pieces) != text:
            raise ValueError('Retry segmentation changed source text.')
        blocks.append({'text': text, 'kind': 'audition_retry',
                       'locator': {'original_chunk': index, 'original_plan': plan['sha256']},
                       'chunk_start': len(chunks), 'chunk_count': len(pieces)})
        mapping.append({'original_chunk': index, 'retry_start': len(chunks), 'retry_count': len(pieces)})
        chunks.extend(pieces)
    if not chunks:
        raise ValueError('No capped-passage candidates found.')
    extracted = {'version': plan['extractor'], 'chapters': [
        {'title': 'Three capped-passage retries' if len(mapping) == 3 else 'Capped-passage retries',
         'chunks': chunks, 'blocks': blocks}]}
    partial = reading_plan.build(extracted, plan['source_sha256'], plan['profile'])
    retry.mkdir(parents=True, exist_ok=True)
    reading_plan.save(retry / 'reading-plan.json', partial)
    receipt = {'parent_plan_sha256': plan['sha256'], 'mapping': mapping}
    target = retry / 'mapping.json'
    if target.exists() and read(target) != receipt:
        raise ValueError('Retry mapping changed; use a fresh directory.')
    target.write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    return plan, report, texts, partial, mapping


def assemble(args):
    import numpy as np
    import soundfile as sf
    plan, original, texts, partial, mapping = prepare(args.plan, args.base, args.retry)
    if args.prepare_only:
        print(json.dumps({'retry_plan': str(args.retry / 'reading-plan.json'),
                          'original_chunks': [m['original_chunk'] for m in mapping],
                          'retry_chunks': sum(m['retry_count'] for m in mapping)}))
        return
    revised = read(args.retry / 'qwen-red-riding.json')
    if revised.get('state') != 'rendered' or revised.get('source_plan_sha256') != partial['sha256']:
        raise ValueError('Retry audition is incomplete or belongs to a different plan.')
    for key in ('upstream_sha', 'model_revision', 'speaker', 'direction', 'dtype', 'sample_rate'):
        if original[key] != revised[key]:
            raise ValueError('Retry changed the voice/model settings: ' + key)
    retry_texts = [p['text'] for s in partial['chapters'][0]['spans'] for p in s['pieces']]
    for index, text in enumerate(retry_texts):
        receipt = read(args.retry / 'qwen-red-riding-parts' / f'{index:04}.json')
        if receipt['identity'] != part_identity(text, revised['direction'], index):
            raise ValueError('Retry part does not match its reading plan.')
    replacements = {m['original_chunk']: m for m in mapping}
    arrays, audit = [], []
    rate = original['sample_rate']
    reused_seconds = 0
    for index, text in enumerate(texts):
        row = replacements.get(index)
        if row:
            paths = [args.retry / 'qwen-red-riding-parts' / f'{i:04}.wav'
                     for i in range(row['retry_start'], row['retry_start'] + row['retry_count'])]
        else:
            paths = [args.base / 'qwen-red-riding-parts' / f'{index:04}.wav']
            reused_seconds += read(paths[0].with_suffix('.json'))['seconds']
        start = sum(len(a) for a in arrays) / rate
        for path in paths:
            audio, actual_rate = sf.read(path, dtype='float32')
            if actual_rate != rate or audio.ndim != 1 or not len(audio) or not np.isfinite(audio).all():
                raise ValueError('Invalid audition part: ' + str(path))
            receipt = read(path.with_suffix('.json'))
            if abs(len(audio) / rate - receipt['audio_seconds']) > .01:
                raise ValueError('Audition part duration changed: ' + str(path))
            arrays.append(audio)
        audit.append({'chunk': index, 'source_text_sha256': hashlib.sha256(text.encode()).hexdigest(),
                      'retried': bool(row), 'start_seconds': start,
                      'duration': sum(len(a) for a in arrays) / rate - start,
                      'parts': [{'path': str(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in paths]})
    args.output.mkdir(parents=True, exist_ok=True)
    aid = 'qwen-red-riding-resegmented'
    wav = args.output / (aid + '.wav')
    temporary = wav.with_suffix('.tmp.wav')
    sf.write(temporary, np.concatenate(arrays), rate)
    temporary.replace(wav)
    mp3 = args.output / (aid + '.mp3')
    encoded = mp3.with_suffix('.tmp.mp3')
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', '-i', str(wav),
                    '-c:a', 'libmp3lame', '-b:a', '128k', str(encoded)], check=True, timeout=60)
    encoded.replace(mp3)
    report = {**original, 'id': aid, 'file': mp3.name,
              'audio_seconds': sum(len(a) for a in arrays) / rate,
              'synthesis_seconds': reused_seconds + revised['synthesis_seconds'],
              'incremental_retry_seconds': revised['synthesis_seconds'],
              'total_trial_synthesis_seconds': original['synthesis_seconds'] + revised['synthesis_seconds'],
              'audio_sha256': hashlib.sha256(mp3.read_bytes()).hexdigest(),
              'retry_plan_sha256': partial['sha256'], 'render_index': audit}
    report.update(source_chunks=len(texts), chunks=sum(len(c['parts']) for c in audit))
    # The inherited speed measurement belongs to the initial, incomplete trial.
    report['audio_per_wall_second'] = report['audio_seconds'] / report['synthesis_seconds']
    report['peak_rss_bytes'] = max(original['peak_rss_bytes'], revised['peak_rss_bytes'])
    (args.output / (aid + '.json')).write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('id', 'audio_seconds', 'synthesis_seconds', 'incremental_retry_seconds')}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--retry', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--prepare-only', action='store_true')
    assemble(parser.parse_args())
