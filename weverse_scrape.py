import os
import sys
import time
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium import webdriver


def load_cookies_from_txt(driver, cookie_file):
    """
    Reads cookies from a text file (a single semicolon-separated line)
    and adds them to the current driver session.
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
        # For this example, we assume the cookies belong to "weverse.io"
        cookie_dict = {"name": name, "value": value, "domain": "weverse.io"}
        try:
            driver.add_cookie(cookie_dict)
        except Exception as e:
            print(f"Could not add cookie {cookie_dict}: {e}")


def get_video_links(target_url, cookie_file, scroll_pause_time=2, headless=True):
    """
    Opens the target URL after loading cookies and scrolls to load all video items.
    Returns a list of video links based on the CSS selector.
    """
    options = Options()
    if headless:
        options.add_argument("--headless")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
    )
    driver = webdriver.Chrome(options=options)
    try:
        # Open the base domain so cookies can be added.
        base_url = "https://weverse.io/"
        driver.get(base_url)
        load_cookies_from_txt(driver, cookie_file)
        driver.refresh()

        # Navigate to the target URL.
        driver.get(target_url)
        wait = WebDriverWait(driver, 30)
        wait.until(EC.presence_of_element_located(
            (By.CSS_SELECTOR, "a.LiveListView_live_item__aX1Ph")))

        # Scroll down until no new content loads.
        last_height = driver.execute_script(
            "return document.body.scrollHeight")
        while True:
            driver.execute_script(
                "window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(scroll_pause_time)
            new_height = driver.execute_script(
                "return document.body.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height

        # Collect all video links.
        video_elements = driver.find_elements(
            By.CSS_SELECTOR, "a.LiveListView_live_item__aX1Ph")
        video_links = []
        counter = 0
        for elem in video_elements:
            href = elem.get_attribute("href")
            if href and href not in video_links:
                video_links.append(href)
                counter += 1
                print(f"Link {counter}: {href}")

        print(f"\nTotal video links found: {counter}")
        return video_links
    finally:
        driver.quit()


def save_links_to_file(links, output_file="video_links.txt"):
    """
    Saves the provided list of links to the specified output file.
    """
    with open(output_file, "w", encoding="utf-8") as f:
        for link in links:
            f.write(link + "\n")
    print(f"\nAll video links have been saved to {output_file}.")


def main():
    if len(sys.argv) != 3:
        print("Usage: python weverse_video_links.py <cookie_file> <target_url>")
        sys.exit(1)

    cookie_file = sys.argv[1]
    target_url = sys.argv[2]

    if not os.path.exists(cookie_file):
        print(f"Cookie file '{cookie_file}' not found.")
        sys.exit(1)

    print(f"Scraping video links from {target_url} ...")
    links = get_video_links(target_url, cookie_file)

    if links:
        print("\nFound video links:")
        for link in links:
            print(link)
        save_links_to_file(links)
    else:
        print("No video links found.")


if __name__ == "__main__":
    main()
