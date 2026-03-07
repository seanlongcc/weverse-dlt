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
import traceback
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Callable

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
    from PySide6.QtCore import Qt, QTimer, QUrl
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import (
        QApplication,
        QHBoxLayout,
        QLabel,
        QLayout,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QPlainTextEdit,
        QPushButton,
        QSizePolicy,
        QTextEdit,
        QVBoxLayout,
        QWidget,
    )
except Exception as exc:
    print(_format_import_error(exc), file=sys.stderr)
    raise SystemExit(1) from exc


APP_TITLE = "Weverse Chat Syncer"
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
SPINNER_FRAMES = ("|", "/", "-", "\\")
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
    return cookie_text.replace("\r", "").replace("\n", "").strip()


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


def load_app_stylesheet() -> str:
    try:
        return APP_STYLE_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Could not read stylesheet: {APP_STYLE_PATH}") from exc


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
    result = capture_command(
        [
            sys.executable,
            "-m",
            "yt_dlp",
            "--dump-single-json",
            "--no-download",
            "--no-playlist",
            "--add-header",
            f"Cookie: {cookie_text}",
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
            "--add-header",
            f"Cookie: {cookie_text}",
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
) -> Path | None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        logger("ffmpeg was not found on PATH. Skipping burned-in video output.")
        return None

    output_path = video_path.with_name(f"{video_path.stem}_chat_burned.mp4")
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

    warnings: list[str] = []
    cookie_path = output_dir / ".weverse_cookie.txt"
    cookie_path.write_text(cookie_text + "\n", encoding="utf-8")

    logger(f"Output folder: {output_dir}")

    try:
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
            "chat_json": chat_json,
            "ass_path": ass_path,
            "burned_path": burned_path,
            "warnings": warnings,
        }
    finally:
        if cookie_path.exists():
            cookie_path.unlink()


