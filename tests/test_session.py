from http.cookiejar import MozillaCookieJar

import pytest

from scripts.weverse_cookies import CookieImportError, import_browser_session
from scripts.weverse_session import cookie_file, parse_cookie_header, redact_cookies, weverse_cookie_header


def test_header_accepts_console_quotes_and_cookie_prefix():
    assert parse_cookie_header('"Cookie: we2_access_token=token==; deviceId=device"') == {
        'we2_access_token': 'token==', 'deviceId': 'device',
    }


def test_import_filters_domains_paths_and_expired_cookies():
    cookies = [
        dict(domain='.weverse.io', name='we2_access_token', value='session', expiry=200),
        dict(domain='weverse.io', name='deviceId', value='device', expiry=None),
        dict(domain='.weverse.io', name='expired', value='gone', expiry=50),
        dict(domain='.weverse.io', name='expired-zero', value='gone', expiry=0),
        dict(domain='weverse.io.evil.example', name='evil', value='no'),
        dict(domain='notweverse.io', name='evil2', value='no'),
        dict(domain='account.weverse.io', name='account-only', value='no'),
        dict(domain='weverse.io', path='/private', name='private', value='no'),
        dict(domain='weverse.io', name='invalid', value='header\ninjection'),
        dict(domain='other.example', name='unrelated', value='no'),
    ]
    assert weverse_cookie_header(cookies, now=100) == 'we2_access_token=session; deviceId=device'


@pytest.mark.parametrize('netscape', [True, False])
def test_cookie_file_is_deleted_on_failure_and_scoped_to_weverse(netscape):
    path = None
    with pytest.raises(RuntimeError):
        with cookie_file('we2_access_token=fake-value', netscape=netscape) as path:
            assert path.exists()
            if netscape:
                jar = MozillaCookieJar(str(path))
                jar.load(ignore_discard=True)
                cookie = list(jar)[0]
                assert cookie.domain == '.weverse.io'
                assert cookie.secure
                assert cookie.value == 'fake-value'
            else:
                assert path.read_text(encoding='utf-8').strip() == 'we2_access_token=fake-value'
            raise RuntimeError('synthetic failure')
    assert not path.exists()


def test_redaction_hides_values_in_exception_messages():
    assert redact_cookies('Failed: token-secret and device-secret', 'we2_access_token=token-secret; deviceId=device-secret') == 'Failed: [redacted] and [redacted]'


def test_redaction_keeps_progress_and_ordinary_words_readable():
    assert redact_cookies('Rendering 10% lang=en', 'consent=1; lang=en') == 'Rendering 10% lang=[redacted]'


def test_browser_import_reports_failure_without_library_credentials(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError('private-cookie-value')
    monkeypatch.setattr('yt_dlp.cookies.extract_cookies_from_browser', fail)
    with pytest.raises(CookieImportError) as error:
        import_browser_session('chrome')
    assert 'private-cookie-value' not in str(error.value)
    assert 'Sign in with Chrome' in str(error.value)


def test_browser_import_requires_login_not_just_analytics(monkeypatch):
    monkeypatch.setattr('yt_dlp.cookies.extract_cookies_from_browser', lambda *a, **k: [])
    with pytest.raises(CookieImportError, match='No readable Weverse login'):
        import_browser_session('firefox')


def test_browser_import_passes_profile_and_returns_only_session(monkeypatch):
    calls = []
    def extract(browser, **kwargs):
        calls.append((browser, kwargs['profile']))
        with cookie_file('we2_access_token=fake-token', netscape=True) as path:
            jar = MozillaCookieJar(str(path))
            jar.load(ignore_discard=True)
            return list(jar)
    monkeypatch.setattr('yt_dlp.cookies.extract_cookies_from_browser', extract)
    assert import_browser_session('edge', 'Profile 1') == 'we2_access_token=fake-token'
    assert calls == [('edge', 'Profile 1')]


def test_signin_captures_session_and_closes_owned_browser(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from scripts.weverse_cookies import sign_in_session
    class Browser:
        window_handles = ['login', 'weverse']
        current_url = 'https://account.weverse.io/'
        closed = False
        def __init__(self):
            self.switch_to = SimpleNamespace(window=self.switch)
        def switch(self, handle):
            self.current_url = 'https://weverse.io/' if handle == 'weverse' else 'https://account.weverse.io/'
        def get(self, url):
            pass
        def set_page_load_timeout(self, seconds):
            pass
        def get_cookies(self):
            return [{'domain': '.weverse.io', 'name': 'we2_access_token', 'value': 'synthetic-session'}]
        def quit(self):
            self.closed = True
    browser = Browser()
    monkeypatch.setattr('selenium.webdriver.Chrome', lambda **kwargs: browser)
    assert sign_in_session(tmp_path) == 'we2_access_token=synthetic-session'
    assert browser.closed


def test_signin_timeout_still_closes_browser(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from scripts.weverse_cookies import sign_in_session
    closed = []
    browser = SimpleNamespace(
        get=lambda url: None, set_page_load_timeout=lambda seconds: None,
        quit=lambda: closed.append(True),
    )
    monkeypatch.setattr('selenium.webdriver.Chrome', lambda **kwargs: browser)
    ticks = iter([0, 301])
    monkeypatch.setattr('scripts.weverse_cookies.time.monotonic', lambda: next(ticks))
    with pytest.raises(CookieImportError, match='timed out'):
        sign_in_session(tmp_path)
    assert closed
