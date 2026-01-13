import os
import sys
import re
import subprocess
from datetime import datetime

from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium import webdriver


def load_cookies_from_txt(driver, cookie_file):
    """
    Loads cookies from a text file and adds them to the current driver session.
    The text file should contain a single cookie string with cookies separated by semicolons.
    """
    with open(cookie_file, "r", encoding="utf-8") as f:
        cookie_str = f.read().strip()
    cookies = cookie_str.split(";")
    for cookie in cookies:
        cookie = cookie.strip()
        if not cookie:
            continue
        parts = cookie.split("=", 1)
        if len(parts) != 2:
            continue
        name, value = parts
        cookie_dict = {"name": name, "value": value, "domain": "weverse.io"}
        try:
            driver.add_cookie(cookie_dict)
        except Exception as e:
            print(f"Could not add cookie {cookie_dict}: {e}")


def extract_video_info(url, cookie_file):
    """
    Logs in using cookies, navigates to the video page,
    and extracts the artist's name, group, date, and title information.
    """
    options = Options()
    options.add_argument("--headless")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
    )
    driver = webdriver.Chrome(options=options)

    try:
        driver.get("https://weverse.io/")
        load_cookies_from_txt(driver, cookie_file)
        driver.refresh()

        driver.get(url)
        wait = WebDriverWait(driver, 30)

        artist_elem = wait.until(EC.presence_of_element_located(
            (By.CSS_SELECTOR, ".LiveArtistProfileView_artist_wrap__nOs54 ul.LiveArtistProfileView_name_list__DDCHd li.LiveArtistProfileView_name_item__8W66y")
        ))
        artist_text = artist_elem.text.strip()

        wait.until(EC.presence_of_element_located(
            (By.CLASS_NAME, "LiveArtistProfileView_info__dICbs")
        ))
        info_elements = driver.find_elements(By.CLASS_NAME, "LiveArtistProfileView_info__dICbs")
        if len(info_elements) < 2:
            print("Error: Could not find both group and date information.")
            sys.exit(1)
        group_text = info_elements[0].text.strip()
        date_text = info_elements[1].text.strip()

        # Extract the title element and clean it up by removing "replay"
        title_elem = wait.until(EC.presence_of_element_located(
            (By.CSS_SELECTOR, "h2.TitleView_title__SSnHb.TitleView_-color_white__6PV8I")
        ))
        video_title = title_elem.text.strip()
        video_title = re.sub(r'\breplay\b', '', video_title, flags=re.IGNORECASE).strip()

        return artist_text, group_text, date_text, video_title
    finally:
        driver.quit()


def format_date(date_str):
    """
    Converts a date string like "Nov 9, 2024, 03:22" or "Feb 23, 01:38" into a formatted string "YYMMDD_HHMM".
    If the year is missing, the current year is assumed.
    """
    try:
        dt = datetime.strptime(date_str, "%b %d, %Y, %H:%M")
    except ValueError:
        current_year = datetime.now().year
        new_date_str = f"{date_str}, {current_year}"
        dt = datetime.strptime(new_date_str, "%b %d, %H:%M, %Y")
    return dt.strftime("%y%m%d_%H%M")


def process_video(video_url, cookie_file):
    print("\nProcessing video:", video_url)
    # Extract info from the video page.
    artist_text, group_text, date_text, video_title = extract_video_info(video_url, cookie_file)

    # Map artist names (or emojis) to desired shorthand.
    artist_map = {
        "STAYC": "STAYC",
        "장재이😝": "J",
        "청숨": "Sumin",
        "박뭐든가능시은🖤": "Sieun",
        "이사님🖤": "Isa",
        "자유니💕": "Yoon",
        "세으니🌷": "Seeun"
    }
    group_member = artist_map.get(artist_text, artist_text[0] if artist_text else "UNK")
    formatted_date = format_date(date_text)

    # Build the folder name.
    if group_member == "STAYC":
        folder_name = f"[ENG SUB] {formatted_date} STAYC Weverse LIVE"
    else:
        folder_name = f"[ENG SUB] {formatted_date} STAYC {group_member} Weverse LIVE"

    # Initial file name (base name)
    base_file_name = f"{folder_name}.mp4"

    if not os.path.exists(folder_name):
        os.makedirs(folder_name)

    output_path = os.path.join(folder_name, base_file_name)

    # Check for duplicate file in the same directory.
    if os.path.exists(output_path):
        current_time = datetime.now().strftime("%H%M%S")
        # Append the current time to the base file name to avoid duplicates.
        base_file_name = f"{folder_name}_{current_time}.mp4"
        output_path = os.path.join(folder_name, base_file_name)
        print(f"Duplicate found. New file name: {base_file_name}")

    print("Download parameters:")
    print(f"  Artist: {artist_text}")
    print(f"  Date: {date_text} (formatted: {formatted_date})")
    print(f"  Title: {video_title}")
    print(f"  Output Folder: {folder_name}")
    print(f"  Output File: {base_file_name}")
    
    download_command = [
        "yt-dlp",
        "-f", "best",
        "-o", output_path,
        "--recode-video", "mp4",
        video_url
    ]
    print("Executing command:", " ".join(download_command))

    download_result = subprocess.run(download_command)
    if download_result.returncode == 0:
        print("Download completed successfully!")
        print(f"File saved as: {output_path}")
        print("Starting translation using WhisperX in the 'whisperx' conda environment...")

        # Execute the translation command.
        translation_command = (
            f'conda run -n whisperx_env whisperx --language ko --task translate --model large-v3 '
            f'--output_format srt --compute_type float32 --output_dir "{folder_name}" --chunk_size 5 "{output_path}"'
        )
        print("Executing translation command:", translation_command)
        translation_result = subprocess.run(translation_command, shell=True)
        if translation_result.returncode == 0:
            # Derive subtitle file name from the video file name.
            srt_filename = base_file_name.replace(".mp4", ".srt")
            subtitle_path = os.path.join(folder_name, srt_filename)
            if os.path.exists(subtitle_path):
                print(f"Subtitle file saved to: {subtitle_path}")
            else:
                print("Subtitle file not found in the specified folder.")
        else:
            print("Translation failed.")

        # Write the video title to a title file named based on the video file name.
        title_file_name = base_file_name.replace(".mp4", "_title.txt")
        title_file_path = os.path.join(folder_name, title_file_name)
        try:
            with open(title_file_path, "w", encoding="utf-8") as tf:
                tf.write(video_title)
            print(f"Title written to: {title_file_path}")
        except Exception as e:
            print(f"Failed to write title file: {e}")
    else:
        print("Download failed. Please check the video URL and your yt-dlp installation.")


def main():
    if len(sys.argv) != 3:
        print("Usage: python download_and_translate.py <cookie_file> <links_file>")
        sys.exit(1)

    cookie_file = sys.argv[1]
    links_file = sys.argv[2]

    if not os.path.exists(links_file):
        print(f"Links file '{links_file}' not found.")
        sys.exit(1)

    with open(links_file, "r", encoding="utf-8") as f:
        links = [line.strip() for line in f if line.strip()]

    if not links:
        print("No video links found in the file.")
        sys.exit(1)

    for video_url in links:
        process_video(video_url, cookie_file)


if __name__ == "__main__":
    main()
