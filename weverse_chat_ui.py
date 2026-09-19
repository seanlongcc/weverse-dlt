#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from scripts.weverse_output import write_title_file
from scripts.weverse_queue import parse_replay_links
from scripts.weverse_session import cookie_file, parse_cookie_header, redact_cookies


def _format_import_error(exc: Exception) -> str:
    lines = [
        "Failed to import a UI dependency.",
        "",
        f"Python: {sys.executable}",
        f"Error: {type(exc).__name__}: {exc}",
        "",
        "Launch the UI with the venv interpreter:",
        r".\.venv\Scripts\python.exe .\weverse_chat_ui.py",
        "",
        "Install missing packages:",
        "pip install -r requirements.txt",
    ]
    return "\n".join(lines)


try:
    from PySide6.QtCore import Qt, QTimer, QUrl, QSettings, QSize
    from PySide6.QtGui import QDesktopServices, QColor, QPalette, QIcon
    from PySide6.QtWidgets import (
        QApplication,
        QAbstractItemView,
        QComboBox,
        QFileDialog,
        QFrame,
        QHeaderView,
        QProgressBar,
        QScrollArea,
        QSplitter,
        QStackedWidget,
        QTableWidget,
        QTableWidgetItem,
        QToolButton,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMainWindow,
        QPlainTextEdit,
        QPushButton,
        QVBoxLayout,
        QWidget,
    )
except Exception as exc:
    print(_format_import_error(exc), file=sys.stderr)
    raise SystemExit(1) from exc


APP_TITLE = "Weverse Live Processor"
REPO_ROOT = Path(__file__).resolve().parent
OUTPUT_ROOT = REPO_ROOT / "output"
SCRIPTS_DIR = REPO_ROOT / "scripts"
STYLES_DIR = REPO_ROOT / "styles"
APP_STYLE_PATH = STYLES_DIR / "weverse_chat_ui.qss"
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".webm", ".mov", ".m4v")
WEVERSE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)
STATUS_TO_ARTIFACT = {
    "Preparing workspace": "folder",
    "Downloading video": "video",
    "Dumping replay chat": "chat_json",
    "Rendering ASS overlay": "ass",
    "Burning subtitles": "burned",
}


class WorkflowCancelled(RuntimeError):
    pass


def normalize_cookie(cookie_text: str) -> str:
    return "; ".join(f"{name}={value}" for name, value in parse_cookie_header(cookie_text).items())


def sanitize_name(value: str, max_length: int = 72) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii")
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        return "weverse-chat"
    return value[:max_length].rstrip(" .")


def make_output_dir(title: str | None) -> Path:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = sanitize_name(title or "weverse-chat")
    candidate = OUTPUT_ROOT / f"{stamp}_{base}"
    suffix = 2
    while candidate.exists():
        candidate = OUTPUT_ROOT / f"{stamp}_{base}_{suffix}"
        suffix += 1
    candidate.mkdir(parents=True)
    return candidate


def _popen_session_kwargs() -> dict[str, object]:
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": creationflags} if creationflags else {}
    return {"start_new_session": True}


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return

    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception:
            pass
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=2)
            except Exception:
                pass
        if process.poll() is None:
            try:
                process.kill()
            except Exception:
                pass
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2)
    except Exception:
        pass
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except Exception:
            pass
    if process.poll() is None:
        try:
            process.kill()
        except Exception:
            pass


def _raise_if_cancelled(cancel_requested: Callable[[], bool] | None) -> None:
    if cancel_requested and cancel_requested():
        raise WorkflowCancelled("Workflow stopped by user.")


THEMES = {
    "midnight": {
        "bg": "#191e22", "panel": "#20262b", "field": "#171c20", "hover": "#2b343b",
        "text": "#edf1ed", "muted": "#a5b1b7", "border": "#3c474f", "accent": "#9bdbc2",
        "accent_hover": "#b1e6d1", "on_accent": "#16352b", "tint": "#293f37",
        "warning": "#e8c28e", "error": "#f1a0a0", "disabled": "#849099",
    },
    "paper": {
        "bg": "#f3f1eb", "panel": "#faf9f5", "field": "#fdfcf8", "hover": "#e8e7df",
        "text": "#293832", "muted": "#5f6c64", "border": "#bec6bd", "accent": "#35664f",
        "accent_hover": "#2b5541", "on_accent": "#f7faf4", "tint": "#e1ece1",
        "warning": "#865b20", "error": "#a13e3e", "disabled": "#727d74",
    },
}


def load_app_stylesheet(theme: str = "midnight") -> str:
    try:
        stylesheet = APP_STYLE_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Could not read stylesheet: {APP_STYLE_PATH}") from exc
    for name, value in THEMES.get(theme, THEMES["midnight"]).items():
        stylesheet = stylesheet.replace("@" + name + "@", value)
    return stylesheet.replace("@chevron@", (STYLES_DIR / "chevron-down.svg").as_posix())


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return env


def capture_command(
    cmd: list[str],
    cwd: Path | None = None,
    on_process: Callable[[subprocess.Popen[str] | None], None] | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> subprocess.CompletedProcess[str]:
    _raise_if_cancelled(cancel_requested)
    try:
        process = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_subprocess_env(),
            **_popen_session_kwargs(),
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"Command not found: {cmd[0]}") from exc
    if on_process:
        on_process(process)

    try:
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                _raise_if_cancelled(cancel_requested)
        _raise_if_cancelled(cancel_requested)
        return subprocess.CompletedProcess(
            cmd,
            process.returncode if process.returncode is not None else 0,
            stdout,
            stderr,
        )
    except WorkflowCancelled:
        _terminate_process_tree(process)
        try:
            process.communicate(timeout=1)
        except Exception:
            pass
        raise
    finally:
        if on_process:
            on_process(None)


def run_command(
    cmd: list[str],
    logger: Callable[[str], None],
    cwd: Path | None = None,
    on_process: Callable[[subprocess.Popen[str] | None], None] | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> None:
    _raise_if_cancelled(cancel_requested)
    try:
        process = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=_subprocess_env(),
            **_popen_session_kwargs(),
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"Command not found: {cmd[0]}") from exc
    if on_process:
        on_process(process)

    try:
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.rstrip()
            if line:
                logger(line)
            _raise_if_cancelled(cancel_requested)

        exit_code = process.wait()
        if cancel_requested and cancel_requested():
            raise WorkflowCancelled("Workflow stopped by user.")
        if exit_code != 0:
            raise RuntimeError(f"Command exited with status {exit_code}.")
    except WorkflowCancelled:
        _terminate_process_tree(process)
        raise
    finally:
        if on_process:
            on_process(None)


