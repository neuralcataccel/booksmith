"""Explicit non-chaining spoken-form substitutions; the source stays unchanged."""
import re


def validate(entries):
    if not isinstance(entries,list) or len(entries)>100:
        raise ValueError('Use at most 100 pronunciation entries per dictionary.')
    result, seen = [], set()
    for entry in entries:
        if not isinstance(entry,dict) or set(entry)!={'word','spoken'}:
            raise ValueError('Each pronunciation needs a word and spoken form.')
        word, spoken = entry['word'].strip(), entry['spoken'].strip()
        if not word or len(word)>100 or not spoken or len(spoken)>200 or any(c in word+spoken for c in '\n\r\x00'):
            raise ValueError('Use a word/phrase up to 100 characters and a spoken form up to 200.')
        if word.casefold() in seen:
            raise ValueError('A dictionary has duplicate words. Keep one reading per word.')
        seen.add(word.casefold())
        result.append({'word':word,'spoken':spoken})
    return result


def merge(*dictionaries):
    values={}
    for dictionary in dictionaries:
        for entry in validate(dictionary):values[entry['word'].casefold()]=entry
    # A profile plus series plus book may exceed a single dictionary's editing cap.
    return sorted(values.values(),key=lambda e:(-len(e['word']),e['word'].casefold()))


def apply(text, entries):
    if not entries:return text
    # One regex pass means replacements are never interpreted as a second source match.
    ordered=sorted(entries,key=lambda e:(-len(e['word']),e['word'].casefold()))
    values={f'r{i}':e['spoken'] for i,e in enumerate(ordered)}
    expression=r'(?<!\w)(?:'+'|'.join(f'(?P<r{i}>'+re.escape(e['word'])+')' for i,e in enumerate(ordered))+r')(?!\w)'
    return re.sub(expression,lambda m:values[m.lastgroup],text,flags=re.IGNORECASE)


def remap(text, entries, occurrences):
    """Keep source offsets honest after substitutions; dictionaries win overlaps."""
    if not entries:
        return text, [dict(o) for o in occurrences], []
    ordered=sorted(entries,key=lambda e:(-len(e['word']),e['word'].casefold()))
    values={f'r{i}':e['spoken'] for i,e in enumerate(ordered)}
    expression=r'(?<!\w)(?:'+'|'.join(f'(?P<r{i}>'+re.escape(e['word'])+')' for i,e in enumerate(ordered))+r')(?!\w)'
    changes=[(m.start(),m.end(),values[m.lastgroup]) for m in re.finditer(expression,text,re.IGNORECASE)]
    mapped, skipped=[],[]
    for occurrence in occurrences:
        start,end=occurrence['start'],occurrence['end']
        if any(a<end and b>start for a,b,_ in changes):
            skipped.append({'id':occurrence['id'],'status':'dictionary_override'})
            continue
        delta=sum(len(spoken)-(b-a) for a,b,spoken in changes if b<=start)
        mapped.append({**occurrence,'start':start+delta,'end':end+delta})
    return apply(text,entries),mapped,skipped
