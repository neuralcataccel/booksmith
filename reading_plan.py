"""Versioned source spans and an immutable narration snapshot, independent of TTS."""
import hashlib
import json
import re

SCHEMA = 1


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def fingerprint(plan):
    return digest({k: v for k, v in plan.items() if k != 'sha256'})


def build(extracted, source_sha256, profile):
    if not re.fullmatch(r'[0-9a-f]{64}', source_sha256):
        raise ValueError('The reading plan needs a source SHA-256.')
    # JSON round-trip prevents later mutations of caller-owned nested settings.
    plan = {'schema': SCHEMA, 'source_sha256': source_sha256,
            'extractor': extracted.get('version', '6'),
            'profile': json.loads(json.dumps(profile, allow_nan=False)), 'chapters': []}
    for ci, chapter in enumerate(extracted['chapters']):
        blocks = chapter.get('blocks')
        if blocks is None:
            # Old extraction caches have no paragraph provenance. Do not invent it.
            blocks = [{'text': text, 'kind': 'legacy_chunk',
                       'locator': {'chapter': ci, 'chunk': ti}, 'chunk_start': ti, 'chunk_count': 1}
                      for ti, text in enumerate(chapter['chunks'])]
        spans = []
        for block in blocks:
            start = block['chunk_start']
            chunks = chapter['chunks'][start:start + block['chunk_count']]
            cursor, pieces = 0, []
            for text in chunks:
                while cursor < len(block['text']) and block['text'][cursor].isspace():
                    cursor += 1
                if block['text'][cursor:cursor + len(text)] != text:
                    raise ValueError('Extraction chunks do not cover their source block.')
                pieces.append({'text': text, 'start': cursor, 'end': cursor + len(text)})
                cursor += len(text)
            if block['text'][cursor:].strip():
                raise ValueError('Extraction omitted the end of a source block.')
            identity = [source_sha256, plan['extractor'], ci, block['locator']]
            spans.append({'id': digest(identity), **block, 'pieces': pieces})
        plan['chapters'].append({'title': chapter['title'], 'spans': spans})
    plan['sha256'] = fingerprint(plan)
    validate(plan, extracted)
    return plan


def validate(plan, extracted=None):
    if plan.get('schema') != SCHEMA or plan.get('sha256') != fingerprint(plan):
        raise ValueError('Unsupported or changed reading plan.')
    seen = set()
    if not plan.get('chapters'):
        raise ValueError('The reading plan is empty.')
    for ci, chapter in enumerate(plan['chapters']):
        next_chunk, texts = 0, []
        for span in chapter['spans']:
            identity = digest([plan['source_sha256'], plan['extractor'], ci, span['locator']])
            if span['id'] != identity or span['id'] in seen:
                raise ValueError('Source span identity is invalid or repeated.')
            seen.add(span['id'])
            if span['chunk_start'] != next_chunk or span['chunk_count'] != len(span['pieces']) or not span['pieces']:
                raise ValueError('Source spans are missing or out of order.')
            cursor = 0
            for piece in span['pieces']:
                a, b = piece['start'], piece['end']
                if a < cursor or b <= a or b > len(span['text']) or span['text'][cursor:a].strip() or span['text'][a:b] != piece['text']:
                    raise ValueError('Source piece coverage is invalid.')
                texts.append(piece['text'])
                cursor = b
            if span['text'][cursor:].strip():
                raise ValueError('Source piece coverage is incomplete.')
            next_chunk += span['chunk_count']
        if extracted is not None:
            expected = extracted['chapters'][ci]
            if chapter['title'] != expected['title'] or texts != expected['chunks']:
                raise ValueError('The reading plan changed extraction order or text.')
    if extracted is not None and len(plan['chapters']) != len(extracted['chapters']):
        raise ValueError('The reading plan changed the chapter count.')
    return plan


def save(path, plan):
    validate(plan)
    if path.exists():
        prior = json.loads(path.read_text(encoding='utf-8'))
        validate(prior)
        if prior != plan:
            raise ValueError('This job already has a different reading plan. Start a new edition.')
        return
    temp = path.with_suffix('.tmp.json')
    temp.write_text(json.dumps(plan, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    temp.replace(path)


def to_extracted(plan):
    validate(plan)
    chapters=[{'title':c['title'],'chunks':[p['text'] for s in c['spans'] for p in s['pieces']],
               'blocks':[{k:v for k,v in s.items() if k not in {'id','pieces'}} for s in c['spans']]}
              for c in plan['chapters']]
    return {'version':plan['extractor'],'chapters':chapters,
            'characters':sum(len(t) for c in chapters for t in c['chunks'])}