def probe_video_metadata(
    url: str,
    cookie_text: str,
    logger: Callable[[str], None],
    on_process: Callable[[subprocess.Popen[str] | None], None] | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> dict[str, object]:
    logger("Probing video metadata with yt-dlp...")
    with cookie_file(cookie_text, netscape=True) as session_path:
        result = capture_command(
            [
                sys.executable,
                "-m",
                "yt_dlp",
                "--dump-single-json",
                "--no-download",
                "--no-playlist",
                "--cookies",
                str(session_path),
                "--add-header",
                f"User-Agent: {WEVERSE_USER_AGENT}",
                "--add-header",
                "Referer: https://weverse.io/",
                url,
            ],
            cwd=REPO_ROOT,
            on_process=on_process,
            cancel_requested=cancel_requested,
        )

    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if detail:
            logger(detail.splitlines()[-1])
        logger("Metadata probe failed. Falling back to a timestamped output folder.")
        return {}

    payload = ""
    for line in result.stdout.splitlines():
        if line.strip():
            payload = line.strip()

    if not payload:
        logger("yt-dlp returned no metadata. Falling back to generic naming.")
        return {}

    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        logger("Could not parse yt-dlp metadata output. Falling back to generic naming.")
        return {}


def find_downloaded_video(output_dir: Path) -> Path | None:
    matches = [
        path
        for path in output_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() in VIDEO_EXTENSIONS
        and not path.name.endswith("_chat_burned.mp4")
    ]
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime)


