# Weverse Live Downloader & Translator

This project is an under-development tool for scraping, downloading, and translating live videos from Weverse. More modularity and additional features are under active development.

## Features

- **weverse_scrape**: Scrapes an entire group's Weverse Live catalog and outputs a `video_links.txt` file containing all video links
- **weverse_dlt**: Downloads and translates videos from `video_links.txt`

## Requirements

- Python 3.10 or greater
- [WhisperX](https://github.com/m-bain/whisperX) (follow installation instructions in the repository)

## Setup & Usage

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
    python weverse_video_links.py cookie.txt https://weverse.io/stayc/live
    ```

    Replace `https://weverse.io/stayc/live` with your target URL if needed.

4. **Download & Translate Videos**:  
    Run the downloader/translator using your cookie file and the generated links file:

    ```bash
    python weverse_dlt.py cookie.txt video_links.txt
    ```
