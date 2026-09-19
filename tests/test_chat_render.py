import json
from pathlib import Path

import pytest
from PySide6.QtCore import QTextBoundaryFinder
from PySide6.QtGui import QFontDatabase, QImage

from scripts.weverse_chat_render import ChatRenderer, configure_fonts, render_timeline
from scripts.weverse_chat_to_ass_twitch import load_chat_messages, name_color, render_chat_text
import weverse_chat_ui as ui


@pytest.fixture
def renderer(app):
    configure_fonts(ui.detect_nanum_font_dir())
    return ChatRenderer(720, 1280)


def saturated_pixels(image):
    return sum(1 for y in range(image.height()) for x in range(image.width())
               if (color := image.pixelColor(x, y)).alpha() > 200
               and color.saturation() > 100 and color.value() > 100)


def test_timestamp_zero_sorting_and_fallback_are_shared(tmp_path):
    chat = tmp_path / 'chat.json'
    chat.write_text(json.dumps([
        {'messageTime': 2000, 'name': 'later', 'content': 'second'},
        {'messageTime': 0, 'createTime': 9000, 'name': 'first', 'content': 'first'},
        {'name': 'missing', 'content': 'no timestamp'},
    ]), encoding='utf-8')
    assert load_chat_messages(str(chat)) == [(0, 'first', 'first'), (2, 'later', 'second')]
    assert load_chat_messages(str(chat), -1)[1][0] == 1
    chat.write_text('[{"name":"a"},{"name":"b"}]', encoding='utf-8')
    assert [t for t, _, _ in load_chat_messages(str(chat), 0.25)] == [0.25, 1.25]


def test_names_use_stable_colors_and_ass_still_escapes_literal_text():
    name = '팬 💚'
    rgb = name_color(name)[1:]
    bgr = rgb[4:6] + rgb[2:4] + rgb[:2]
    assert f'\\1c&H00{bgr}&' in render_chat_text(name, '{hi}\\N')
    assert r'\{hi\}\\N' in render_chat_text(name, '{hi}\\N')
    assert len({name_color(f'viewer-{i}') for i in range(30)}) >= 5


def test_wrapping_keeps_emoji_clusters_and_utf16_nickname_range(renderer):
    name = '팬 👩🏽‍💻'
    message = ('👩🏽‍💻 ❤️ 🥺 ' * 30) + '\n한글 <b>{literal}\\N'
    layout = renderer.layout(name, message)
    assert layout.formats()[1].length == len(name.encode('utf-16-le')) // 2
    assert '<b>{literal}\\N' in layout.text()
    assert layout.lineCount() <= renderer.max_lines
    finder = QTextBoundaryFinder(QTextBoundaryFinder.BoundaryType.Grapheme, layout.text())
    for i in range(layout.lineCount()):
        line = layout.lineAt(i)
        finder.setPosition(line.textStart() + line.textLength())
        assert finder.isAtBoundary(), 'Wrapping split an emoji sequence'
        assert line.naturalTextWidth() <= renderer.text_width + 1


def test_native_emoji_retain_color_with_outline(renderer):
    if not QFontDatabase.applicationEmojiFontFamilies():
        pytest.skip('No supported color emoji font is installed')
    # No colored nickname: saturation must come from the emoji itself.
    tile = renderer.tile(renderer.layout('', '🥺 ❤️ 👩🏽‍💻'))
    assert saturated_pixels(tile) > 200
    assert tile.pixelColor(0, 0).alpha() == 0


def test_timeline_starts_transparent_and_clears_expired_chat(renderer, tmp_path):
    manifest = render_timeline([(0.123, 'Viewer', 'Hello')], renderer, tmp_path, hold=0.777)
    text = manifest.read_text(encoding='utf-8')
    assert 'duration 0.123000' in text
    assert 'duration 0.777000' in text
    blank = renderer.snapshot([]).convertToFormat(QImage.Format.Format_ARGB32)
    assert QImage(str(tmp_path / 'chat-000000.png')) == blank
    assert QImage(str(tmp_path / 'chat-000001.png')) != blank
    assert QImage(str(tmp_path / 'chat-000002.png')) == blank


def test_simultaneous_arrivals_obey_stack_capacity(renderer, tmp_path):
    renderer.max_lines = 1
    render_timeline([(0, 'A', 'first'), (0, 'B', 'last')], renderer, tmp_path, hold=1)
    expected = renderer.snapshot([(0, renderer.tile(renderer.layout('B', 'last')))]).convertToFormat(
        QImage.Format.Format_ARGB32)
    assert QImage(str(tmp_path / 'chat-000000.png')) == expected


@pytest.mark.parametrize('failure_stage', ['prepare', 'encode'])
def test_color_burn_cancellation_removes_frames(monkeypatch, tmp_path, failure_stage):
    monkeypatch.setattr(ui.shutil, 'which', lambda _: 'ffmpeg')
    monkeypatch.setattr(ui, 'detect_nanum_font_dir', lambda: None)
    temporary_dirs = []
    def run(command, *args, **kwargs):
        if '--output-dir' in command:
            directory = Path(command[command.index('--output-dir') + 1])
            temporary_dirs.append(directory)
            (directory / 'partial.png').touch()
            if failure_stage == 'prepare':
                raise ui.WorkflowCancelled('stopped')
        else:
            raise ui.WorkflowCancelled('stopped')
    monkeypatch.setattr(ui, 'run_command', run)
    with pytest.raises(ui.WorkflowCancelled):
        ui.burn_subtitles(tmp_path / 'source.mp4', tmp_path / 'chat.ass', lambda _: None,
                          chat_json=tmp_path / 'chat.json', resolution=(720, 1280))
    assert temporary_dirs and all(not directory.exists() for directory in temporary_dirs)


def test_color_burn_failure_is_reported_and_cleans_frames(monkeypatch, tmp_path):
    monkeypatch.setattr(ui.shutil, 'which', lambda _: 'ffmpeg')
    monkeypatch.setattr(ui, 'detect_nanum_font_dir', lambda: None)
    def fail(*args, **kwargs):
        raise RuntimeError('render failed')
    monkeypatch.setattr(ui, 'run_command', fail)
    messages = []
    assert ui.burn_subtitles(tmp_path / 'source.mp4', tmp_path / 'chat.ass', messages.append,
                            chat_json=tmp_path / 'chat.json', resolution=(720, 1280)) is None
    assert 'render failed' in messages[-1]
    assert not list(tmp_path.glob('weverse-chat-*'))