class WeverseChatStudio(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1100, 690)
        self.setMinimumSize(1100, 690)

        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.output_dir: Path | None = None
        self.is_running = False
        self.stop_requested = threading.Event()
        self.active_process: subprocess.Popen[str] | None = None
        self.active_process_lock = threading.Lock()
        self.artifact_bullets: dict[str, QLabel] = {}
        self.artifact_spinners: dict[str, QLabel] = {}
        self.active_artifact_key: str | None = None
        self.spinner_frame_index = 0

        self._build_ui()
        self._set_run_state("idle", "Idle")

        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(120)
        self.poll_timer.timeout.connect(self._poll_events)
        self.poll_timer.start()

        self.spinner_timer = QTimer(self)
        self.spinner_timer.setInterval(120)
        self.spinner_timer.timeout.connect(self._advance_active_spinner)

    def _build_card(self, title: str, subtitle: str | None, parent: QWidget) -> tuple[QWidget, QVBoxLayout]:
        card = QWidget(parent)
        card.setObjectName("card")

        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(4)

        title_label = QLabel(title, card)
        title_label.setObjectName("cardTitle")
        title_label.setProperty("hasSubtitle", subtitle is not None)

        layout.addWidget(title_label)
        if subtitle:
            subtitle_label = QLabel(subtitle, card)
            subtitle_label.setObjectName("cardSubtitle")
            subtitle_label.setWordWrap(True)
            layout.addWidget(subtitle_label)
        return card, layout

    def _build_ui(self) -> None:
        page = QWidget(self)
        page.setObjectName("page")
        self.setCentralWidget(page)

        root = QVBoxLayout(page)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(14)

        hero = QWidget(page)
        hero.setObjectName("hero")
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(16, 12, 16, 12)
        hero_layout.setSpacing(14)

        hero_text = QVBoxLayout()
        hero_text.setSpacing(0)
        hero_title = QLabel(APP_TITLE, hero)
        hero_title.setObjectName("heroTitle")
        hero_text.addWidget(hero_title)

        self.run_state_chip = QLabel("Idle", hero)
        self.run_state_chip.setObjectName("runStateChip")
        self.run_state_chip.setAlignment(Qt.AlignCenter)
        self.run_state_chip.setMinimumWidth(140)

        hero_layout.addLayout(hero_text, 1)
        hero_layout.addWidget(self.run_state_chip, 0, Qt.AlignRight | Qt.AlignTop)

        content = QHBoxLayout()
        content.setSpacing(16)

        left_column = QWidget(page)
        left_layout = QVBoxLayout(left_column)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(16)

        right_column = QWidget(page)
        right_layout = QVBoxLayout(right_column)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(16)

        setup_card, setup_layout = self._build_card(
            "Session Setup",
            "Log into Weverse, run <code>document.cookie</code> in the browser console, then paste the raw output below.",
            left_column,
        )
        setup_layout.setSpacing(4)
        setup_layout.setSizeConstraint(QLayout.SetMinimumSize)

        fields_wrap = QWidget(setup_card)
        fields_layout = QVBoxLayout(fields_wrap)
        fields_layout.setContentsMargins(0, 0, 0, 12)
        fields_layout.setSpacing(14)
        fields_layout.setSizeConstraint(QLayout.SetMinimumSize)

        cookie_block = QWidget(fields_wrap)
        cookie_block_layout = QVBoxLayout(cookie_block)
        cookie_block_layout.setContentsMargins(0, 0, 0, 0)
        cookie_block_layout.setSpacing(6)

        cookie_label = QLabel("Access Cookie", cookie_block)
        cookie_label.setObjectName("fieldLabel")
        self.cookie_text = QTextEdit(cookie_block)
        self.cookie_text.document().setDocumentMargin(9)
        cookie_height = (
            self.cookie_text.fontMetrics().lineSpacing() * 3
            + self.cookie_text.document().documentMargin() * 2
            + self.cookie_text.frameWidth() * 2
        )
        self.cookie_text.setFixedHeight(max(104, int(cookie_height)))
        self.cookie_text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.cookie_text.setLineWrapMode(QTextEdit.WidgetWidth)
        self.cookie_text.setPlaceholderText("Paste the full browser cookie string")
        cookie_block_layout.addWidget(cookie_label)
        cookie_block_layout.addWidget(self.cookie_text)

        url_block = QWidget(fields_wrap)
        url_block_layout = QVBoxLayout(url_block)
        url_block_layout.setContentsMargins(0, 0, 0, 0)
        url_block_layout.setSpacing(6)

        url_label = QLabel("Weverse URL", url_block)
        url_label.setObjectName("fieldLabel")
        self.url_entry = QLineEdit(url_block)
        self.url_entry.setFixedHeight(38)
        self.url_entry.setPlaceholderText("https://weverse.io/.../live/...")
        url_block_layout.addWidget(url_label)
        url_block_layout.addWidget(self.url_entry)

        self.start_button = QPushButton("Run", setup_card)
        self.start_button.clicked.connect(self._start_workflow)
        self.clear_button = QPushButton("Clear", setup_card)
        self.clear_button.clicked.connect(self._clear_inputs)
        self.stop_button = QPushButton("Stop", setup_card)
        self.stop_button.setObjectName("stopButton")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._request_stop)
        self.open_folder_button = QPushButton("Open Folder", setup_card)
        self.open_folder_button.setEnabled(False)
        self.open_folder_button.clicked.connect(self._open_output_folder)

        actions_row = QHBoxLayout()
        actions_row.setContentsMargins(0, 0, 0, 0)
        actions_row.setSpacing(8)
        actions_row.addWidget(self.start_button, 0)
        actions_row.addWidget(self.clear_button, 0)
        actions_row.addWidget(self.stop_button, 0)
        actions_row.addWidget(self.open_folder_button, 0)
        actions_row.addStretch(1)

        fields_layout.addWidget(cookie_block)
        fields_layout.addWidget(url_block)

        setup_layout.addWidget(fields_wrap)
        setup_layout.addSpacing(8)
        setup_layout.addLayout(actions_row)
        setup_layout.addStretch(1)

        outputs_card, outputs_layout = self._build_card(
            "Output Status",
            None,
            left_column,
        )
        outputs_layout.setSizeConstraint(QLayout.SetMinimumSize)
        outputs_card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        artifact_rows = [
            ("Workspace folder initialized", "folder"),
            ("Source video downloaded", "video"),
            ("Replay chat exported (JSON)", "chat_json"),
            ("Chat overlay rendered (ASS)", "ass"),
            ("Final burn-in video exported (MP4)", "burned"),
        ]
        for label_text, key in artifact_rows:
            row = QWidget(outputs_card)
            row.setObjectName("artifactRow")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 4, 0, 4)
            row_layout.setSpacing(10)

            bullet = QLabel("●", row)
            bullet.setObjectName("artifactBullet")
            bullet.setProperty("done", False)

            text = QLabel(label_text, row)
            text.setObjectName("artifactText")

            spinner = QLabel("", row)
            spinner.setObjectName("artifactSpinner")
            spinner.setAlignment(Qt.AlignCenter)
            spinner.setFixedWidth(18)
            spinner.setProperty("active", False)

            row_layout.addWidget(bullet, 0, Qt.AlignVCenter)
            row_layout.addWidget(text, 1)
            row_layout.addWidget(spinner, 0, Qt.AlignVCenter)
            outputs_layout.addWidget(row)

            self.artifact_bullets[key] = bullet
            self.artifact_spinners[key] = spinner

        left_layout.addWidget(setup_card, 0)
        left_layout.addWidget(outputs_card, 0)
        left_layout.addStretch(1)

        log_card, log_layout = self._build_card(
            "Execution Feed",
            None,
            right_column,
        )
        log_layout.setSizeConstraint(QLayout.SetMinimumSize)
        log_card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.log_text = QPlainTextEdit(log_card)
        self.log_text.setReadOnly(True)
        self.log_text.document().setDocumentMargin(9)
        self.log_text.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        log_height = (
            self.log_text.fontMetrics().lineSpacing() * 8
            + self.log_text.document().documentMargin() * 2
            + self.log_text.frameWidth() * 2
        )
        self.log_text.setMinimumHeight(max(168, int(log_height)))
        self.log_text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        log_layout.addWidget(self.log_text)

        right_layout.addWidget(log_card, 1)

        content.addWidget(left_column, 11)
        content.addWidget(right_column, 14)

        root.addWidget(hero, 0)
        root.addLayout(content, 1)

    def _set_run_state(self, state: str, text: str) -> None:
        self.run_state_chip.setProperty("state", state)
        self.run_state_chip.setText(text)
        self.run_state_chip.style().unpolish(self.run_state_chip)
        self.run_state_chip.style().polish(self.run_state_chip)

    def _set_active_process(self, process: subprocess.Popen[str] | None) -> None:
        with self.active_process_lock:
            self.active_process = process

    def _terminate_active_process(self) -> None:
        with self.active_process_lock:
            process = self.active_process
        if process is not None:
            _terminate_process_tree(process)

    def _start_workflow(self) -> None:
        if self.worker and self.worker.is_alive():
            return

        cookie_text = self.cookie_text.toPlainText().strip()
        url = self.url_entry.text().strip()

        if not cookie_text:
            QMessageBox.critical(self, "Missing cookie", "Paste your Weverse cookie before launching the pipeline.")
            return

        if not url.startswith("http://") and not url.startswith("https://"):
            QMessageBox.critical(self, "Invalid URL", "Enter a full replay URL starting with http:// or https://.")
            return

        self.stop_requested.clear()
        self._set_active_process(None)
        self.output_dir = None
        self.open_folder_button.setEnabled(False)
        self._set_run_state("running", "Running")
        self._set_active_artifact(None)
        for key in self.artifact_bullets:
            self._set_artifact_state(key, False)

        self.log_text.clear()
        self._append_log("Launching a new processing run.")
        self._set_running(True)

        self.worker = threading.Thread(
            target=self._run_worker,
            args=(cookie_text, url),
            daemon=True,
        )
        self.worker.start()

    def _run_worker(self, cookie_text: str, url: str) -> None:
        emit = lambda kind, payload: self.events.put((kind, payload))
        logger = lambda message: emit("log", message)
        status = lambda value: emit("status", value)
        progress = lambda value: None
        artifact = lambda key, value: emit("artifact", (key, value))
        output_dir = lambda value: emit("output_dir", str(value))

        try:
            result = run_workflow(
                cookie_text,
                url,
                logger,
                status,
                progress,
                artifact,
                set_output_dir=output_dir,
                on_process=self._set_active_process,
                cancel_requested=self.stop_requested.is_set,
            )
        except WorkflowCancelled as exc:
            self._set_active_process(None)
            emit("cancelled", str(exc))
            return
        except Exception as exc:
            self._set_active_process(None)
            emit("log", traceback.format_exc().rstrip())
            emit("error", str(exc))
            return

        self._set_active_process(None)
        emit("success", result)

    def _poll_events(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break

            if kind == "log":
                self._append_log(str(payload))
            elif kind == "status":
                self._set_active_artifact(STATUS_TO_ARTIFACT.get(str(payload)))
            elif kind == "output_dir":
                self.output_dir = Path(str(payload))
            elif kind == "artifact":
                key, value = payload
                self._set_artifact_state(str(key), bool(value))
            elif kind == "error":
                self._handle_error(str(payload))
            elif kind == "cancelled":
                self._handle_cancelled(str(payload))
            elif kind == "success":
                self._handle_success(payload)

    def _set_artifact_state(self, key: str, done: bool) -> None:
        bullet = self.artifact_bullets.get(key)
        if bullet is None:
            return
        if key == self.active_artifact_key:
            self._set_active_artifact(None)
        bullet.setProperty("done", bool(done))
        bullet.style().unpolish(bullet)
        bullet.style().polish(bullet)

    def _set_active_artifact(self, key: str | None) -> None:
        normalized_key = key if key in self.artifact_spinners else None
        if normalized_key == self.active_artifact_key:
            if normalized_key is not None:
                self.spinner_frame_index = 0
                self.artifact_spinners[normalized_key].setText(SPINNER_FRAMES[self.spinner_frame_index])
            return

        if self.active_artifact_key is not None:
            previous = self.artifact_spinners[self.active_artifact_key]
            previous.clear()
            previous.setProperty("active", False)
            previous.style().unpolish(previous)
            previous.style().polish(previous)

        self.active_artifact_key = normalized_key
        self.spinner_frame_index = 0

        if self.active_artifact_key is None:
            self.spinner_timer.stop()
            return

        current = self.artifact_spinners[self.active_artifact_key]
        current.setText(SPINNER_FRAMES[self.spinner_frame_index])
        current.setProperty("active", True)
        current.style().unpolish(current)
        current.style().polish(current)
        if not self.spinner_timer.isActive():
            self.spinner_timer.start()

    def _advance_active_spinner(self) -> None:
        if self.active_artifact_key is None:
            return
        self.spinner_frame_index = (self.spinner_frame_index + 1) % len(SPINNER_FRAMES)
        self.artifact_spinners[self.active_artifact_key].setText(SPINNER_FRAMES[self.spinner_frame_index])

    def _handle_success(self, payload: object) -> None:
        assert isinstance(payload, dict)

        self._set_running(False)
        self.stop_requested.clear()
        self._set_active_artifact(None)
        self.output_dir = Path(str(payload["output_dir"]))
        self.open_folder_button.setEnabled(self.output_dir.exists())

        warnings = payload.get("warnings") or []
        if warnings:
            self._set_run_state("warning", "Warnings")
            for warning in warnings:
                self._append_log(f"Warning: {warning}")
            QMessageBox.warning(
                self,
                "Pipeline completed with warnings",
                "\n".join(str(item) for item in warnings),
            )
        else:
            self._set_run_state("success", "Completed")
            QMessageBox.information(
                self,
                "Pipeline completed",
                f"Artifacts were written to:\n{payload['output_dir']}",
            )

    def _handle_error(self, message: str) -> None:
        self._set_running(False)
        self.stop_requested.clear()
        self._set_active_artifact(None)
        self.open_folder_button.setEnabled(False)
        self._set_run_state("error", "Failed")
        QMessageBox.critical(self, "Pipeline failed", message)

    def _handle_cancelled(self, message: str) -> None:
        self._set_running(False)
        self.stop_requested.clear()
        self._set_active_artifact(None)
        self._set_run_state("warning", "Stopped")

        if self.output_dir is not None and self.output_dir.exists():
            self.open_folder_button.setEnabled(True)
            QMessageBox.information(
                self,
                "Pipeline stopped",
                f"{message}\n\nPartial artifacts are in:\n{self.output_dir}",
            )
            return

        self.open_folder_button.setEnabled(False)
        QMessageBox.information(self, "Pipeline stopped", message)

    def _request_stop(self) -> None:
        if not self.is_running or self.stop_requested.is_set():
            return

        self.stop_requested.set()
        self._set_run_state("warning", "Stopping")
        self.stop_button.setText("Stopping...")
        self.stop_button.setEnabled(False)
        self._append_log("Stop requested. Terminating the active step...")
        self._terminate_active_process()

    def _clear_inputs(self) -> None:
        if self.is_running:
            return
        self.cookie_text.clear()
        self.url_entry.clear()

    def _open_output_folder(self) -> None:
        if self.output_dir is None or not self.output_dir.exists():
            self.open_folder_button.setEnabled(False)
            QMessageBox.warning(self, "Output folder unavailable", "No completed output folder is available yet.")
            return

        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.output_dir))):
            QMessageBox.critical(
                self,
                "Unable to open folder",
                f"Could not open:\n{self.output_dir}",
            )

    def _set_running(self, running: bool) -> None:
        self.is_running = running
        self.start_button.setEnabled(not running)
        self.start_button.setText("Running..." if running else "Run")
        self.clear_button.setEnabled(not running)
        self.stop_button.setEnabled(running and not self.stop_requested.is_set())
        self.stop_button.setText("Stopping..." if running and self.stop_requested.is_set() else "Stop")
        self.open_folder_button.setEnabled(
            (not running)
            and self.output_dir is not None
            and self.output_dir.exists()
        )
        self.url_entry.setEnabled(not running)
        self.cookie_text.setReadOnly(running)

    def _append_log(self, message: str) -> None:
        for line in str(message).splitlines():
            if not line.strip():
                continue
            stamp = datetime.now().strftime("%H:%M:%S")
            self.log_text.appendPlainText(f"[{stamp}] {line}")
        scrollbar = self.log_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def run(self) -> None:
        self.show()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setStyleSheet(load_app_stylesheet())
    window = WeverseChatStudio()
    window.run()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
