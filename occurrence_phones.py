"""Reviewed known readings with conservative source-occurrence/phone alignment."""
import re
import string

from reading_plan import digest

LEXICON = {
    'lead': {'metal': ('metal, pronounced led', 'lˈɛd'), 'guide': ('guide, pronounced leed', 'lˈiːd')},
    'read': {'past': ('past tense or past participle: read yesterday, has read, will have read; pronounced red', 'ɹˈɛd'),
             'present': ('base/present verb or infinitive: read every day, to read, will read; pronounced reed', 'ɹˈiːd')},
    'wind': {'air': ('moving air', 'wˈɪnd'), 'turn': ('turn or coil', 'wˈaɪnd')},
    'tear': {'eye': ('liquid from an eye', 'tˈɪɹ'), 'rip': ('rip something', 'tˈɛɹ')},
    'bow': {'bend': ('bend at the waist', 'bˈaʊ'), 'weapon': ('archery weapon or tied ribbon, pronounced bo', 'bˈoʊ')},
    'bass': {'fish': ('fish, rhymes with mass', 'bˈæs'), 'music': ('low musical range or instrument, rhymes with base', 'bˈeɪs')},
    'close': {'near': ('near, with a final s sound', 'klˈoʊs'), 'shut': ('shut, with a final z sound', 'klˈoʊz')},
    'live': {'reside': ('verb: to live, reside or remain alive; pronounced liv, rhymes with give', 'lˈɪv'),
             'alive': ('adjective/adverb: a live animal, live music, go live on television; pronounced lyve, rhymes with five', 'lˈaɪv')},
    'dove': {'bird': ('bird, rhymes with love', 'dˈʌv'), 'dived': ('past tense of dive, rhymes with drove', 'dˈoʊv')},
    'wound': {'injury': ('injury, pronounced woond', 'wˈuːnd'), 'wrapped': ('past tense of wind or wrap, rhymes with found', 'wˈaʊnd')},
    'sow': {'plant': ('plant seeds, rhymes with go', 'sˈoʊ'), 'pig': ('female pig, rhymes with cow', 'sˈaʊ')},
    'does': {'verb': ('form of do, rhymes with buzz', 'dˈʌz'), 'deer': ('plural of female deer, rhymes with goes', 'dˈoʊz')},
}
LEXICON_SHA = digest({'language':'en-us','revision':2,'entries':LEXICON})
WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)
PUNCT = string.punctuation + '“”‘’—–…«» '


def candidate(word, choice):
    try:
        label, phones = LEXICON[word.casefold()][choice]
    except KeyError as exc:
        raise ValueError('Unknown pronunciation candidate.') from exc
    return {'id':choice,'label':label,'phones':phones}


def normalized(phones):
    return phones.replace('ˈ','').replace('ˌ','').strip(PUNCT)


def marked(text, tokenizer, lang):
    import phonemizer
    from phonemizer.separator import Separator
    from kokoro_onnx.tokenizer import _espeak_lock
    # Share the installed tokenizer's process-global eSpeak lock.
    with _espeak_lock:
        result=phonemizer.phonemize(text,lang,preserve_punctuation=True,with_stress=True,
                                  separator=Separator(phone='',syllable='',word='|'))
    return result, [tokenizer.known(p) for p in result.split('|') if p]


