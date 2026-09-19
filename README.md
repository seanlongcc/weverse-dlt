# Weverse Live Processor

This project is an under-development tool for scraping, downloading, and translating live videos from Weverse. More modularity and additional features are under active development.

## Features

- **weverse_scrape**: Scrapes an entire group's Weverse Live catalog and outputs a `video_links.txt` file containing all video links
- **weverse_dlt**: Downloads and translates videos from `video_links.txt`
- **weverse_chat_dump**: Dumps Weverse live/VOD chat to JSON for later subtitle rendering
- **weverse_chat_ui**: PySide6 desktop app with Midnight and Paper appearances, automatic browser-session import, and a queue for multiple replay links. Downloads each video, exports replay chat, generates a Twitch-style ASS overlay, and burns it into a final MP4 when `ffmpeg` is available.

## Requirements

- Python 3.11
- [WhisperX](https://github.com/m-bain/whisperX) (follow installation instructions in the repository)

## Setup & Usage

Note: This workflow is outdated.

1. **Clone the Repository**:

    ```bash
    git clone https://github.com/seanlongcc/weverse-dlt.git
    cd weverse-dlt
    ```

2. **Obtain Cookies**:  
    Log into Weverse, open the browser console, run:

    ```javascript
    document.cookie
    ```

    and save the output to a file named `cookie.txt` in the repository root.

3. **Scrape Video Links**:  
    Run the scraper to generate `video_links.txt`:

    ```bash
    python scripts/weverse_scrape.py cookie.txt https://weverse.io/stayc/live
    ```

    Replace `https://weverse.io/stayc/live` with your target URL if needed.

4. **Download & Translate Videos**:  
    Run the downloader/translator using your cookie file and the generated links file:

    ```bash
    python scripts/weverse_dlt.py cookie.txt video_links.txt
    ```

## Video Subtitle Translation Workflow

1. **Create a native transcript**:

    ```bash
    conda run -n whisperx_env whisperx --language ko --model large-v3 --compute_type float32 --output_format srt --chunk_size 5 --no_align "FILE_PATH"
    ```

2. **Translate with the Custom GPT**:  
    Upload the transcript to [STAYC SRT Translator](https://chatgpt.com/g/g-689a0b1e6d888191813ceb489dd0dba4-stayc-srt-translator).

## Desktop replay workflow

1. **Create and activate a Python 3.11 environment** (or use your existing environment):

    ```powershell
    py -3.11 -m venv .venv
    .\.venv\Scripts\activate
    python -m pip install -r requirements.txt
    ```

2. **Install Chrome, ffmpeg, and Nanum Gothic**:

    - Chrome is required for replay-chat collection and the sign-in helper.
    - Add `ffmpeg` to `PATH` for the final video with chat burned in. Without it, the source video, JSON, and ASS files are still produced.
    - `ffprobe`, normally included with ffmpeg, supplies video dimensions.
    - Install [Nanum Gothic](https://fonts.google.com/specimen/Nanum+Gothic) for the intended overlay font.
    - Color chat burn-in requires PySide6 6.9 or later (included in `requirements.txt`). It uses native color emoji fonts: Segoe UI Emoji on Windows, Apple Color Emoji on macOS, or Noto Color Emoji on Linux. Install Noto Color Emoji on Linux if emoji are missing. Available emoji designs and sequences depend on the installed font; for example, Segoe UI Emoji displays many flags as country letters.

3. **Launch Weverse Live Processor**:

    ```powershell
    .\.venv\Scripts\python.exe .\weverse_chat_ui.py
    ```

    On Linux, use your Linux environment's `python weverse_chat_ui.py`. Windows and Linux virtual environments are not interchangeable.

4. **Connect your Weverse session** using either option:

    - **Import browser session** reads your existing Weverse login from Zen, Chrome, Edge, Firefox, or Brave. Zen is selected by default; the app remembers your browser choice. Log into Weverse in that browser first. Under **Manual cookie & profile**, optionally select a profile name or a full profile path. Leave it blank for automatic profile selection.
    - **Zen profiles** are detected in the standard Windows, macOS, Linux, and Linux Flatpak locations. Import uses yt-dlp's Firefox cookie reader with the resolved Zen profile. The installation's default profile takes priority, followed by the profile marked as default, then the most recently updated cookie database. For another profile or a portable installation, paste its Profile Directory from [Zen's `about:support` page](https://docs.zen-browser.app/user-manual/window-sync) into **Browser profile**. Zen provides the session cookies; replay-chat collection and the optional sign-in helper still use Chrome.
    - **Sign in with Chrome** opens a separate, temporary Chrome window. Sign into Weverse there, completing any login prompts yourself. The app detects the Weverse access-token cookie automatically and closes this window. You have five minutes to finish signing in.

    Direct import can fail when browser cookies are locked or cannot be decrypted, especially with Windows Chromium encryption. Close the selected browser and retry, or use **Sign in with Chrome**. If the sign-in page rejects browser automation, the manual option remains available. See [yt-dlp's cookie FAQ](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp) and [Windows decryption issue](https://github.com/yt-dlp/yt-dlp/issues/10927).

    **Manual cookie** still accepts your full raw `document.cookie` output or a `Cookie:` header. The field hides its contents. Session import confirms that a login cookie was found; access to a particular replay is checked when it is processed. Reconnect if your session expires.

    Cookies are not saved in app settings or logs. Temporary cookie files are removed after use, including on handled failure, Stop, and normal app closure. The sign-in browser uses a temporary profile. **Disconnect** clears the in-memory session. The app never asks for your password directly.

5. **Add replays and start the queue**:

    - Paste one or several full Weverse `/live/…` or `/media/…` links, separated by newlines or whitespace, then click **Add to queue**. **Start queue** also adds any links still in the editor.
    - **Import .txt** accepts a UTF-8 links file, including files created by `weverse_scrape.py`.
    - Duplicate replay links are skipped, including copies with different share query parameters. Invalid input is rejected as a whole so it can be corrected before adding.
    - Click **Start queue**. Replays run sequentially. Each gets its own folder under `output/`, containing `title.txt`, the source video, `weverse_chat.json`, `weverse_twitch_chat.ass`, and `*_chat_burned.mp4` when rendering succeeds.
    - Burned chat has stable colors for viewer names, white message text, and native color emoji. Text is measured before wrapping, keeping joined emoji and skin tones together. The MP4 uses transparent snapshots rendered from the chat JSON; temporary snapshots are removed after rendering, errors, or cancellation. The separate ASS overlay keeps colored names, but emoji rendering in ASS depends on the subtitle player and may remain monochrome.
    - Inspect each row's status, select it to see output stages, and click **Open selected folder** or the row's **Open folder** cell. Partial files remain accessible after errors or cancellation.
    - A failed replay does not block later links. **Stop** cancels active work; links that have not started stay queued. **Start queue** runs those remaining links. **Retry unfinished** also queues failed, stopped, and warning items for a fresh run in a new folder; it does not resume a partial download in place.
    - **Remove selected** removes a row. **Clear all** clears the queue, input links, and session. These actions leave downloaded files on disk. Queue entries and credentials are not restored after restarting the app.

6. **Choose an appearance**:

    Use the single **theme button** in the header to switch between **Midnight**, the default charcoal and mint workspace, and **Paper**, with warm light surfaces and forest green. The icon and tooltip show the theme the next click will switch to: sun for light, moon for dark. The app remembers your choice. Both appearances have the same features. The main sections are **1. Session**, **2. Queue**, and **3. Outputs**. Panels scroll at smaller window sizes, and **Activity log** can be collapsed for more queue space.

### Development checks

```powershell
.\.venv\Scripts\python.exe -m pip install pytest
.\.venv\Scripts\python.exe -m pytest -q tests
```

Tests use synthetic cookies and replay data, native Qt controls in offscreen mode, and a short real ffmpeg render when ffmpeg is installed. Browser login and authenticated downloads require a real user session and are not exercised by the automated suite.
