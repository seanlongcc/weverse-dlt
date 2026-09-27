import json
from http.cookiejar import CookieJar
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import weverse_chat_dump as chat_dump
import weverse_chat_ui as ui


def message(index):
    return {'messageTime': index * 60000, 'userId': f'user-{index}',
            'profile': {'profileName': f'Viewer {index}'}, 'content': f'CHAT_MESSAGE_{index:03d} 한글 💚'}


def test_collects_all_pages_deduplicates_overlap_and_exports_late_ass_comments(tmp_path):
    requests = []
    pages = [
        {'data': [message(i) for i in range(65, 49, -1)], 'paging': {'nextParams': {'after': 'older'}}},
        {'data': [message(i) for i in range(50, -1, -1)], 'paging': {'nextParams': {'after': 'end'}}},
        {'data': [], 'paging': {}},
    ]
    def fetch(params):
        requests.append(params)
        return pages.pop(0)
    result = chat_dump.collect_chat_pages(fetch, logger=lambda _: None)
    assert result == [message(i) for i in range(66)]
    assert [params.get('after') for params in requests] == [None, 'older', 'end']
    path = tmp_path / 'chat.json'
    path.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    ass = tmp_path / 'chat.ass'
    ui.build_ass(path, ass, 720, 1280, lambda _: None)
    text = ass.read_text(encoding='utf-8-sig')
    assert all(f'CHAT_MESSAGE_{i:03d}' in text for i in range(66))
    assert '1:05:00.00' in text


def test_empty_page_with_cursor_is_not_end_of_history():
    pages = iter([
        {'data': [], 'paging': {'nextParams': {'after': 'more'}}},
        {'data': [message(0)], 'paging': {'nextParams': None}},
    ])
    assert chat_dump.collect_chat_pages(lambda _: next(pages), logger=lambda _: None) == [message(0)]


def test_repeated_cursor_fails_instead_of_saving_partial_history():
    page = {'data': [message(0)], 'paging': {'nextParams': {'after': 'stuck'}}}
    with pytest.raises(chat_dump.ChatCollectionError, match='repeated a cursor'):
        chat_dump.collect_chat_pages(lambda _: page, logger=lambda _: None)


@pytest.mark.parametrize('payload', [
    None, {'data': []}, {'data': {}, 'paging': {}},
    {'data': [], 'paging': {'nextParams': 'bad'}},
    {'data': [{'content': 'no timestamp'}], 'paging': {}},
])
def test_malformed_page_is_not_silently_treated_as_complete(payload):
    with pytest.raises(chat_dump.ChatCollectionError, match='incomplete'):
        chat_dump.collect_chat_pages(lambda _: payload, logger=lambda _: None)


def test_fetch_failure_does_not_log_request_credentials():
    def fail(_):
        raise RuntimeError('Authorization: Bearer synthetic-secret')
    with pytest.raises(chat_dump.ChatCollectionError, match='Chat page 1') as caught:
        chat_dump.collect_chat_pages(fail)
    assert 'synthetic-secret' not in str(caught.value)


def test_channel_is_resolved_from_replay_and_cursor_is_url_encoded():
    calls = []
    pages = iter([
        {'data': [message(1)], 'paging': {'nextParams': {'after': '123,user+id'}}},
        {'data': [message(0)], 'paging': {}},
    ])
    def call_api(endpoint, post_id, **kwargs):
        calls.append((endpoint, post_id))
        return next(pages)
    api = SimpleNamespace(
        _call_post_api=lambda _: {'extension': {'mediaInfo': {'chat': {'chatId': 'test-channel'}}}},
        _call_api=call_api,
    )
    assert chat_dump.fetch_replay_chat(api, '1-123', logger=lambda _: None) == [message(0), message(1)]
    assert calls[0] == ('/chat/v1.0/chat-test-channel/messages?limit=50', '1-123')
    assert 'after=123%2Cuser%2Bid' in calls[1][0]


def test_failed_dump_preserves_existing_output_and_uses_only_memory_cookies(monkeypatch, tmp_path):
    sessions = []
    class Downloader:
        def __init__(self, options):
            assert 'cookiefile' not in options
            self.cookiejar = CookieJar()
            sessions.append(self)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    class API:
        def __init__(self, ydl):
            self.ydl = ydl
        def initialize(self):
            assert [(c.name, c.value, c.domain) for c in self.ydl.cookiejar] == [
                ('we2_access_token', 'synthetic-secret', '.weverse.io')]
    monkeypatch.setattr('yt_dlp.YoutubeDL', Downloader)
    monkeypatch.setattr('yt_dlp.extractor.weverse.WeverseIE', API)
    def fail(*args):
        raise chat_dump.ChatCollectionError('incomplete')
    monkeypatch.setattr(chat_dump, 'fetch_replay_chat', fail)
    cookies = tmp_path / 'cookies.txt'
    cookies.write_text('we2_access_token=synthetic-secret', encoding='utf-8')
    output = tmp_path / 'chat.json'
    output.write_text('["previous complete output"]', encoding='utf-8')
    with pytest.raises(chat_dump.ChatCollectionError):
        chat_dump.dump_chat(str(cookies), 'https://weverse.io/stayc/live/1-123', str(output))
    assert output.read_text(encoding='utf-8') == '["previous complete output"]'
    assert sessions
    assert set(tmp_path.iterdir()) == {cookies, output}
