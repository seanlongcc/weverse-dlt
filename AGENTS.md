# Repository guidance

## Working style

- Keep code, commands, commit messages, documentation, and review findings precise and in normal technical language.
- Treat requests for action as authorization to complete the work within the requested scope. Make reasonable assumptions for routine, reversible choices and continue through implementation and relevant verification.
- Ask for clarification only when missing information materially changes the outcome and cannot be inferred from the repository or conversation. Continue independent work while waiting; do not ask again for authorization already given.
- If approval is needed, prepare the concrete, reviewable result first. Explain the specific action and why approval is required.
- Explicit user instructions take precedence over this file and skill guidance, subject to system and developer instructions. If a local instruction blocks work or conflicts with the request, identify the file and relevant instruction rather than silently stopping or changing scope.
- Keep updates brief and useful. The final response should state what changed, what was verified, and any remaining limitation. Do not claim checks or live workflows succeeded unless they were run.
- When delegation is authorized, assign bounded, independent work with clear file ownership and verify the combined result.

## Project map

This is a Python 3.11 desktop and command-line project for downloading Weverse videos and producing subtitles and replay-chat overlays.

- `weverse_chat_ui.py`: PySide6 UI and workflow orchestration, subprocess handling, progress, cancellation, and output artifacts.
- `styles/weverse_chat_ui.qss`: desktop UI styling.
- `scripts/weverse_chat_dump.py`: Selenium Wire replay-chat collection and JSON output.
- `scripts/weverse_chat_to_ass_twitch.py`: chat JSON conversion into Twitch-style ASS subtitles.
- `scripts/weverse_scrape.py`: group live-catalog scraping into a links file.
- `scripts/weverse_dlt.py`: older batch download and WhisperX translation workflow.
- `scripts/weverse_output.py`: shared output helpers.
- `tests/`: pytest tests.

The README labels the older setup workflow as outdated. Check the relevant script's current arguments and implementation before changing or documenting its behavior.

## Setup and commands

Run commands from the repository root using the project's Python environment. Use an existing environment when available. A Windows virtual environment and a Linux/WSL virtual environment have different interpreters; do not assume they are interchangeable.

With the intended environment activated:

```sh
python -m pip install -r requirements.txt
python weverse_chat_ui.py
```

On Windows, the UI can also be launched directly with `.\.venv\Scripts\python.exe .\weverse_chat_ui.py`.

Chrome is required for scraping and chat collection. `ffmpeg` enables video burn-in, `ffprobe` supplies video dimensions when available, and Nanum Gothic is the intended overlay font. WhisperX runs in the separate `whisperx_env` conda environment used by the older translation workflow.

Tests require pytest, which is not included in `requirements.txt`. If needed, install it into the development environment with `python -m pip install pytest`, then run the relevant test file or the small suite:

```sh
python -m pytest -q tests
```

There is no configured build, linter, or type-check command. Do not invent mandatory checks or introduce tooling solely to validate an unrelated change.

## Implementation constraints

- Inspect `git status` and the affected code before editing. Preserve existing user changes and keep edits focused on the requested behavior.
- Follow nearby Python conventions. Preserve Korean text and emoji with explicit UTF-8 file I/O; keep subtitle timing, escaping, and output naming compatible unless the task changes those contracts.
- Keep long-running downloads, browser operations, and rendering off the UI thread. Preserve cancellation, process cleanup, and temporary-cookie cleanup when changing workflow code.
- Prefer subprocess argument lists and `sys.executable` for Python child processes. Account for Windows paths and ffmpeg filter escaping; do not interpolate untrusted titles, URLs, or cookies into shell commands.
- Treat cookies as credentials. Do not print their contents or commit cookie files, private chat dumps, or downloaded media. Use synthetic data for tests where possible.

## Verification and completion

- Match verification to the change. For documentation-only edits, check accuracy, paths, commands, and whitespace; application tests are unnecessary.
- For behavior changes, run the relevant tests and add regression coverage when it catches a meaningful failure. Avoid tests that only restate implementation details.
- Use a small synthetic chat fixture for subtitle-rendering checks. For UI changes, check the affected interaction when a graphical session is available.
- Live scraping, downloads, WhisperX, and video rendering need their external dependencies and suitable inputs. Use those checks when relevant to the task; report what could not be exercised and why.
- After appropriate checks pass, inspect the final diff for accidental changes and finish. Broaden or repeat checks only when new evidence warrants it.

Guidance references: [OpenAI Codex best practices](https://learn.chatgpt.com/guides/best-practices), [AGENTS.md discovery](https://learn.chatgpt.com/docs/agent-configuration/agents-md), and [GPT-6 Astra prompting guidance](https://developers.openai.com/api/docs/guides/latest-model?model=gpt-6-astra#prompting-best-practices).
