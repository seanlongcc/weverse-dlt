#!/usr/bin/env python3
"""Download complete replay chat by following the API's pagination cursors."""

from __future__ import annotations

import argparse
import json
import sys
from http.cookiejar import Cookie
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

try:
    from .weverse_cookies import QuietLogger
    from .weverse_queue import parse_replay_links
    from .weverse_session import parse_cookie_header
except ImportError:
    from weverse_cookies import QuietLogger
    from weverse_queue import parse_replay_links
    from weverse_session import parse_cookie_header


class ChatCollectionError(RuntimeError):
    """The collector cannot confirm that it has reached the end of history."""


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")


def log(message: str) -> None:
    print(str(message), flush=True)


def collect_chat_pages(fetch_page, logger=log) -> list[dict]:
    """Follow every cursor, deduplicating overlap without treating a stall as EOF."""
    params = {"limit": 50}
    seen_cursors = set()
    messages = {}
    page_number = 0
    while True:
        cursor = urlencode(sorted(params.items()))
        if cursor in seen_cursors:
            raise ChatCollectionError("Chat pagination repeated a cursor; collection is incomplete.")
        seen_cursors.add(cursor)
        page_number += 1
        try:
            payload = fetch_page(params)
        except Exception as exc:
            # Do not include signed URLs, request headers or server bodies in logs.
            raise ChatCollectionError(
                f"Chat page {page_number} could not be downloaded ({type(exc).__name__}). "
                "Collection is incomplete; reconnect your Weverse session and retry."
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ChatCollectionError(f"Chat page {page_number} has an unexpected format; collection is incomplete.")
        paging = payload.get("paging")
        if not isinstance(paging, dict):
            raise ChatCollectionError(f"Chat page {page_number} has no pagination status; collection is incomplete.")
        for message in payload["data"]:
            if not isinstance(message, dict):
                raise ChatCollectionError("Chat response contains an invalid message; collection is incomplete.")
            try:
                timestamp = int(message["messageTime"])
                key = (timestamp, message.get("userId"), message.get("content"))
                messages.setdefault(key, message)
            except (KeyError, TypeError, ValueError) as exc:
                raise ChatCollectionError("Chat response contains an invalid timestamp or message; collection is incomplete.") from exc
        logger(f"Chat page {page_number}: {len(payload['data'])} messages, {len(messages)} unique total")
        next_params = paging.get("nextParams")
        if next_params is None or next_params == {}:
            break
        if not isinstance(next_params, dict) or any(
            not isinstance(key, str) or not isinstance(value, (str, int))
            for key, value in next_params.items()
        ):
            raise ChatCollectionError("Chat response contains an invalid cursor; collection is incomplete.")
        params = {"limit": 50, **next_params}
    return sorted(messages.values(), key=lambda message: int(message["messageTime"]))


def fetch_replay_chat(api, post_id: str, logger=log) -> list[dict]:
    try:
        post = api._call_post_api(post_id)
        channel_id = post["extension"]["mediaInfo"]["chat"]["chatId"]
    except Exception as exc:
        raise ChatCollectionError(
            "Could not resolve the replay chat channel. Reconnect your Weverse session "
            "and update yt-dlp before retrying."
        ) from exc
    if not isinstance(channel_id, str) or not channel_id:
        raise ChatCollectionError("This replay has no chat channel.")
    endpoint = f"/chat/v1.0/chat-{quote(channel_id, safe='')}/messages"
    return collect_chat_pages(
        lambda params: api._call_api(f"{endpoint}?{urlencode(params)}", post_id,
                                     note="Downloading replay chat page"),
        logger=logger,
    )


def dump_chat(cookie_file: str, target_url: str, out_file: str, headless: bool = True):
    # Keep the headless argument for callers of the previous browser collector.
    # yt-dlp already handles Weverse request signatures and session refresh.
    from yt_dlp import YoutubeDL
    from yt_dlp.extractor.weverse import WeverseIE

    links = parse_replay_links(target_url)
    if len(links) != 1:
        raise ValueError("Provide exactly one Weverse replay URL.")
    post_id = urlsplit(links[0]).path.rsplit("/", 1)[-1]
    cookie_text = Path(cookie_file).read_text(encoding="utf-8-sig")
    with YoutubeDL({"logger": QuietLogger(), "socket_timeout": 30}) as ydl:
        # Keep credentials in memory; cancellation cannot leave a second cookie
        # file behind. The UI owns and cleans up the input cookie file.
        for name, value in parse_cookie_header(cookie_text).items():
            ydl.cookiejar.set_cookie(Cookie(
                version=0, name=name, value=value, port=None, port_specified=False,
                domain=".weverse.io", domain_specified=True, domain_initial_dot=True,
                path="/", path_specified=True, secure=True, expires=None,
                discard=True, comment=None, comment_url=None, rest={}, rfc2109=False,
            ))
        api = WeverseIE(ydl)
        try:
            api.initialize()
        except Exception as exc:
            raise ChatCollectionError("Could not initialize the Weverse session. Reconnect and retry.") from exc
        log("Downloading replay chat until the API reports the end of history...")
        messages = fetch_replay_chat(api, post_id)
    # Only publish the requested output after pagination completes. A failed
    # fetch must not overwrite an earlier complete dump or look successful.
    Path(out_file).write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
    span = (int(messages[-1]["messageTime"]) - int(messages[0]["messageTime"])) / 1000 if messages else 0
    log(f"Saved {len(messages)} messages spanning {span:.1f} seconds to {out_file} (pagination complete)")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cookies", help="Path to cookies.txt")
    ap.add_argument("--url", help="Weverse live/VOD URL")
    ap.add_argument("--out", help="Output JSON path")
    ap.add_argument("cookie_file", nargs="?", help="Cookies txt path (positional fallback)")
    ap.add_argument("target_url", nargs="?", help="Weverse live/VOD URL (positional fallback)")
    ap.add_argument("out_file", nargs="?", help="Output JSON path (positional fallback)")
    ap.add_argument("--no-headless", dest="headless", action="store_false",
                    help="Accepted for compatibility; chat collection no longer launches a browser")
    ap.set_defaults(headless=True)
    args = ap.parse_args()
    args.cookie_file = args.cookies or args.cookie_file
    args.target_url = args.url or args.target_url
    args.out_file = args.out or args.out_file
    if not args.cookie_file or not args.target_url or not args.out_file:
        ap.error("Missing required inputs. Provide --cookies, --url, --out (or positional equivalents).")
    return args


def main() -> int:
    configure_stdio()
    args = parse_args()
    try:
        dump_chat(args.cookie_file, args.target_url, args.out_file, headless=args.headless)
    except ChatCollectionError as exc:
        log(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
