"""Idempotently configure only the dedicated Booksmith library."""
import json
from pathlib import Path

import plex_delivery
import store
from settings import CONFIG, ROOT


def configure():
    store.init()
    target = CONFIG['plex_output_root'].replace('/', '\\')
    Path(CONFIG['output_root']).mkdir(parents=True, exist_ok=True)
    Path(CONFIG['staging_root']).mkdir(parents=True, exist_ok=True)
    server = plex_delivery.connect()
    matches = [x for x in server.query('/library/sections') if x.attrib.get('title') == 'Booksmith']
    if not matches:
        server.library.add(name='Booksmith', type='artist', agent='tv.plex.agents.music', scanner='Plex Music', location=target, language='en-US')
        matches = [x for x in server.query('/library/sections') if x.attrib.get('title') == 'Booksmith']
    if len(matches) != 1:
        raise RuntimeError('Could not identify a unique Booksmith library in Plex.')
    match = matches[0]
    paths = [x.attrib['path'] for x in match.findall('Location')]
    if {plex_delivery.normalized(x) for x in paths} != {plex_delivery.normalized(target)}:
        raise RuntimeError('The existing Booksmith library points elsewhere. No changes were made to it.')
    section_id = int(match.attrib['key'])
    section = server.library.sectionByID(section_id)
    preferences = {'respectTags': 1, 'enableTrackOffsets': 1, 'albumPosters': 3, 'genres': 2, 'artistBios': 0, 'albumReviews': 0, 'popularTracks': 0, 'useExternalLyrics': 0, 'concerts': 0, 'augmentWithSharedContent': 0}
    section.edit(name=section.title, type=section.type, scanner=section.scanner, language=section.language, **{'prefs[' + k + ']': v for k, v in preferences.items()})
    actual = {x.id: x.value for x in section.settings()}
    if not actual.get('respectTags') or not actual.get('enableTrackOffsets') or actual.get('albumPosters') != 3:
        raise RuntimeError('Plex did not retain the audiobook library preferences.')
    CONFIG['plex_section'] = section_id
    (ROOT / 'config.json').write_text(json.dumps(CONFIG, indent=2) + '\n', encoding='utf-8')
    plex_delivery.ensure_location()
    section.update()
    print(f'Plex Booksmith library ready: section {section_id}; local metadata, covers and resume enabled.')


if __name__ == '__main__':
    configure()
