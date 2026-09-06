import subprocess
import threading
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLineEdit

import weverse_chat_ui as ui


LINKS = 'https://weverse.io/stayc/live/1\nhttps://weverse.io/stayc/live/2\nhttps://weverse.io/stayc/live/3'


def wait_until(app, predicate, timeout=5000):
    for _ in range(timeout // 10):
        app.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    raise AssertionError('UI did not reach expected state')


def test_native_controls_add_deduplicate_remove_and_switch_themes(window, app):
    assert window.theme == 'midnight'
    window.url_entry.setPlainText(LINKS)
    QTest.mouseClick(window.add_links_button, Qt.LeftButton)
    assert len(window.items) == 3
    assert window.url_entry.toPlainText() == ''
    window.url_entry.setPlainText(LINKS)
    QTest.mouseClick(window.add_links_button, Qt.LeftButton)
    assert len(window.items) == 3
    QTest.mouseClick(window.theme_button, Qt.LeftButton)
    assert window.theme == 'paper'
    assert window.settings.value('appearance') == 'paper'
    QTest.mouseClick(window.theme_button, Qt.LeftButton)
    assert window.theme == 'midnight'
    assert window.settings.value('appearance') == 'midnight'
    assert len(window.items) == 3
    window.queue_table.selectRow(1)
    QTest.mouseClick(window.remove_button, Qt.LeftButton)
    assert [item.url for item in window.items] == LINKS.splitlines()[::2]
    assert window.cookie_text.echoMode() == QLineEdit.Password


def test_invalid_batch_keeps_editor_and_queue_unchanged(window):
    window.url_entry.setPlainText(LINKS + '\nhttps://example.com/video')
    window._add_links()
    assert not window.items
    assert 'example.com' in window.url_entry.toPlainText()
    assert window.input_feedback.property('error')


def test_start_requires_session_and_automatically_adds_draft(window):
    window.url_entry.setPlainText(LINKS)
    window._start_workflow()
    assert len(window.items) == 3
    assert not window.is_running
    assert 'Connect a browser session' in window.input_feedback.text()


def test_queue_continues_after_failure_and_keeps_each_folder(window, app, monkeypatch, tmp_path):
    calls = []
    def workflow(cookie, url, logger, status, progress, artifact, **kwargs):
        calls.append(url)
        folder = tmp_path / str(len(calls))
        folder.mkdir()
        kwargs['set_output_dir'](folder)
        artifact('folder', True)
        progress(20)
        if len(calls) == 1:
            raise RuntimeError('failed with token-secret')
        artifact('video', True)
        status('Complete')
        return {'output_dir': folder, 'warnings': ['Burn-in unavailable'] if len(calls) == 2 else []}
    monkeypatch.setattr(ui, 'run_workflow', workflow)
    window.cookie_text.setText('we2_access_token=token-secret')
    window.url_entry.setPlainText(LINKS)
    QTest.mouseClick(window.start_button, Qt.LeftButton)
    wait_until(app, lambda: not window.is_running)
    assert calls == LINKS.splitlines()
    assert [item.status for item in window.items] == ['Failed', 'Warning', 'Complete']
    assert all(item.output_dir.exists() for item in window.items)
    assert window.batch_progress.value() == 100
    assert 'token-secret' not in window.log_text.toPlainText()
    assert 'token-secret' not in window.items[0].error
    window.queue_table.selectRow(0)
    assert window.open_folder_button.isEnabled()
    assert window.output_dir == tmp_path / '1'
    window._retry_unfinished()
    assert [item.status for item in window.items] == ['Queued', 'Queued', 'Complete']


def test_stop_does_not_start_remaining_links_and_preserves_partial_folder(window, app, monkeypatch, tmp_path):
    started = threading.Event()
    calls = []
    def workflow(cookie, url, logger, status, progress, artifact, **kwargs):
        calls.append(url)
        kwargs['set_output_dir'](tmp_path)
        started.set()
        assert window.stop_requested.wait(3)
        raise ui.WorkflowCancelled('stopped')
    monkeypatch.setattr(ui, 'run_workflow', workflow)
    window.cookie_text.setText('we2_access_token=fake')
    window.url_entry.setPlainText(LINKS)
    window._start_workflow()
    wait_until(app, started.is_set)
    QTest.mouseClick(window.stop_button, Qt.LeftButton)
    wait_until(app, lambda: not window.is_running)
    assert len(calls) == 1
    assert [item.status for item in window.items] == ['Stopped', 'Queued', 'Queued']
    assert window.open_folder_button.isEnabled()


def test_closing_waits_for_worker_cleanup(window, app, monkeypatch):
    started = threading.Event()
    cleaned = threading.Event()
    def workflow(*args, **kwargs):
        started.set()
        assert window.stop_requested.wait(3)
        cleaned.set()
        raise ui.WorkflowCancelled('stopped')
    monkeypatch.setattr(ui, 'run_workflow', workflow)
    window.cookie_text.setText('we2_access_token=fake')
    window.url_entry.setPlainText(LINKS)
    window._start_workflow()
    wait_until(app, started.is_set)
    window.close()
    wait_until(app, lambda: not window.isVisible())
    assert cleaned.is_set()
    assert not window.worker.is_alive()


def test_cookie_import_cleans_temporary_output_and_never_logs_value(window, app, monkeypatch):
    paths = []
    def capture(cmd, **kwargs):
        path = Path(cmd[cmd.index('--output') + 1])
        path.write_text('we2_access_token=synthetic-secret', encoding='utf-8')
        paths.append(path)
        return subprocess.CompletedProcess(cmd, 0, '', '')
    monkeypatch.setattr(ui, 'capture_command', capture)
    QTest.mouseClick(window.import_cookie_button, Qt.LeftButton)
    wait_until(app, lambda: not window.is_running)
    assert window.cookie_text.text() == 'we2_access_token=synthetic-secret'
    assert all(not path.parent.exists() for path in paths)
    assert 'synthetic-secret' not in window.log_text.toPlainText()
    window._disconnect()
    assert not window.cookie_text.text()


def test_cookie_import_failure_keeps_previous_session(window, app, monkeypatch):
    window.cookie_text.setText('we2_access_token=previous-session')
    monkeypatch.setattr(ui, 'capture_command', lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, 'Try Sign in with Chrome.', ''))
    window._start_cookie_import('import')
    wait_until(app, lambda: not window.is_running)
    assert window.cookie_text.text() == 'we2_access_token=previous-session'
    assert 'previous session is still available' in window.session_status.text()


def test_zen_is_default_and_browser_choice_is_remembered(window):
    assert window.browser_combo.currentData() == 'zen'
    window.browser_combo.setCurrentIndex(window.browser_combo.findData('firefox'))
    assert window.settings.value('browser') == 'firefox'
    reopened = ui.WeverseChatStudio()
    try:
        assert reopened.browser_combo.currentData() == 'firefox'
    finally:
        reopened.close()
