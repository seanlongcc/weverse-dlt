import pytest

from scripts.weverse_queue import parse_replay_links


def test_paste_normalizes_and_deduplicates_share_links_in_order():
    assert parse_replay_links(
        '\ufeffhttps://weverse.io/stayc/live/4-123?hl=en\r\n'
        'https://www.weverse.io/stayc/live/4-123/#chat\n'
        'https://weverse.io/stayc/media/3-456\t'
        'http://weverse.io/stayc/live/4-123'
    ) == ['https://weverse.io/stayc/live/4-123', 'https://weverse.io/stayc/media/3-456']


@pytest.mark.parametrize('url', [
    'https://weverse.io.evil.example/stayc/live/1',
    'https://evil.example/?next=https://weverse.io/stayc/live/1',
    'https://weverse.io@evil.example/stayc/live/1',
    'https://user:pass@weverse.io/stayc/live/1',
    'https://weverse.io:123/stayc/live/1',
    'https://weverse.io:bad/stayc/live/1',
    'https://weverse.io/stayc/live',
    'https://weverse.io/stayc/live/',
    '--cookies=secret', 'file:///tmp/example', 'https://[bad',
])
def test_invalid_or_unrelated_links_rejected(url):
    with pytest.raises(ValueError):
        parse_replay_links(url)


def test_invalid_batch_is_rejected_atomically():
    with pytest.raises(ValueError):
        parse_replay_links('https://weverse.io/stayc/live/1\nnot-a-url')
    assert parse_replay_links(' \n\t') == []
