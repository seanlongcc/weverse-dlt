#!/usr/bin/env python3
"""Render color chat snapshots for ffmpeg without losing native emoji shaping.

Each PNG lasts until the next chat change. ffmpeg composites this sparse,
transparent timeline over the video; no full-video Python frame loop is needed.
"""

from __future__ import annotations

import argparse
import math
import os
from pathlib import Path

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import (
    QColor, QFont, QFontDatabase, QFontMetricsF, QGuiApplication, QImage,
    QPainter, QTextCharFormat, QTextLayout, QTextOption,
)

try:
    from .weverse_chat_to_ass_twitch import (
        build_twitch_segments, configure_stdio, load_chat_messages, name_color,
    )
except ImportError:
    from weverse_chat_to_ass_twitch import (
        build_twitch_segments, configure_stdio, load_chat_messages, name_color,
    )


def configure_fonts(font_dir: Path | None = None) -> None:
    # Qt's Windows offscreen plugin starts with an empty font database (also
    # used by the tests). Load the platform fonts explicitly in that case.
    if os.name == "nt" and not QFontDatabase.families():
        system_fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        for filename in ("segoeui.ttf", "malgun.ttf", "seguiemj.ttf"):
            QFontDatabase.addApplicationFont(str(system_fonts / filename))
    if font_dir:
        for path in font_dir.iterdir():
            if path.name.lower().startswith("nanumgothic") and path.suffix.lower() in (".ttf", ".otf"):
                QFontDatabase.addApplicationFont(str(path))
    families = QFontDatabase.families()
    for family in ("Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji"):
        if family in families:
            QFontDatabase.setApplicationEmojiFontFamilies([family])
            print(f"Color emoji font: {family}", flush=True)
            return
    print("Using system emoji fallback; install Noto Color Emoji if emoji are missing.", flush=True)


