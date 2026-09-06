"""Validation for pasted replay URLs and UTF-8 link files."""

from urllib.parse import urlsplit, urlunsplit


def parse_replay_links(text: str) -> list[str]:
    links = []
    seen = set()
    for token in text.lstrip("\ufeff").split():
        try:
            parsed = urlsplit(token)
            parts = parsed.path.strip("/").split("/")
            valid = (
                parsed.scheme in ("https", "http")
                and parsed.hostname in ("weverse.io", "www.weverse.io")
                and parsed.username is None and parsed.password is None
                and parsed.port in (None, 80, 443)
                and len(parts) == 3 and parts[1] in ("live", "media")
                and bool(parts[0]) and bool(parts[2])
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(f"Not a Weverse replay link: {token[:120]}\nUse https://weverse.io/group/live/replay-id.")
        # Share query parameters and fragments don't identify a different replay.
        canonical = urlunsplit(("https", "weverse.io", parsed.path.rstrip("/"), "", ""))
        if canonical not in seen:
            links.append(canonical)
            seen.add(canonical)
    return links
