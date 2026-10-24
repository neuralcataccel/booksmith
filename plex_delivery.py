import json
import logging
import time
from pathlib import Path
from urllib.parse import quote, urlencode

import store
from settings import CONFIG, RUNTIME


def connect():
    import winreg
    from plexapi.server import PlexServer
    # Read the existing owner's token in memory; never persist or return it.
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Plex, Inc.\Plex Media Server') as key:
        token = winreg.QueryValueEx(key, 'PlexOnlineToken')[0]
    return PlexServer(CONFIG['plex_url'], token, timeout=12)


def normalized(path):
    return str(path).replace('/', '\\').rstrip('\\').casefold()


def ensure_location():
    Path(CONFIG['output_root']).mkdir(parents=True, exist_ok=True)
    Path(CONFIG['staging_root']).mkdir(parents=True, exist_ok=True)
    server = connect()
    section = server.library.sectionByID(CONFIG['plex_section'])
    target = CONFIG['plex_output_root'].replace('/', '\\')
    if normalized(target) not in [normalized(x) for x in section.locations]:
        backup = RUNTIME / 'data' / f'plex-library-before-{int(time.time())}.json'
        backup.write_text(json.dumps({'section': section.key, 'title': section.title, 'agent': section.agent, 'locations': section.locations}, indent=2), encoding='utf-8')
        section.edit(name=section.title, type=section.type, scanner=section.scanner, language=section.language, location=[*section.locations, target])
        actual = next(x for x in server.query('/library/sections') if x.attrib.get('key') == str(CONFIG['plex_section']))
        if normalized(target) not in [normalized(x.attrib['path']) for x in actual.findall('Location')]:
            raise RuntimeError('Plex did not retain the generated-audiobook folder.')
    store.set_meta('plex_connected', '1')
    store.set_meta('plex_library_name', section.title)
    return {'library': section.title, 'locations': section.locations, 'server': server.friendlyName}


def plex_path(relative):
    return str(Path(CONFIG['plex_output_root']) / relative).replace('/', '\\')


def indexed_tracks(folder, server=None, page_size=250):
    """Find exact media-part paths through Plex's paginated, section-scoped API."""
    server = server or connect()
    prefix = normalized(folder) + '\\'
    rows, start = [], 0
    while True:
        params = urlencode({'type': 10, 'includeMedia': 1,
                            'X-Plex-Container-Start': start,
                            'X-Plex-Container-Size': page_size})
        container = server.query(f"/library/sections/{CONFIG['plex_section']}/all?{params}")
        if int(container.get('offset', start)) != start:
            raise RuntimeError('Plex returned an unexpected page offset.')
        tracks = list(container)
        for track in tracks:
            if track.get('type') != 'track':
                continue
            for part in track.findall('./Media/Part'):
                path = part.get('file', '')
                if normalized(path).startswith(prefix):
                    if not track.get('ratingKey') or not track.get('parentRatingKey'):
                        raise RuntimeError('Plex returned an incomplete track identity.')
                    rows.append((track.get('ratingKey'), track.get('parentRatingKey'), path))
        start += len(tracks)
        total = container.get('totalSize')
        if total is not None:
            if start >= int(total):
                break
            if not tracks:
                raise RuntimeError('Plex returned an incomplete track listing.')
        elif len(tracks) < page_size:
            break
        if start > 1_000_000:
            raise RuntimeError('Plex track listing exceeded the verification limit.')
    return rows


def scan(relative):
    server = connect()
    server.library.sectionByID(CONFIG['plex_section']).update(path=plex_path(relative))


def confirm_import(job):
    output = Path(job['output_path'])
    manifest = json.loads((output / 'booksmith.json').read_text(encoding='utf-8'))
    relative = Path(manifest['relative_folder'])
    folder = plex_path(relative)
    server = connect()
    rows = indexed_tracks(folder, server=server)
    wanted = {normalized(str(Path(folder) / x['file'])) for x in manifest['tracks']}
    if not wanted:
        raise ValueError('The completed audiobook has no tracks.')
    found = {normalized(row[2]) for row in rows}
    if not wanted.issubset(found):
        return False
    album_ids = {r[1] for r in rows if normalized(r[2]) in wanted}
    if len(album_ids) != 1:
        raise RuntimeError('Plex imported these chapters into multiple albums; check the generated audio tags.')
    album_id = next(iter(album_ids))
    album = server.fetchItem(int(album_id))
    # Only edit the newly generated album. Leave existing narration and library preferences alone.
    album.editTitle(manifest['album_title'])
    album.editSummary(manifest['summary'])
    cover = output / 'cover.jpg'
    if cover.is_file():
        album.uploadPoster(filepath=str(cover))
    url = f"{CONFIG['plex_web_url']}#!/server/{server.machineIdentifier}/details?key={quote('/library/metadata/' + str(album_id), safe='')}"
    store.update_job(job['id'], status='ready', message='Ready in Plex', error=None, plex_key=str(album_id), plex_url=url)
    return True


def status():
    return {'configured': store.meta('plex_connected') == '1', 'library': store.meta('plex_library_name', 'Booksmith')}
