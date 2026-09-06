import os
import sqlite3
from pathlib import Path

import pytest

from scripts import weverse_cookies as cookies


def profile_folder(root, name):
    folder = root / 'Profiles' / name
    folder.mkdir(parents=True)
    (folder / 'cookies.sqlite').touch()
    return folder


def test_zen_prefers_installation_default_over_newer_profile(monkeypatch, tmp_path):
    default = profile_folder(tmp_path, 'first.default')
    other = profile_folder(tmp_path, 'second.other')
    os.utime(default / 'cookies.sqlite', (100, 100))
    os.utime(other / 'cookies.sqlite', (200, 200))
    (tmp_path / 'profiles.ini').write_text(
        '[Profile0]\nName=Personal\nIsRelative=1\nPath=Profiles/first.default\n'
        '[Profile1]\nName=Other\nIsRelative=1\nPath=Profiles/second.other\nDefault=1\n'
        '[InstallABC]\nDefault=Profiles/first.default\nLocked=1\n', encoding='utf-8')
    monkeypatch.setattr(cookies, 'zen_profile_roots', lambda: [tmp_path])
    assert cookies.find_zen_profile() == default.resolve()
    assert cookies.find_zen_profile('Other') == other.resolve()
    assert cookies.find_zen_profile('second.other') == other.resolve()


def test_zen_honors_profile_default_and_absolute_profile_paths(monkeypatch, tmp_path):
    default = profile_folder(tmp_path, 'default')
    other = profile_folder(tmp_path, 'other')
    os.utime(default / 'cookies.sqlite', (100, 100))
    os.utime(other / 'cookies.sqlite', (200, 200))
    (tmp_path / 'profiles.ini').write_text(
        f'[Profile0]\nName=Default\nIsRelative=0\nPath={default}\nDefault=1\n', encoding='utf-8')
    monkeypatch.setattr(cookies, 'zen_profile_roots', lambda: [tmp_path])
    assert cookies.find_zen_profile() == default.resolve()
    assert cookies.find_zen_profile(str(other)) == other.resolve()


def test_zen_falls_back_to_newest_database_when_registry_missing(monkeypatch, tmp_path):
    old = profile_folder(tmp_path, 'old')
    latest = profile_folder(tmp_path, 'latest')
    os.utime(old / 'cookies.sqlite', (100, 100))
    os.utime(latest / 'cookies.sqlite', (200, 200))
    monkeypatch.setattr(cookies, 'zen_profile_roots', lambda: [tmp_path])
    assert cookies.find_zen_profile() == latest.resolve()


def test_missing_zen_profile_does_not_read_firefox_cookies(monkeypatch, tmp_path):
    monkeypatch.setattr(cookies, 'zen_profile_roots', lambda: [tmp_path])
    def unexpected(*args, **kwargs):
        pytest.fail('Must not fall back to an unrelated Firefox profile')
    monkeypatch.setattr('yt_dlp.cookies.extract_cookies_from_browser', unexpected)
    with pytest.raises(cookies.CookieImportError, match='about:support'):
        cookies.import_browser_session('zen', 'missing')


@pytest.mark.parametrize('platform, relative', [
    ('win32', 'Roaming/zen'), ('darwin', 'Library/Application Support/zen'),
    ('linux', '.zen'),
])
def test_platform_profile_roots(monkeypatch, tmp_path, platform, relative):
    monkeypatch.setattr(cookies.sys, 'platform', platform)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('APPDATA', str(tmp_path / 'Roaming'))
    assert cookies.zen_profile_roots()[0] == tmp_path / relative
    if platform == 'linux':
        assert tmp_path / '.var/app/app.zen_browser.zen/.zen' in cookies.zen_profile_roots()


@pytest.mark.parametrize('schema_version', [15, 17])
def test_real_firefox_reader_imports_synthetic_zen_database(monkeypatch, tmp_path, schema_version):
    profile = profile_folder(tmp_path, 'synthetic.default')
    expiry = 4102444800 * (1000 if schema_version >= 16 else 1)
    connection = sqlite3.connect(profile / 'cookies.sqlite')
    try:
        connection.execute(f'PRAGMA user_version={schema_version}')
        connection.execute('CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT, path TEXT, expiry INTEGER, isSecure INTEGER, originAttributes TEXT)')
        connection.executemany('INSERT INTO moz_cookies VALUES (?, ?, ?, ?, ?, ?, ?)', [
            ('.weverse.io', 'we2_access_token', 'synthetic-zen-token', '/', expiry, 1, ''),
            ('.unrelated.example', 'private', 'must-not-export', '/', expiry, 1, ''),
        ])
        connection.commit()
    finally:
        connection.close()
    monkeypatch.setattr(cookies, 'zen_profile_roots', lambda: [tmp_path])
    assert cookies.import_browser_session('zen') == 'we2_access_token=synthetic-zen-token'
    assert (profile / 'cookies.sqlite').exists()
