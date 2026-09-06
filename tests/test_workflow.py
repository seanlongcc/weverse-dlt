import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import weverse_chat_ui as ui


def test_metadata_uses_temporary_scoped_cookies_not_command_line_credentials(monkeypatch):
    captured = []
    def capture(cmd, **kwargs):
        assert 'fake-secret' not in ' '.join(cmd)
        path = Path(cmd[cmd.index('--cookies') + 1])
        captured.append(path)
        assert '.weverse.io' in path.read_text(encoding='utf-8')
        assert 'fake-secret' in path.read_text(encoding='utf-8')
        return subprocess.CompletedProcess(cmd, 0, '{"title":"한글 라이브"}', '')
    monkeypatch.setattr(ui, 'capture_command', capture)
    result = ui.probe_video_metadata('https://weverse.io/stayc/live/1', 'we2_access_token=fake-secret', lambda msg: None)
    assert result['title'] == '한글 라이브'
    assert all(not path.exists() for path in captured)


@pytest.mark.parametrize('cancel_chat', [False, True])
def test_workflow_preserves_title_and_outputs_and_cleans_chat_cookie(monkeypatch, tmp_path, cancel_chat):
    monkeypatch.setattr(ui, 'OUTPUT_ROOT', tmp_path)
    monkeypatch.setattr(ui, 'probe_video_metadata', lambda *a, **k: {'title': '장재이 라이브', 'width': 640, 'height': 360})
    def download(url, cookie, folder, *args, **kwargs):
        video = folder / 'replay.mp4'
        video.touch()
        return video
    monkeypatch.setattr(ui, 'download_video', download)
    monkeypatch.setattr(ui, 'resolve_video_dimensions', lambda *a, **k: (640, 360))
    cookie_paths = []
    def run(cmd, *args, **kwargs):
        cookie_path = Path(cmd[cmd.index('--cookies') + 1])
        cookie_paths.append(cookie_path)
        assert cookie_path.read_text(encoding='utf-8').strip() == 'we2_access_token=fake-secret'
        if cancel_chat:
            raise ui.WorkflowCancelled('stopped')
        Path(cmd[cmd.index('--out') + 1]).write_text('[]', encoding='utf-8')
    monkeypatch.setattr(ui, 'run_command', run)
    monkeypatch.setattr(ui, 'build_ass', lambda chat, ass, *a, **k: ass.write_text('[Script Info]', encoding='utf-8'))
    monkeypatch.setattr(ui, 'burn_subtitles', lambda *a, **k: None)
    folders, artifacts, progress = [], {}, []
    def workflow():
        return ui.run_workflow('we2_access_token=fake-secret', 'https://weverse.io/stayc/live/1',
            lambda msg: None, lambda status: None, progress.append,
            lambda key, done: artifacts.update({key: done}), set_output_dir=folders.append)
    if cancel_chat:
        with pytest.raises(ui.WorkflowCancelled):
            workflow()
    else:
        result = workflow()
        assert result['video_path'].exists()
        assert result['chat_json'].exists()
        assert result['ass_path'].exists()
        assert result['warnings']
        assert artifacts == {'folder': True, 'video': True, 'chat_json': True, 'ass': True, 'burned': False}
        assert progress[-1] == 100
    assert (folders[0] / 'title.txt').read_text(encoding='utf-8') == '장재이 라이브\n'
    assert all(not path.exists() for path in cookie_paths)
    assert not list(folders[0].glob('*cookie*'))


def test_burn_cancellation_is_not_reported_as_optional_warning(monkeypatch, tmp_path):
    monkeypatch.setattr(ui.shutil, 'which', lambda _: 'ffmpeg')
    monkeypatch.setattr(ui, 'detect_nanum_font_dir', lambda: None)
    def stop(*a, **k):
        raise ui.WorkflowCancelled('stopped')
    monkeypatch.setattr(ui, 'run_command', stop)
    with pytest.raises(ui.WorkflowCancelled):
        ui.burn_subtitles(tmp_path / 'replay.mp4', tmp_path / 'chat.ass', lambda msg: None)


def test_real_subprocess_can_be_cancelled_and_reaped():
    cancelled = threading.Event()
    processes = []
    def process_started(process):
        if process is not None:
            processes.append(process)
            cancelled.set()
    started = time.monotonic()
    with pytest.raises(ui.WorkflowCancelled):
        ui.capture_command([sys.executable, '-c', 'import time; time.sleep(30)'],
                           on_process=process_started, cancel_requested=cancelled.is_set)
    assert time.monotonic() - started < 8
    assert processes[0].poll() is not None


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='ffmpeg not installed')
def test_synthetic_unicode_chat_to_ass_and_real_burn_in(tmp_path):
    folder = tmp_path / 'replay [sample], space'
    folder.mkdir()
    video = folder / 'sample.mp4'
    chat = folder / 'chat.json'
    ass = folder / 'chat.ass'
    chat.write_text(json.dumps([
        {'messageTime': 1000, 'profile': {'profileName': '팬'}, 'content': '안녕하세요 💚'},
        {'messageTime': 1500, 'profile': {'profileName': 'Viewer'}, 'content': 'Hello {chat}'}
    ], ensure_ascii=False), encoding='utf-8')
    subprocess.run([shutil.which('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i',
                    'color=c=gray:s=640x360:r=12', '-t', '1', '-c:v', 'libx264', str(video)], check=True)
    messages = []
    ui.build_ass(chat, ass, 640, 360, messages.append)
    content = ass.read_text(encoding='utf-8-sig')
    assert '안녕하세요' in content and 'Dialogue:' in content
    assert 'PlayResX: 640' in content and 'PlayResY: 360' in content
    result = ui.burn_subtitles(video, ass, messages.append)
    assert result and result.exists(), '\n'.join(messages)
    assert result.stat().st_size > 0