class ChatRenderer:
    def __init__(
        self, width: int, height: int, *, font_name: str = "Nanum Gothic",
        font_size: int = 44, max_lines: int = 6, margin_l: int = 10,
        margin_r: int = 10, margin_v: int = 10, outline: int = 2, line_gap: int = 2,
    ) -> None:
        if min(width, height, font_size, max_lines) <= 0 or min(margin_l, margin_r, margin_v, outline, line_gap) < 0:
            raise ValueError("Dimensions, font size and line count must be positive; margins cannot be negative.")
        families = QFontDatabase.families()
        if font_name == "Nanum Gothic" and "NanumGothic" in families:
            font_name = "NanumGothic"
        if font_name not in families:
            font_name = next((name for name in ("Noto Sans CJK KR", "Malgun Gothic", "Segoe UI")
                              if name in families), font_name)
        self.font = QFont(font_name)
        self.font.setPixelSize(font_size)
        self.font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
        self.outline = outline
        self.padding = outline + 1
        self.width = width
        self.margin_l = margin_l
        self.margin_v = margin_v
        self.text_width = width - margin_l - margin_r - 2 * self.padding
        # Emoji fonts often reserve excessive ascent/descent. Center their
        # shaped lines in the text line box to retain the compact ASS layout.
        self.line_height = math.ceil(QFontMetricsF(self.font).height()) + line_gap + 2 * outline
        available_lines = (height - margin_v - 2 * self.padding) // self.line_height
        if self.text_width < font_size or available_lines < 1:
            raise ValueError("Video is too small for the requested chat font and margins.")
        self.max_lines = min(max_lines, available_lines)
        self.height = self.max_lines * self.line_height + margin_v + 2 * self.padding

    def layout(self, name: str, message: str) -> QTextLayout:
        # QTextLayout uses UTF-16 offsets and shapes whole grapheme clusters.
        # Keep text literal: chat braces, backslashes and HTML are never markup.
        text = (f"{name}: " if name and message else name) + message
        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\u2028")
        layout = QTextLayout(text, self.font)
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        option.setAlignment(Qt.AlignmentFlag.AlignLeft)
        layout.setTextOption(option)
        body = QTextLayout.FormatRange()
        body.start = 0
        body.length = len(text.encode("utf-16-le")) // 2
        body.format = QTextCharFormat()
        body.format.setForeground(QColor("#ffffff"))
        formats = [body]
        if name:
            nickname = QTextLayout.FormatRange()
            nickname.start = 0
            nickname.length = len(name.encode("utf-16-le")) // 2
            nickname.format = QTextCharFormat()
            nickname.format.setForeground(QColor(name_color(name)))
            nickname.format.setFontWeight(QFont.Weight.DemiBold)
            formats.append(nickname)
        layout.setFormats(formats)
        layout.beginLayout()
        for row in range(self.max_lines):
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(self.text_width)
            line.setPosition(QPointF(0, row * self.line_height + (self.line_height - line.height()) / 2))
        layout.endLayout()
        return layout

    def tile(self, layout: QTextLayout) -> QImage:
        image = QImage(self.text_width + 2 * self.padding,
                       max(1, layout.lineCount()) * self.line_height + 2 * self.padding,
                       QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        layout.draw(painter, QPointF(self.padding, self.padding))
        painter.end()

        # Outline the alpha mask separately. Text outlines applied to glyphs can
        # replace an emoji's colored layers with a monochrome silhouette.
        mask = image.copy()
        painter = QPainter(mask)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(mask.rect(), QColor(0, 0, 0, 210))
        painter.end()
        result = QImage(image.size(), image.format())
        result.fill(Qt.GlobalColor.transparent)
        painter = QPainter(result)
        for dy in range(-self.outline, self.outline + 1):
            for dx in range(-self.outline, self.outline + 1):
                if dx * dx + dy * dy <= self.outline * self.outline:
                    painter.drawImage(dx, dy, mask)
        painter.drawImage(0, 0, image)
        painter.end()
        return result

    def snapshot(self, rows: list[tuple[int, QImage]]) -> QImage:
        image = QImage(self.width, self.height, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        for slot, tile in rows:
            y = self.height - self.margin_v - slot * self.line_height - tile.height()
            painter.drawImage(self.margin_l, y, tile)
        painter.end()
        return image


def render_timeline(
    messages: list[tuple[float, str, str]], renderer: ChatRenderer, output_dir: Path,
    *, hold: float = 3600.0,
) -> Path:
    """Write PNG snapshots and a concat manifest with millisecond timestamps."""
    if hold <= 0:
        raise ValueError("Message hold duration must be positive.")
    output_dir.mkdir(parents=True, exist_ok=True)
    layouts = [renderer.layout(name, msg) for _, name, msg in messages]
    scheduled = build_twitch_segments(
        [(t, name, msg, max(1, layout.lineCount()))
         for (t, name, msg), layout in zip(messages, layouts)],
        hold=hold, max_lines=renderer.max_lines,
    )
    changes: dict[float, list[tuple[int, int | None]]] = {0.0: []}
    for cm in scheduled:
        for segment in cm.segments:
            changes.setdefault(segment.start, []).append((cm.idx, segment.slot))
            changes.setdefault(segment.end, []).append((cm.idx, None))
    times = sorted(changes)
    active: dict[int, int] = {}
    tiles: dict[int, QImage] = {}
    manifest = ["ffconcat version 1.0\n"]
    for index, timestamp in enumerate(times):
        updates = changes[timestamp]
        # End old segments before starting replacements at the same instant.
        for midx, slot in updates:
            if slot is None:
                active.pop(midx, None)
        for midx, slot in updates:
            if slot is not None:
                active[midx] = slot
        tiles = {midx: tile for midx, tile in tiles.items() if midx in active}
        for midx in active:
            if midx not in tiles:
                tiles[midx] = renderer.tile(layouts[midx])
        image = renderer.snapshot([(slot, tiles[midx]) for midx, slot in active.items()])
        filename = f"chat-{index:06d}.png"
        if not image.save(str(output_dir / filename)):
            raise OSError(f"Could not save chat snapshot: {filename}")
        manifest.append(f"file '{filename}'\noption framerate 1000\n")
        if index + 1 < len(times):
            manifest.append(f"duration {times[index + 1] - timestamp:.6f}\n")
        if index % 100 == 0:
            print(f"Rendered chat snapshot {index + 1}/{len(times)}", flush=True)
    path = output_dir / "chat.ffconcat"
    path.write_text("".join(manifest), encoding="utf-8")
    return path


def main() -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--resx", type=int, default=1080)
    parser.add_argument("--resy", type=int, default=1920)
    parser.add_argument("--font-dir", type=Path)
    parser.add_argument("--font-name", default="Nanum Gothic")
    parser.add_argument("--font-size", type=int, default=44)
    parser.add_argument("--max-lines", type=int, default=6)
    parser.add_argument("--hold", type=float, default=3600.0)
    parser.add_argument("--offset-seconds", type=float, default=0.0)
    args = parser.parse_args()
    if os.name != "nt":
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QGuiApplication.instance() or QGuiApplication([])
    configure_fonts(args.font_dir)
    renderer = ChatRenderer(args.resx, args.resy, font_name=args.font_name,
                            font_size=args.font_size, max_lines=args.max_lines)
    path = render_timeline(load_chat_messages(args.chat, args.offset_seconds), renderer,
                           args.output_dir, hold=args.hold)
    print(f"Wrote color chat timeline: {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
