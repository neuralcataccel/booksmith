from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit
from xml.etree.ElementTree import fromstring

import pytest

import plex_delivery


def test_api_paginates_and_matches_exact_folder_not_titles_or_neighbors():
    server = Mock()
    server.query.side_effect = [
        fromstring('''<MediaContainer totalSize="3" offset="0">
          <Track type="track" ratingKey="1" parentRatingKey="7"><Media><Part file="L:/Books/Novel/001.mp3"/></Media></Track>
          <Track type="track" ratingKey="2" parentRatingKey="8"><Media><Part file="L:/Books/Novel-extra/001.mp3"/></Media></Track>
        </MediaContainer>'''),
        fromstring('''<MediaContainer totalSize="3" offset="2">
          <Track type="track" ratingKey="3" parentRatingKey="7"><Media><Part file="l:/books/NOVEL/002.mp3"/></Media></Track>
        </MediaContainer>''')]
    rows = plex_delivery.indexed_tracks('L:/Books/Novel', server, page_size=2)
    assert [r[0] for r in rows] == ['1', '3']
    queries = [parse_qs(urlsplit(c.args[0]).query) for c in server.query.call_args_list]
    assert [q['X-Plex-Container-Start'] for q in queries] == [['0'], ['2']]
    assert all(q['includeMedia'] == ['1'] and q['type'] == ['10'] for q in queries)


def test_api_failure_and_partial_listing_do_not_look_like_success():
    server = Mock()
    server.query.side_effect = ConnectionError('offline')
    with pytest.raises(ConnectionError):
        plex_delivery.indexed_tracks('L:/Books/Novel', server)
    server.query.side_effect = None
    server.query.return_value = fromstring('<MediaContainer totalSize="3" offset="0"/>')
    with pytest.raises(RuntimeError, match='incomplete'):
        plex_delivery.indexed_tracks('L:/Books/Novel', server)


def test_api_rejects_repeated_page_instead_of_looping_forever():
    server = Mock()
    server.query.return_value = fromstring('''<MediaContainer totalSize="3" offset="0">
      <Track type="track" ratingKey="1" parentRatingKey="7"><Media><Part file="L:/Books/Novel/001.mp3"/></Media></Track>
    </MediaContainer>''')
    with pytest.raises(RuntimeError, match='offset'):
        plex_delivery.indexed_tracks('L:/Books/Novel', server, page_size=1)
