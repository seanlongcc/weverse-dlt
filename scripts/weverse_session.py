"""Cookie handling shared by the desktop app and its browser helper."""

from __future__ import annotations

import os
import re
import tempfile
import time
from contextlib import contextmanager
from http.cookiejar import Cookie, MozillaCookieJar
from pathlib import Path
from typing import Iterable, Iterator


ACCESS_TOKEN_NAME = "we2_access_token"


def parse_cookie_header(text: str) -> dict[str, str]:
    text = text.strip().strip("\"'")
    if text.lower().startswith("cookie:"):
        text = text[7:].strip()
    cookies = {}
    for part in text.replace("\r", "").replace("\n", "").split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) and value:
            cookies[name] = value
    return cookies


def weverse_cookie_header(cookies: Iterable[dict], now: float | None = None) -> str:
    """Select only unexpired cookies applicable to the Weverse root page."""
    now = time.time() if now is None else now
    selected = {}
    for cookie in cookies:
        if str(cookie.get("domain", "")).lower().lstrip(".") != "weverse.io":
            continue
        if cookie.get("path", "/") != "/":
            continue
        expiry = cookie.get("expiry")
        if expiry is not None and 0 <= float(expiry) <= now:
            continue
        name, value = cookie.get("name", ""), cookie.get("value", "")
        if name and value and not any(c in str(name) + str(value) for c in "\r\n\t;"):
            selected[str(name)] = str(value)
    return "; ".join(f"{name}={value}" for name, value in selected.items())


def redact_cookies(message: str, cookie_text: str) -> str:
    """Remove credential values from tool output before it reaches the UI."""
    cookies = parse_cookie_header(cookie_text)
    for name, value in sorted(cookies.items(), key=lambda item: len(item[1]), reverse=True):
        message = message.replace(f"{name}={value}", f"{name}=[redacted]")
        # Short locale/consent values such as "en" or "1" also occur in normal logs.
        if len(value) >= 8 or re.search(r"token|auth|session|password", name, re.I):
            message = message.replace(value, "[redacted]")
    return message


@contextmanager
def cookie_file(cookie_text: str, *, netscape: bool = False) -> Iterator[Path]:
    """Create a private, short-lived file. Never put credentials in argv."""
    fd, name = tempfile.mkstemp(prefix="weverse-session-", suffix=".txt")
    path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            if not netscape:
                handle.write(cookie_text + "\n")
        if netscape:
            jar = MozillaCookieJar(str(path))
            for key, value in parse_cookie_header(cookie_text).items():
                jar.set_cookie(Cookie(
                    version=0, name=key, value=value, port=None, port_specified=False,
                    domain=".weverse.io", domain_specified=True, domain_initial_dot=True,
                    path="/", path_specified=True, secure=True, expires=None,
                    discard=True, comment=None, comment_url=None, rest={}, rfc2109=False,
                ))
            jar.save(ignore_discard=True, ignore_expires=True)
        yield path
    finally:
        path.unlink(missing_ok=True)
