from __future__ import annotations

from pathlib import Path


TITLE_FILE_NAME = "title.txt"


def write_title_file(output_dir: str | Path, title: str) -> Path:
    title_path = Path(output_dir) / TITLE_FILE_NAME
    title_path.write_text(f"{title.strip()}\n", encoding="utf-8")
    return title_path
