# Weverse Live Downloader & Translator

This project is an under-development tool for scraping, downloading, and translating live videos from Weverse. More modularity and additional features are under active development.

## Features

- **weverse_scrape**: Scrapes an entire group's Weverse Live catalog and outputs a `video_links.txt` file containing all video links
- **weverse_dlt**: Downloads and translates videos from `video_links.txt`
- **weverse_chat_dump**: Dumps Weverse live/VOD chat to JSON for later subtitle rendering
- **weverse_chat_ui**: PySide6 desktop UI that downloads a VOD, dumps replay chat, generates a Twitch-style ASS overlay, and burns it into a final MP4 when `ffmpeg` is available

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

## Chat Dump Instructions

1. **Obtain Cookies**:  
    Log into Weverse, open the browser console, run:

    ```javascript
    document.cookie
    ```

    and keep the raw output ready to paste into the UI.

2. **Create and activate a Python 3.11 venv**:

    ```bash
    py -3.11 -m venv .venv
    .\.venv\Scripts\activate
    ```

3. **Install chat dump deps**:

    ```bash
    pip install -r requirements.txt
    ```

4. **Install `ffmpeg` and Nanum Gothic**:

    - Add `ffmpeg` to your `PATH` if you want the final burned-in video file.
    - Download and install Nanum Gothic from:
      <https://fonts.google.com/specimen/Nanum+Gothic>

5. **Launch the chat workflow UI**:

    ```bash
    python .\weverse_chat_ui.py
    ```

6. **Paste the cookie and URL, then run the workflow**:

    - Paste the raw `document.cookie` output into the cookie field.
    - Paste the Weverse live/VOD URL into the URL field.
    - Click **Run Workflow**.
    - The app will:
      - create a fresh output folder under `output/`
      - download the video with `yt-dlp`
      - open Chrome and dump replay chat to `weverse_chat.json`
      - convert the dump to `weverse_twitch_chat.ass`
      - render a burned-in `*_chat_burned.mp4` when `ffmpeg` is available
