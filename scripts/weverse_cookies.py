#!/usr/bin/env python3
"""Import the user's Weverse session without writing cookie values to stdout."""

from __future__ import annotations

import argparse
import configparser
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

try:
    from scripts.weverse_session import ACCESS_TOKEN_NAME, parse_cookie_header, weverse_cookie_header
except ModuleNotFoundError:
    from weverse_session import ACCESS_TOKEN_NAME, parse_cookie_header, weverse_cookie_header


class CookieImportError(RuntimeError):
    pass


class QuietLogger:
    def debug(self, *args, **kwargs):
        pass

    info = warning = error = debug

    def progress_bar(self, *args, **kwargs):
        return None


def zen_profile_roots() -> list[Path]:
    home = Path.home()
    if sys.platform == "win32":
        return [Path(os.environ.get("APPDATA", home / "AppData/Roaming")) / "zen"]
    if sys.platform == "darwin":
        return [home / "Library/Application Support/zen"]
    return [
        home / ".zen",
        home / ".var/app/app.zen_browser.zen/.zen",
        home / ".var/app/app.zen_browser.zen/zen",
        home / ".var/app/io.github.zen_browser.zen/.zen",
    ]


def find_zen_profile(profile: str | None = None) -> Path:
    """Resolve Zen's own profile, never fall back to an unrelated Firefox login."""
    if profile:
        explicit = Path(os.path.expandvars(profile)).expanduser()
        if (explicit / "cookies.sqlite").is_file():
            return explicit.resolve()

    candidates: dict[Path, int] = {}

    def consider(path: Path, name: str = "", priority: int = 2) -> None:
        if profile and profile not in (name, path.name):
            return
        if (path / "cookies.sqlite").is_file():
            path = path.resolve()
            candidates[path] = min(priority, candidates.get(path, priority))

    for root in zen_profile_roots():
        config = configparser.ConfigParser(interpolation=None)
        try:
            config.read(root / "profiles.ini", encoding="utf-8-sig")
        except (OSError, configparser.Error, UnicodeError):
            config.clear()
        for section in config.sections():
            values = config[section]
            if section.startswith("Profile") and values.get("Path"):
                path = Path(values["Path"])
                if values.get("IsRelative", "1") == "1":
                    path = root / path
                consider(path, values.get("Name", ""), 1 if values.get("Default") == "1" else 2)
            elif section.startswith("Install") and values.get("Default") and not profile:
                consider(root / values["Default"], priority=0)
        # Also support profiles without a registry, including portable installations.
        for pattern in ("*/cookies.sqlite", "Profiles/*/cookies.sqlite"):
            for database in root.glob(pattern):
                consider(database.parent)

    if not candidates:
        raise CookieImportError(
            "No matching Zen cookie profile was found. Open about:support in Zen, "
            "find Profile Directory, and paste that folder into Browser profile. "
            "Sign in to Weverse in Zen before importing."
        )
    return min(candidates, key=lambda path: (candidates[path], -(path / "cookies.sqlite").stat().st_mtime))


def import_browser_session(browser: str, profile: str | None = None) -> str:
    from yt_dlp.cookies import extract_cookies_from_browser

    try:
        if browser == "zen":
            profile = str(find_zen_profile(profile))
            browser = "firefox"
        jar = extract_cookies_from_browser(browser, profile=profile, logger=QuietLogger())
        header = weverse_cookie_header({
            "domain": cookie.domain, "path": cookie.path, "name": cookie.name,
            "value": cookie.value, "expiry": cookie.expires,
        } for cookie in jar)
    except CookieImportError:
        raise
    except Exception:
        raise CookieImportError(
            "Could not read this browser's cookies. Close the browser and retry, "
            "check the profile, or use Sign in with Chrome. Windows browser encryption "
            "can prevent direct import."
        ) from None
    if ACCESS_TOKEN_NAME not in parse_cookie_header(header):
        raise CookieImportError(
            "No readable Weverse login was found in this profile. Sign in to Weverse "
            "in that browser, select its profile, or use Sign in with Chrome."
        )
    return header


def sign_in_session(profile_dir: Path, timeout: float = 300) -> str:
    from selenium import webdriver
    from selenium.common.exceptions import WebDriverException
    from selenium.webdriver.chrome.service import Service

    options = webdriver.ChromeOptions()
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--window-size=1100,800")
    options.add_argument("--no-first-run")
    options.add_argument("--disable-background-mode")
    options.page_load_strategy = "eager"
    driver = None
    try:
        driver = webdriver.Chrome(options=options, service=Service(log_output=os.devnull))
        driver.set_page_load_timeout(45)
        driver.get("https://weverse.io/")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # Login may open a separate tab. Inspect only Weverse pages.
            for handle in driver.window_handles:
                driver.switch_to.window(handle)
                if urlsplit(driver.current_url).hostname != "weverse.io":
                    continue
                header = weverse_cookie_header(driver.get_cookies())
                if ACCESS_TOKEN_NAME in parse_cookie_header(header):
                    return header
            time.sleep(0.5)
        raise CookieImportError("Sign-in timed out after five minutes. Click Sign in with Chrome to retry.")
    except WebDriverException:
        raise CookieImportError(
            "Chrome sign-in closed or could not start. Check that Chrome is installed "
            "and try again, or paste a cookie under Manual cookie."
        ) from None
    finally:
        if driver is not None:
            try:
                driver.quit()
            except WebDriverException:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("import", "signin"), required=True)
    parser.add_argument("--browser", choices=("zen", "chrome", "edge", "firefox", "brave"), default="zen")
    parser.add_argument("--profile", default=None)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        header = (sign_in_session(args.output.parent / "chrome-profile")
                  if args.mode == "signin" else import_browser_session(args.browser, args.profile))
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(header)
    except CookieImportError as exc:
        print(str(exc))
        return 1
    except Exception:
        print("Session import failed. Check the installed dependencies or use Manual cookie.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