def align(text, occurrences, tokenizer, lang='en-us', marker=marked):
    """Apply only independently aligned replacements; return visible item outcomes."""
    if not occurrences:
        return None, []
    occupied=[]
    for occurrence in occurrences:
        start,end=occurrence['start'],occurrence['end']
        if (type(start) is not int or type(end) is not int or start<0 or end<=start or end>len(text)
            or text[start:end].casefold()!=occurrence['word'].casefold()):
            raise ValueError('Pronunciation source offsets changed.')
        if any(start<b and end>a for a,b in occupied):raise ValueError('Pronunciation occurrences overlap.')
        occupied.append((start,end))
        candidate(occurrence['word'],occurrence['choice'])
    outcomes=[]
    if lang!='en-us' or any(c.isdigit() for c in text):
        return None,[{'id':o['id'],'status':'unsupported_language' if lang!='en-us' else 'number_alignment'} for o in occurrences]
    lexemes=list(WORD.finditer(text))
    original=tokenizer.phonemize(text,lang=lang)
    raw,units=marker(text,tokenizer,lang)
    if tokenizer.known(raw.replace('|',' ')).strip()!=original or ' '.join(units).strip()!=original:
        return None,[{'id':o['id'],'status':'unaligned'} for o in occurrences]
    changed={}
    claimed={}
    zebra=normalized(tokenizer.phonemize('zebra',lang=lang))
    for occurrence in occurrences:
        oid=occurrence['id'];start,end=occurrence['start'],occurrence['end']
        desired=candidate(occurrence['word'],occurrence['choice'])['phones']
        if tokenizer.known(desired)!=desired:
            outcomes.append({'id':oid,'status':'unsupported_phones'});continue
        if not any((m.start(),m.end())==(start,end) for m in lexemes):
            outcomes.append({'id':oid,'status':'unaligned'});continue
        # Exactly this source occurrence must affect exactly this phone group.
        probe=text[:start]+'zebra'+text[end:]
        _,probe_units=marker(probe,tokenizer,lang)
        differences=[i for i,(a,b) in enumerate(zip(units,probe_units)) if a!=b]
        if len(probe_units)!=len(units) or len(differences)!=1:
            outcomes.append({'id':oid,'status':'unaligned'});continue
        index=differences[0]
        if normalized(probe_units[index])!=zebra:
            outcomes.append({'id':oid,'status':'unaligned'});continue
        core=units[index].strip(PUNCT)
        possible={normalized(v[1]) for v in LEXICON[occurrence['word'].casefold()].values()}
        if normalized(core) not in possible:
            outcomes.append({'id':oid,'status':'unrecognized_baseline'});continue
        if index in claimed:
            changed.pop(index,None);outcomes[claimed[index]]['status']='unaligned'
            outcomes.append({'id':oid,'status':'unaligned'});continue
        claimed[index]=len(outcomes)
        if normalized(core)==normalized(desired):
            outcomes.append({'id':oid,'status':'already_correct'});continue
        a=units[index].index(core)
        changed[index]=units[index][:a]+desired+units[index][a+len(core):]
        outcomes.append({'id':oid,'status':'applied'})
    if not changed:
        return None,outcomes
    result=' '.join(changed.get(i,unit) for i,unit in enumerate(units)).strip()
    if tokenizer.known(result)!=result:
        raise ValueError('Pronunciation produced unsupported phone input.')
    return result,outcomes


def block_key(source_sha, extractor, span):
    # Independent of selected chapter numbering; anthology selection keeps source identity.
    return digest([source_sha,extractor,span['locator'],span['text']])


def find(plan, book_id, chapter, scope=None):
    result=[]
    c=plan['chapters'][chapter]
    for block_order,span in enumerate(c['spans']):
        key=block_key(plan['source_sha256'],plan['extractor'],span)
        for match in WORD.finditer(span['text']):
            word=match.group().casefold()
            if word not in LEXICON:continue
            result.append({'id':digest([book_id,key,match.start(),match.end(),LEXICON_SHA,scope or {}]),
                           'book_id':book_id,'chapter':chapter,'chapter_title':c['title'],
                           'block_order':block_order,
                           'block_key':key,'start':match.start(),'end':match.end(),'word':word,
                           'text':span['text'],'lexicon_sha256':LEXICON_SHA})
    return result


def chunk_occurrences(plan, span, piece, approvals):
    key=block_key(plan['source_sha256'],plan['extractor'],span)
    result=[]
    for approval in approvals:
        if approval['block_key']!=key:continue
        if approval['lexicon_sha256']!=LEXICON_SHA:
            raise ValueError('The approved pronunciation lexicon changed.')
        start,end=approval['start'],approval['end']
        if span['text'][start:end].casefold()!=approval['word']:
            raise ValueError('The approved pronunciation source changed.')
        if piece['start']<=start and end<=piece['end']:
            candidate(approval['word'],approval['choice'])
            result.append({**approval,'start':start-piece['start'],'end':end-piece['start']})
    return result


def validate_plan(plan, approvals):
    matched=[]
    for chapter in plan['chapters']:
        for span in chapter['spans']:
            for piece in span['pieces']:
                matched.extend(o['id'] for o in chunk_occurrences(plan,span,piece,approvals))
    if sorted(matched)!=sorted(o['id'] for o in approvals):
        raise ValueError('Approved pronunciation coverage does not match this reading plan.')