def download_video(
    url: str,
    cookie_text: str,
    output_dir: Path,
    logger: Callable[[str], None],
    on_process: Callable[[subprocess.Popen[str] | None], None] | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> Path:
    logger("Downloading video into the run folder...")
    with cookie_file(cookie_text, netscape=True) as session_path:
        run_command(
            [
                sys.executable,
                "-m",
                "yt_dlp",
                "--newline",
                "--no-playlist",
                "--merge-output-format",
                "mp4",
                "--restrict-filenames",
                "--cookies",
                str(session_path),
                "--add-header",
                f"User-Agent: {WEVERSE_USER_AGENT}",
                "--add-header",
                "Referer: https://weverse.io/",
                "-o",
                str(output_dir / "%(title)s.%(ext)s"),
                url,
            ],
            logger,
            cwd=REPO_ROOT,
            on_process=on_process,
            cancel_requested=cancel_requested,
        )

    video_path = find_downloaded_video(output_dir)
    if video_path is None:
        raise RuntimeError("yt-dlp completed, but no downloaded video file was found.")

    logger(f"Downloaded video: {video_path.name}")
    return video_path


def positive_int(value: object) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def resolve_video_dimensions(
    video_path: Path,
    fallback_width: object,
    fallback_height: object,
    logger: Callable[[str], None],
    on_process: Callable[[subprocess.Popen[str] | None], None] | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> tuple[int, int]:
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        result = capture_command(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                str(video_path),
            ],
            on_process=on_process,
            cancel_requested=cancel_requested,
        )
        if result.returncode == 0:
            try:
                payload = json.loads(result.stdout)
                streams = payload.get("streams") or []
                if streams:
                    width = positive_int(streams[0].get("width"))
                    height = positive_int(streams[0].get("height"))
                    if width and height:
                        logger(f"Using video resolution {width}x{height} for the ASS overlay.")
                        return width, height
            except json.JSONDecodeError:
                logger("ffprobe output was not valid JSON. Falling back to metadata defaults.")

    width = positive_int(fallback_width) or 1080
    height = positive_int(fallback_height) or 1920
    logger(f"Using fallback resolution {width}x{height} for the ASS overlay.")
    return width, height


def build_ass(
    chat_json: Path,
    ass_path: Path,
    resx: int,
    resy: int,
    logger: Callable[[str], None],
    on_process: Callable[[subprocess.Popen[str] | None], None] | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> None:
    logger("Rendering the chat ASS overlay...")
    run_command(
        [
            sys.executable,
            str(SCRIPTS_DIR / "weverse_chat_to_ass_twitch.py"),
            "--chat",
            str(chat_json),
            "--ass",
            str(ass_path),
            "--resx",
            str(resx),
            "--resy",
            str(resy),
        ],
        logger,
        cwd=REPO_ROOT,
        on_process=on_process,
        cancel_requested=cancel_requested,
    )


def detect_nanum_font_dir() -> Path | None:
    candidates: list[Path] = []

    windir = os.environ.get("WINDIR")
    localappdata = os.environ.get("LOCALAPPDATA")
    if windir:
        candidates.append(Path(windir) / "Fonts")
    if localappdata:
        candidates.append(Path(localappdata) / "Microsoft/Windows/Fonts")

    candidates.extend(
        [
            Path("/mnt/c/Windows/Fonts"),
            Path.home() / ".local/share/fonts",
            Path.home() / ".fonts",
        ]
    )

    users_root = Path("/mnt/c/Users")
    if users_root.exists():
        candidates.extend(users_root.glob("*/AppData/Local/Microsoft/Windows/Fonts"))

    for candidate in candidates:
        if not candidate.exists():
            continue
        for pattern in ("NanumGothic*.ttf", "NanumGothic*.otf"):
            if any(candidate.glob(pattern)):
                return candidate

    return None


def ffmpeg_filter_path(path: Path) -> str:
    escaped = str(path.resolve()).replace("\\", "/")
    escaped = escaped.replace(":", r"\:")
    escaped = escaped.replace("'", r"\'")
    escaped = escaped.replace(",", r"\,")
    escaped = escaped.replace("[", r"\[")
    escaped = escaped.replace("]", r"\]")
    return escaped


def burn_subtitles(
    video_path: Path,
    ass_path: Path,
    logger: Callable[[str], None],
    on_process: Callable[[subprocess.Popen[str] | None], None] | None = None,
    cancel_requested: Callable[[], bool] | None = None,
    *,
    chat_json: Path | None = None,
    resolution: tuple[int, int] | None = None,
    output_path: Path | None = None,
) -> Path | None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        logger("ffmpeg was not found on PATH. Skipping burned-in video output.")
        return None

    output_path = output_path or video_path.with_name(f"{video_path.stem}_chat_burned.mp4")
    if output_path.resolve() == video_path.resolve():
        raise ValueError("Burn-in output must be different from the source video.")
    if chat_json is not None:
        try:
            width, height = resolution or resolve_video_dimensions(
                video_path, None, None, logger, on_process, cancel_requested,
            )
            # The parent owns temporary frames so cancellation of either child
            # process still removes them after run_command reaps that process.
            with tempfile.TemporaryDirectory(prefix="weverse-chat-", dir=output_path.parent) as tmp:
                frames_dir = Path(tmp)
                command = [sys.executable, str(SCRIPTS_DIR / "weverse_chat_render.py"),
                           "--chat", str(chat_json), "--output-dir", str(frames_dir),
                           "--resx", str(width), "--resy", str(height)]
                font_dir = detect_nanum_font_dir()
                if font_dir is not None:
                    command.extend(["--font-dir", str(font_dir)])
                logger("Rendering colored viewer names and native color emoji...")
                run_command(command, logger, cwd=REPO_ROOT, on_process=on_process,
                            cancel_requested=cancel_requested)
                logger("Burning the color chat overlay into the final video...")
                run_command(
                    [ffmpeg, "-y", "-i", str(video_path), "-f", "concat", "-safe", "0",
                     "-i", str(frames_dir / "chat.ffconcat"),
                     "-filter_complex", "[0:v:0][1:v:0]overlay=x=0:y=main_h-overlay_h:format=auto:eof_action=pass[video]",
                     "-map", "[video]", "-map", "0:a?", "-c:v", "libx264", "-crf", "18",
                     "-preset", "medium", "-pix_fmt", "yuv420p", "-c:a", "copy",
                     "-movflags", "+faststart", str(output_path)],
                    logger, cwd=REPO_ROOT, on_process=on_process, cancel_requested=cancel_requested,
                )
        except WorkflowCancelled:
            raise
        except (RuntimeError, OSError) as exc:
            logger(f"Color chat burn-in failed: {exc}")
            return None
        return output_path

    subtitle_filter = f"subtitles='{ffmpeg_filter_path(ass_path)}'"

    font_dir = detect_nanum_font_dir()
    if font_dir is not None:
        subtitle_filter += f":fontsdir='{ffmpeg_filter_path(font_dir)}'"
        logger(f"Using Nanum Gothic font directory: {font_dir}")
    else:
        logger("Nanum Gothic was not detected automatically. ffmpeg will use its default font lookup.")

    logger("Burning the chat overlay into the final video...")
    try:
        run_command(
            [
                ffmpeg,
                "-y",
                "-i",
                str(video_path),
                "-vf",
                subtitle_filter,
                "-c:a",
                "copy",
                str(output_path),
            ],
            logger,
            cwd=REPO_ROOT,
            on_process=on_process,
            cancel_requested=cancel_requested,
        )
    except WorkflowCancelled:
        raise
    except RuntimeError as exc:
        logger(f"ffmpeg burn-in failed: {exc}")
        return None

    return output_path


def run_workflow(
    cookie_text: str,
    url: str,
    logger: Callable[[str], None],
    set_status: Callable[[str], None],
    set_progress: Callable[[float], None],
    mark_artifact: Callable[[str, bool], None],
    set_output_dir: Callable[[Path], None] | None = None,
    on_process: Callable[[subprocess.Popen[str] | None], None] | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> dict[str, object]:
    cookie_text = normalize_cookie(cookie_text)
    _raise_if_cancelled(cancel_requested)
    set_status("Preparing workspace")
    set_progress(5)
    metadata = probe_video_metadata(
        url,
        cookie_text,
        logger,
        on_process=on_process,
        cancel_requested=cancel_requested,
    )
    _raise_if_cancelled(cancel_requested)
    folder_title = str(metadata.get("fulltitle") or metadata.get("title") or "weverse-chat")
    output_dir = make_output_dir(folder_title)
    if set_output_dir:
        set_output_dir(output_dir)
    mark_artifact("folder", True)
    title_path = write_title_file(output_dir, folder_title)

    warnings: list[str] = []

    logger(f"Output folder: {output_dir}")
    logger(f"Live title written to: {title_path}")

    with cookie_file(cookie_text) as cookie_path:
        _raise_if_cancelled(cancel_requested)
        set_status("Downloading video")
        set_progress(20)
        video_path = download_video(
            url,
            cookie_text,
            output_dir,
            logger,
            on_process=on_process,
            cancel_requested=cancel_requested,
        )
        mark_artifact("video", True)

        width, height = resolve_video_dimensions(
            video_path,
            metadata.get("width"),
            metadata.get("height"),
            logger,
            on_process=on_process,
            cancel_requested=cancel_requested,
        )

        _raise_if_cancelled(cancel_requested)
        set_status("Dumping replay chat")
        set_progress(45)
        chat_json = output_dir / "weverse_chat.json"
        logger("Launching Chrome to collect replay chat...")
        run_command(
            [
                sys.executable,
                str(SCRIPTS_DIR / "weverse_chat_dump.py"),
                "--cookies",
                str(cookie_path),
                "--url",
                url,
                "--out",
                str(chat_json),
                "--no-headless",
            ],
            logger,
            cwd=REPO_ROOT,
            on_process=on_process,
            cancel_requested=cancel_requested,
        )
        if not chat_json.exists():
            raise RuntimeError("Chat dump finished without creating weverse_chat.json.")
        mark_artifact("chat_json", True)

        _raise_if_cancelled(cancel_requested)
        set_status("Rendering ASS overlay")
        set_progress(70)
        ass_path = output_dir / "weverse_twitch_chat.ass"
        build_ass(
            chat_json,
            ass_path,
            width,
            height,
            logger,
            on_process=on_process,
            cancel_requested=cancel_requested,
        )
        mark_artifact("ass", True)

        _raise_if_cancelled(cancel_requested)
        set_status("Burning subtitles")
        set_progress(85)
        burned_path = burn_subtitles(
            video_path,
            ass_path,
            logger,
            on_process=on_process,
            cancel_requested=cancel_requested,
            chat_json=chat_json,
            resolution=(width, height),
        )
        if burned_path is None:
            warnings.append(
                "The burned-in MP4 was not generated. The downloaded video, chat JSON, and ASS overlay are still ready."
            )
            mark_artifact("burned", False)
        else:
            mark_artifact("burned", True)

        set_status("Complete")
        set_progress(100)
        return {
            "output_dir": output_dir,
            "video_path": video_path,
            "title_path": title_path,
            "chat_json": chat_json,
            "ass_path": ass_path,
            "burned_path": burned_path,
            "warnings": warnings,
        }


@dataclass
class ReplayItem:
    url: str
    status: str = "Queued"
    stage: str = "Waiting to start"
    progress: float = 0
    output_dir: Path | None = None
    artifacts: dict[str, bool] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str = ""


class WeverseChatStudio(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1220, 850)
        self.setMinimumSize(880, 620)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.items: list[ReplayItem] = []
        self.output_dir: Path | None = None
        self.is_running = False
        self.closing = False
        self.stop_requested = threading.Event()
        self.active_process: subprocess.Popen[str] | None = None
        self.active_process_lock = threading.Lock()
        self.batch_indices: list[int] = []
        # Keep the existing settings namespace so saved preferences survive the rename.
        self.settings = QSettings(QSettings.defaultFormat(), QSettings.UserScope, "weverse-dlt", "ReplayStudio")
        self.artifact_bullets: dict[str, QLabel] = {}
        self._build_ui()
        theme = str(self.settings.value("appearance", "midnight"))
        self._apply_theme(theme if theme in THEMES else "midnight")
        self._set_run_state("idle", "Ready")
        self._refresh_controls()
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(100)
        self.poll_timer.timeout.connect(self._poll_events)
        self.poll_timer.start()

    @staticmethod
    def _label(text: str, name: str = "", wrap: bool = False) -> QLabel:
        label = QLabel(text)
        label.setTextFormat(Qt.PlainText)
        label.setObjectName(name)
        label.setWordWrap(wrap)
        return label

    @staticmethod
    def _button(text: str, callback, variant: str = "secondary") -> QPushButton:
        button = QPushButton(text)
        button.setProperty("variant", variant)
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(callback)
        return button

    @staticmethod
    def _divider() -> QFrame:
        line = QFrame()
        line.setObjectName("divider")
        line.setFixedHeight(1)
        return line

    def _build_ui(self) -> None:
        page = QWidget()
        page.setObjectName("page")
        self.setCentralWidget(page)
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        header = QWidget()
        header.setObjectName("header")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(26, 18, 26, 18)
        header_layout.setSpacing(14)
        mark = self._label("w", "brandMark")
        mark.setAlignment(Qt.AlignCenter)
        mark.setFixedSize(38, 38)
        header_layout.addWidget(mark)
        header_layout.addWidget(self._label(APP_TITLE, "brandName"))
        header_layout.addStretch()
        self.theme_button = self._button(
            "", lambda: self._apply_theme("paper" if self.theme == "midnight" else "midnight"), "theme"
        )
        self.theme_button.setIconSize(QSize(20, 20))
        self.theme_button.setFixedSize(38, 38)
        header_layout.addWidget(self.theme_button)
        self.run_state_chip = self._label("Ready", "runStateChip")
        self.run_state_chip.setAlignment(Qt.AlignCenter)
        header_layout.addSpacing(12)
        header_layout.addWidget(self.run_state_chip)
        root.addWidget(header)
        root.addWidget(self._divider())

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        sidebar_scroll = QScrollArea()
        sidebar_scroll.setWidgetResizable(True)
        sidebar_scroll.setFrameShape(QFrame.NoFrame)
        sidebar_scroll.setMinimumWidth(286)
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar_scroll.setWidget(sidebar)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(26, 28, 26, 24)
        side.setSpacing(8)
        side.addWidget(self._label("1. Session", "sectionTitle"))
        side.addSpacing(6)
        browser_label = self._label("Browser", "fieldLabel")
        self.browser_combo = QComboBox()
        for title in ("Zen", "Chrome", "Edge", "Firefox", "Brave"):
            self.browser_combo.addItem(title, title.lower())
        browser_index = self.browser_combo.findData(str(self.settings.value("browser", "zen")))
        self.browser_combo.setCurrentIndex(max(0, browser_index))
        self.browser_combo.currentIndexChanged.connect(
            lambda: self.settings.setValue("browser", self.browser_combo.currentData())
        )
        self.browser_combo.setAccessibleName("Browser to import cookies from")
        browser_label.setBuddy(self.browser_combo)
        side.addWidget(browser_label)
        side.addWidget(self.browser_combo)
        self.import_cookie_button = self._button("Import browser session", lambda: self._start_cookie_import("import"), "primary")
        self.signin_button = self._button("Sign in with Chrome", lambda: self._start_cookie_import("signin"))
        side.addWidget(self.import_cookie_button)
        side.addWidget(self.signin_button)
        self.session_status = self._label("No session connected", "sessionStatus", True)
        side.addWidget(self.session_status)
        self.disconnect_button = self._button("Disconnect", self._disconnect, "quiet")
        side.addWidget(self.disconnect_button, 0, Qt.AlignLeft)

        self.manual_toggle = QToolButton()
        self.manual_toggle.setText("Manual cookie && profile")
        self.manual_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.manual_toggle.setArrowType(Qt.RightArrow)
        self.manual_toggle.setCheckable(True)
        side.addWidget(self.manual_toggle)
        self.manual_panel = QWidget()
        manual = QVBoxLayout(self.manual_panel)
        manual.setContentsMargins(0, 4, 0, 0)
        manual.setSpacing(8)
        profile_label = self._label("Browser profile (optional)", "fieldLabel")
        self.profile_entry = QLineEdit()
        self.profile_entry.setPlaceholderText("Auto-detect, or enter profile name / path")
        self.profile_entry.setToolTip("For Zen, find Profile Directory in about:support. Leave blank to use its default profile.")
        profile_label.setBuddy(self.profile_entry)
        manual.addWidget(profile_label)
        manual.addWidget(self.profile_entry)
        cookie_label = self._label("Manual cookie", "fieldLabel")
        self.cookie_text = QLineEdit()
        self.cookie_text.setEchoMode(QLineEdit.Password)
        self.cookie_text.setPlaceholderText("Paste the full Cookie header")
        self.cookie_text.setAccessibleName("Weverse cookie, hidden")
        self.cookie_text.setToolTip("Paste document.cookie output or a Cookie header. Values stay hidden; temporary cookie files are removed after use.")
        self.cookie_text.textEdited.connect(self._manual_cookie_changed)
        cookie_label.setBuddy(self.cookie_text)
        manual.addWidget(cookie_label)
        manual.addWidget(self.cookie_text)
        side.addWidget(self.manual_panel)
        self.manual_panel.hide()
        self.manual_toggle.toggled.connect(self._toggle_manual)
        side.addSpacing(12)
        side.addWidget(self._divider())
        side.addSpacing(12)
        self.detail_title = self._label("3. Outputs", "sectionTitle", True)
        side.addWidget(self.detail_title)
        self.detail_status = self._label("", "muted", True)
        self.detail_status.hide()
        side.addWidget(self.detail_status)
        self.item_progress = QProgressBar()
        self.item_progress.setRange(0, 100)
        self.item_progress.setValue(0)
        self.item_progress.setTextVisible(False)
        self.item_progress.setFixedHeight(5)
        self.item_progress.setAccessibleName("Selected replay stage progress")
        self.item_progress.setToolTip("Burn-in needs ffmpeg. Other files remain available if rendering is skipped.")
        side.addWidget(self.item_progress)
        for label, key in (("Folder + original title", "folder"), ("Source video", "video"),
                           ("Replay chat · JSON", "chat_json"), ("Chat overlay · ASS", "ass"),
                           ("Video with chat · MP4", "burned")):
            row = QHBoxLayout()
            bullet = self._label("○", "artifactBullet")
            bullet.setFixedWidth(20)
            row.addWidget(bullet)
            row.addWidget(self._label(label, "artifactText"), 1)
            self.artifact_bullets[key] = bullet
            side.addLayout(row)
        side.addStretch()
        self.open_folder_button = self._button("Open selected folder", self._open_output_folder)
        side.addWidget(self.open_folder_button)
        splitter.addWidget(sidebar_scroll)

        workspace_scroll = QScrollArea()
        workspace_scroll.setWidgetResizable(True)
        workspace_scroll.setFrameShape(QFrame.NoFrame)
        workspace_scroll.setMinimumWidth(540)
        workspace = QWidget()
        workspace.setObjectName("workspace")
        workspace_scroll.setWidget(workspace)
        main = QVBoxLayout(workspace)
        main.setContentsMargins(30, 28, 30, 24)
        main.setSpacing(10)
        title_row = QHBoxLayout()
        title_row.addWidget(self._label("2. Queue", "pageTitle"), 1)
        self.queue_count = self._label("0 replays", "muted")
        title_row.addWidget(self.queue_count)
        main.addLayout(title_row)
        self.url_entry = QPlainTextEdit()
        self.url_entry.setPlaceholderText("Paste Weverse replay links, one per line\nhttps://weverse.io/group/live/replay-id")
        self.url_entry.setAccessibleName("Weverse replay links, one per line")
        self.url_entry.setFixedHeight(82)
        self.url_entry.setTabChangesFocus(True)
        main.addWidget(self.url_entry)
        input_actions = QHBoxLayout()
        self.load_links_button = self._button("Import .txt", self._import_links)
        self.add_links_button = self._button("Add to queue", self._add_links, "primary")
        input_actions.addWidget(self.load_links_button)
        input_actions.addStretch()
        input_actions.addWidget(self.add_links_button)
        main.addLayout(input_actions)
        self.input_feedback = self._label("", "finePrint", True)
        main.addWidget(self.input_feedback)
        self.input_feedback.hide()

        self.queue_stack = QStackedWidget()
        self.queue_stack.setMinimumHeight(150)
        empty = QWidget()
        empty.setObjectName("emptyQueue")
        empty_layout = QVBoxLayout(empty)
        empty_layout.setContentsMargins(20, 20, 20, 20)
        empty_layout.setSpacing(10)
        empty_layout.addStretch()
        empty_icon = self._label("")
        empty_icon.setPixmap(QIcon(str(STYLES_DIR / "video-plus.svg")).pixmap(QSize(40, 40)))
        empty_icon.setAlignment(Qt.AlignCenter)
        empty_layout.addWidget(empty_icon)
        empty_title = self._label("Add a live to get started", "sectionTitle", True)
        empty_title.setAlignment(Qt.AlignCenter)
        empty_layout.addWidget(empty_title)
        empty_layout.addStretch()
        self.queue_stack.addWidget(empty)
        self.queue_table = QTableWidget(0, 4)
        self.queue_table.setAccessibleName("Replay queue")
        self.queue_table.setHorizontalHeaderLabels(["#", "REPLAY", "STATUS", "FILES"])
        self.queue_table.verticalHeader().hide()
        self.queue_table.verticalHeader().setDefaultSectionSize(48)
        self.queue_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.queue_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.queue_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.queue_table.setShowGrid(False)
        self.queue_table.setWordWrap(False)
        self.queue_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for column, width in ((0, 42), (2, 140), (3, 98)):
            self.queue_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.Fixed)
            self.queue_table.setColumnWidth(column, width)
        self.queue_table.itemSelectionChanged.connect(self._render_details)
        self.queue_table.cellDoubleClicked.connect(lambda row, col: self._open_item_folder(row))
        self.queue_table.cellClicked.connect(lambda row, col: self._open_item_folder(row) if col == 3 else None)
        self.queue_stack.addWidget(self.queue_table)
        main.addWidget(self.queue_stack, 1)
        queue_actions = QHBoxLayout()
        self.remove_button = self._button("Remove selected", self._remove_selected, "quiet")
        self.retry_button = self._button("Retry unfinished", self._retry_unfinished, "quiet")
        self.clear_button = self._button("Clear all", self._clear_inputs, "quiet")
        queue_actions.addWidget(self.remove_button)
        queue_actions.addWidget(self.retry_button)
        queue_actions.addStretch()
        queue_actions.addWidget(self.clear_button)
        main.addLayout(queue_actions)
        main.addWidget(self._divider())
        run_row = QHBoxLayout()
        self.batch_status = self._label("Ready when you are", "fieldLabel", True)
        run_row.addWidget(self.batch_status, 1)
        self.stop_button = self._button("Stop", self._request_stop, "danger")
        self.start_button = self._button("Start queue", self._start_workflow, "primary")
        run_row.addWidget(self.stop_button)
        run_row.addWidget(self.start_button)
        main.addLayout(run_row)
        self.batch_progress = QProgressBar()
        self.batch_progress.setRange(0, 100)
        self.batch_progress.setValue(0)
        self.batch_progress.setFormat("%p% of queue")
        self.batch_progress.setTextVisible(False)
        self.batch_progress.setFixedHeight(5)
        self.batch_progress.setAccessibleName("Queue stage progress")
        main.addWidget(self.batch_progress)
        log_header = QHBoxLayout()
        self.log_toggle = QToolButton()
        self.log_toggle.setText("Activity log")
        self.log_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.log_toggle.setArrowType(Qt.DownArrow)
        self.log_toggle.setCheckable(True)
        self.log_toggle.setChecked(True)
        log_header.addWidget(self.log_toggle)
        log_header.addStretch()
        main.addLayout(log_header)
        self.log_text = QPlainTextEdit()
        self.log_text.setObjectName("activityLog")
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumBlockCount(3000)
        self.log_text.setFixedHeight(100)
        self.log_text.setPlaceholderText("Downloads, chat collection, and rendering updates appear here.")
        self.log_text.setAccessibleName("Workflow activity log")
        main.addWidget(self.log_text)
        self.log_toggle.toggled.connect(self._toggle_log)
        splitter.addWidget(workspace_scroll)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([332, 888])
        root.addWidget(splitter, 1)

    def _apply_theme(self, theme: str) -> None:
        self.theme = theme
        app = QApplication.instance()
        palette = QPalette()
        colors = THEMES[theme]
        for role, color in ((QPalette.Window, "bg"), (QPalette.WindowText, "text"),
                            (QPalette.Base, "field"), (QPalette.AlternateBase, "panel"),
                            (QPalette.Text, "text"), (QPalette.Button, "panel"),
                            (QPalette.ButtonText, "text"), (QPalette.Highlight, "accent"),
                            (QPalette.HighlightedText, "on_accent"),
                            (QPalette.ToolTipBase, "panel"), (QPalette.ToolTipText, "text")):
            palette.setColor(role, QColor(colors[color]))
        app.setPalette(palette)
        app.setStyleSheet(load_app_stylesheet(theme))
        icon = "sun.svg" if theme == "midnight" else "moon.svg"
        action = "Switch to light theme (Paper)" if theme == "midnight" else "Switch to dark theme (Midnight)"
        self.theme_button.setIcon(QIcon(str(STYLES_DIR / icon)))
        self.theme_button.setToolTip(action)
        self.theme_button.setAccessibleName(action)
        self.settings.setValue("appearance", theme)
        for index in range(len(self.items)):
            self._update_row(index)

    def _toggle_manual(self, visible: bool) -> None:
        self.manual_panel.setVisible(visible)
        self.manual_toggle.setArrowType(Qt.DownArrow if visible else Qt.RightArrow)

    def _toggle_log(self, visible: bool) -> None:
        self.log_text.setVisible(visible)
        self.log_toggle.setArrowType(Qt.DownArrow if visible else Qt.RightArrow)

    def _manual_cookie_changed(self) -> None:
        has_cookie = bool(parse_cookie_header(self.cookie_text.text()))
        self._set_session_status("Manual session ready (not verified)" if has_cookie else "No session connected", "ready" if has_cookie else "idle")
        self._refresh_controls()

    def _set_session_status(self, message: str, state: str = "idle") -> None:
        self.session_status.setText(message)
        self.session_status.setProperty("state", state)
        self.session_status.style().unpolish(self.session_status)
        self.session_status.style().polish(self.session_status)

    def _disconnect(self) -> None:
        if self.is_running:
            return
        self.cookie_text.clear()
        self._set_session_status("No session connected")
        self._refresh_controls()

    def _feedback(self, message: str, error: bool = False) -> None:
        self.input_feedback.setText(message)
        self.input_feedback.setVisible(bool(message))
        self.input_feedback.setProperty("error", error)
        self.input_feedback.style().unpolish(self.input_feedback)
        self.input_feedback.style().polish(self.input_feedback)

    def _add_links(self, checked: bool = False, text: str | None = None) -> bool:
        if self.is_running:
            return False
        from_editor = text is None
        text = self.url_entry.toPlainText() if from_editor else text
        try:
            links = parse_replay_links(text)
        except ValueError as exc:
            self._feedback(str(exc), True)
            return False
        if not links:
            self._feedback("Paste at least one Weverse replay link.", True)
            return False
        existing = {item.url for item in self.items}
        additions = [ReplayItem(url) for url in links if url not in existing]
        self.items.extend(additions)
        self._refresh_table()
        duplicate_count = len(text.split()) - len(additions)
        self._feedback(f"Added {len(additions)} replay(s)." + (f" Skipped {duplicate_count} duplicate(s)." if duplicate_count else ""))
        if from_editor:
            self.url_entry.clear()
        return True

    def _import_links(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "Import replay links", "", "Text files (*.txt);;All files (*)")
        if filename:
            try:
                self._add_links(text=Path(filename).read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError) as exc:
                self._feedback(f"Could not read this UTF-8 links file: {exc}", True)

    def _refresh_table(self) -> None:
        selected = self.queue_table.currentRow()
        self.queue_table.setRowCount(len(self.items))
        for index in range(len(self.items)):
            self._update_row(index)
        self.queue_stack.setCurrentIndex(1 if self.items else 0)
        self.queue_count.setText(f"{len(self.items)} replay{'s' if len(self.items) != 1 else ''}")
        if self.items:
            self.queue_table.selectRow(max(0, min(selected, len(self.items) - 1)))
        self._render_details()
        self._refresh_controls()

    def _update_row(self, index: int) -> None:
        item = self.items[index]
        replay = item.url.removeprefix("https://weverse.io/")
        values = (f"{index + 1:02}", replay, item.status, "Open folder" if item.output_dir else "Waiting")
        for col, value in enumerate(values):
            cell = self.queue_table.item(index, col)
            if cell is None:
                cell = QTableWidgetItem()
                self.queue_table.setItem(index, col, cell)
            cell.setText(value)
            cell.setToolTip(str(item.output_dir) if col == 3 and item.output_dir else (item.error or item.url))
            if col in (0, 2, 3):
                cell.setTextAlignment(Qt.AlignVCenter | Qt.AlignHCenter)
            tone = ("error" if item.status == "Failed" else "warning" if item.status in ("Warning", "Stopped")
                    else "accent" if item.status in ("Complete", "Running") else "muted")
            cell.setForeground(QColor(THEMES[self.theme][tone if col == 2 else "text"]))

    def _remove_selected(self) -> None:
        row = self.queue_table.currentRow()
        if not self.is_running and 0 <= row < len(self.items):
            self.items.pop(row)
            self._refresh_table()

    def _retry_unfinished(self) -> None:
        if self.is_running:
            return
        for item in self.items:
            if item.status in ("Failed", "Stopped", "Warning"):
                item.status = "Queued"
                item.stage = "Ready to retry"
                item.error = ""
        self._refresh_table()

    def _clear_inputs(self) -> None:
        if self.is_running:
            return
        self._disconnect()
        self.url_entry.clear()
        self.items.clear()
        self.batch_indices.clear()
        self._refresh_table()
        self.batch_progress.setValue(0)
        self.batch_status.setText("Ready when you are")
        self._set_run_state("idle", "Ready")
        self._feedback("")

    def _render_details(self) -> None:
        row = self.queue_table.currentRow()
        item = self.items[row] if 0 <= row < len(self.items) else None
        self.output_dir = item.output_dir if item else None
        self.detail_status.setText((item.error or "\n".join(item.warnings) or item.stage) if item else "")
        self.detail_status.setVisible(item is not None)
        self.item_progress.setValue(int(item.progress) if item else 0)
        for key, bullet in self.artifact_bullets.items():
            done = bool(item and item.artifacts.get(key))
            active = bool(item and item.status == "Running" and STATUS_TO_ARTIFACT.get(item.stage) == key)
            bullet.setText("✓" if done else "›" if active else "○")
            bullet.setProperty("done", done)
            bullet.setProperty("active", active)
            bullet.style().unpolish(bullet)
            bullet.style().polish(bullet)
        self.open_folder_button.setEnabled(self.output_dir is not None and self.output_dir.exists())
        self.remove_button.setEnabled(not self.is_running and item is not None)

    def _open_item_folder(self, row: int) -> None:
        if 0 <= row < len(self.items):
            path = self.items[row].output_dir
            if path and path.exists():
                if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                    self._feedback(f"Could not open folder: {path}", True)

    def _open_output_folder(self) -> None:
        self._open_item_folder(self.queue_table.currentRow())

    def _set_run_state(self, state: str, text: str) -> None:
        self.run_state_chip.setProperty("state", state)
        self.run_state_chip.setText(text)
        self.run_state_chip.style().unpolish(self.run_state_chip)
        self.run_state_chip.style().polish(self.run_state_chip)

    def _refresh_controls(self) -> None:
        busy = self.is_running
        for control in (self.add_links_button, self.load_links_button, self.clear_button,
                        self.import_cookie_button, self.signin_button, self.browser_combo,
                        self.profile_entry, self.disconnect_button):
            control.setEnabled(not busy)
        self.disconnect_button.setVisible(bool(self.cookie_text.text()))
        self.cookie_text.setReadOnly(busy)
        self.url_entry.setReadOnly(busy)
        self.start_button.setEnabled(not busy)
        self.start_button.setText("Working…" if busy else "Start queue")
        self.stop_button.setEnabled(busy and not self.stop_requested.is_set())
        self.stop_button.setText("Stopping…" if busy and self.stop_requested.is_set() else "Stop")
        self.remove_button.setEnabled(not busy and self.queue_table.currentRow() >= 0)
        self.retry_button.setEnabled(not busy and any(item.status in ("Failed", "Stopped", "Warning") for item in self.items))

    def _set_active_process(self, process: subprocess.Popen[str] | None) -> None:
        with self.active_process_lock:
            self.active_process = process
        if process is not None and self.stop_requested.is_set():
            _terminate_process_tree(process)

    def _begin_work(self, message: str) -> bool:
        if self.is_running or (self.worker and self.worker.is_alive()):
            return False
        self.stop_requested.clear()
        self.is_running = True
        self._set_run_state("running", "Working")
        self.batch_status.setText(message)
        self._refresh_controls()
        return True

    def _start_cookie_import(self, mode: str) -> None:
        message = "Sign in to Weverse in the Chrome window. This app will capture the session automatically." if mode == "signin" else "Reading your browser session…"
        if not self._begin_work(message):
            return
        self._set_session_status(message)
        browser = str(self.browser_combo.currentData())
        profile = self.profile_entry.text().strip()
        self.worker = threading.Thread(target=self._cookie_worker, args=(mode, browser, profile), daemon=True)
        self.worker.start()

    def _cookie_worker(self, mode: str, browser: str, profile: str) -> None:
        try:
            with tempfile.TemporaryDirectory(prefix="weverse-connect-") as directory:
                result_path = Path(directory) / "session.txt"
                cmd = [sys.executable, str(SCRIPTS_DIR / "weverse_cookies.py"), "--mode", mode,
                       "--browser", browser, "--output", str(result_path)]
                if profile:
                    cmd.extend(["--profile", profile])
                result = capture_command(cmd, cwd=REPO_ROOT, on_process=self._set_active_process,
                                         cancel_requested=self.stop_requested.is_set)
                if result.returncode != 0 or not result_path.exists():
                    # Helper stdout contains only controlled, credential-free messages.
                    raise RuntimeError(result.stdout.strip() or "Could not import session. Try Sign in with Chrome or Manual cookie.")
                cookie = result_path.read_text(encoding="utf-8")
            self.events.put(("cookie_ready", cookie))
        except WorkflowCancelled:
            self.events.put(("cookie_error", "Session connection stopped."))
        except Exception as exc:
            self.events.put(("cookie_error", str(exc)))
        finally:
            self._set_active_process(None)

    def _start_workflow(self) -> None:
        if self.is_running:
            return
        if self.url_entry.toPlainText().strip() and not self._add_links():
            return
        indices = [i for i, item in enumerate(self.items) if item.status == "Queued"]
        if not indices:
            self._feedback("Add replay links, or use Retry unfinished to queue failed or stopped items.", True)
            return
        cookie = normalize_cookie(self.cookie_text.text())
        if not cookie:
            self._feedback("Connect a browser session or paste a manual cookie before starting.", True)
            return
        if not self._begin_work(f"Starting {len(indices)} replay(s)…"):
            return
        self.batch_indices = indices
        for index in indices:
            self.items[index].progress = 0
        self.batch_progress.setValue(0)
        self._feedback("Queue runs in order. A failed replay does not stop the next one.")
        self._append_log(f"Starting queue with {len(indices)} replay(s).")
        # Immutable snapshot: only the UI thread changes table state.
        jobs = [(index, self.items[index].url) for index in indices]
        self.worker = threading.Thread(target=self._run_worker, args=(cookie, jobs), daemon=True)
        self.worker.start()

    def _run_worker(self, cookie: str, jobs: list[tuple[int, str]]) -> None:
        emit = lambda kind, payload: self.events.put((kind, payload))
        for index, url in jobs:
            if self.stop_requested.is_set():
                break
            emit("item_started", index)
            safe = lambda message: redact_cookies(str(message), cookie)
            try:
                result = run_workflow(
                    cookie, url, lambda message: emit("log", safe(message)),
                    lambda value: emit("item_status", (index, value)),
                    lambda value: emit("item_progress", (index, value)),
                    lambda key, value: emit("item_artifact", (index, key, value)),
                    set_output_dir=lambda value: emit("item_folder", (index, str(value))),
                    on_process=self._set_active_process, cancel_requested=self.stop_requested.is_set,
                )
                _raise_if_cancelled(self.stop_requested.is_set)
                emit("item_success", (index, result))
            except WorkflowCancelled:
                emit("item_stopped", index)
                break
            except Exception as exc:
                if self.stop_requested.is_set():
                    emit("item_stopped", index)
                    break
                emit("item_error", (index, safe(exc)))
            finally:
                self._set_active_process(None)
        emit("batch_done", self.stop_requested.is_set())

    def _poll_events(self) -> None:
        # Bound each tick so a noisy downloader cannot starve Qt input events.
        for _ in range(250):
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._append_log(str(payload))
            elif kind in ("cookie_ready", "cookie_error"):
                self.is_running = False
                if kind == "cookie_ready":
                    self.cookie_text.setText(str(payload))
                    self._set_session_status("Session imported. Ready to use.", "ready")
                    self._set_run_state("success", "Connected")
                    self.batch_status.setText("Session ready. Start your queue.")
                else:
                    self._set_session_status(str(payload) + (" Your previous session is still available." if self.cookie_text.text() else ""), "warning")
                    self._set_run_state("warning", "Connection paused")
                    self.batch_status.setText("Check the session panel to continue.")
                self._refresh_controls()
            elif kind == "batch_done":
                self._finish_batch(bool(payload))
            else:
                self._handle_item_event(kind, payload)
        if self.closing and not self.is_running and not (self.worker and self.worker.is_alive()):
            self.close()

    def _handle_item_event(self, kind: str, payload: object) -> None:
        index = int(payload if kind in ("item_started", "item_stopped") else payload[0])
        item = self.items[index]
        if kind == "item_started":
            item.status, item.stage = "Running", "Preparing workspace"
            item.progress = 0
            item.artifacts.clear()
            item.warnings.clear()
            item.error = ""
            item.output_dir = None
            self.queue_table.selectRow(index)
            position = self.batch_indices.index(index) + 1
            self.batch_status.setText(f"Replay {position} of {len(self.batch_indices)} · Processing")
            self._append_log(f"Replay {index + 1}: {item.url}")
        elif kind == "item_status":
            item.stage = str(payload[1])
        elif kind == "item_progress":
            item.progress = max(0, min(100, float(payload[1])))
        elif kind == "item_artifact":
            item.artifacts[str(payload[1])] = bool(payload[2])
        elif kind == "item_folder":
            item.output_dir = Path(str(payload[1]))
        elif kind == "item_success":
            result = payload[1]
            item.output_dir = Path(result["output_dir"])
            item.warnings = list(result.get("warnings") or [])
            item.status = "Warning" if item.warnings else "Complete"
            item.stage = "Finished with warnings" if item.warnings else "All files are ready"
            item.progress = 100
            for warning in item.warnings:
                self._append_log(f"Replay {index + 1}: {warning}")
        elif kind == "item_error":
            item.status, item.stage, item.error = "Failed", "Failed", str(payload[1])
            self._append_log(f"Replay {index + 1} failed: {item.error}")
        elif kind == "item_stopped":
            item.status, item.stage = "Stopped", "Stopped. Partial files are kept."
            self._append_log(f"Replay {index + 1} stopped. Remaining links stay queued.")
        self._update_row(index)
        self._render_details()
        if self.batch_indices:
            total = sum(100 if self.items[i].status in ("Complete", "Warning", "Failed") else self.items[i].progress for i in self.batch_indices)
            self.batch_progress.setValue(int(total / len(self.batch_indices)))

    def _finish_batch(self, stopped: bool) -> None:
        self.is_running = False
        finished = [self.items[i] for i in self.batch_indices]
        complete = sum(item.status == "Complete" for item in finished)
        warnings = sum(item.status == "Warning" for item in finished)
        failed = sum(item.status == "Failed" for item in finished)
        if stopped:
            self._set_run_state("warning", "Stopped")
            message = f"Stopped · {complete} complete · Remaining links can be resumed"
        else:
            self.batch_progress.setValue(100)
            self._set_run_state("warning" if warnings or failed else "success", "Needs attention" if warnings or failed else "Complete")
            message = f"Queue finished · {complete} complete · {warnings} with warnings · {failed} failed"
        self.batch_status.setText(message)
        self._append_log(message)
        self._refresh_controls()
        self._render_details()

    def _request_stop(self) -> None:
        if not self.is_running or self.stop_requested.is_set():
            return
        self.stop_requested.set()
        self._set_run_state("warning", "Stopping")
        self.batch_status.setText("Stopping active work and cleaning up…")
        self._refresh_controls()
        with self.active_process_lock:
            process = self.active_process
        if process is not None:
            threading.Thread(target=_terminate_process_tree, args=(process,), daemon=True).start()

    def _append_log(self, message: str) -> None:
        for line in str(message).splitlines():
            if line.strip():
                stamp = datetime.now().strftime("%H:%M:%S")
                self.log_text.appendPlainText(f"{stamp}  {line}")
        scrollbar = self.log_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def closeEvent(self, event) -> None:
        if self.is_running or (self.worker and self.worker.is_alive()):
            self.closing = True
            self._request_stop()
            event.ignore()
            return
        self.cookie_text.clear()
        event.accept()

    def run(self) -> None:
        self.show()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setStyle("Fusion")
    window = WeverseChatStudio()
    window.run()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
