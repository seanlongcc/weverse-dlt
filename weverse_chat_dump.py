import argparse
import json
import time
import gzip

from seleniumwire import webdriver  # pip install selenium-wire
from selenium.webdriver.chrome.options import Options


# ---------- cookie loader ----------
def load_cookies_from_txt(driver, cookie_file):
    with open(cookie_file, "r", encoding="utf-8") as f:
        cookie_str = f.read().strip()

    for cookie in cookie_str.split(";"):
        cookie = cookie.strip()
        if not cookie or "=" not in cookie:
            continue
        name, value = cookie.split("=", 1)
        cookie_dict = {"name": name.strip(), "value": value.strip(), "domain": "weverse.io"}
        try:
            driver.add_cookie(cookie_dict)
        except Exception:
            pass


# ---------- response body decode ----------
def decode_body(resp):
    body = resp.body or b""
    enc = ""
    try:
        enc = (resp.headers.get("Content-Encoding") or "").lower()
    except Exception:
        enc = ""

    if not enc:
        return body

    if "gzip" in enc:
        return gzip.decompress(body)

    if "br" in enc:
        import brotli
        return brotli.decompress(body)

    if "zstd" in enc:
        import zstandard as zstd
        d = zstd.ZstdDecompressor()
        return d.decompress(body)

    return body


# ---------- identify chat requests ----------
def is_chat_messages_request(req) -> bool:
    if not req.response:
        return False
    url = req.url or ""
    return "/weverse/wevweb/chat/v1.0/chat-" in url and "/messages" in url


def parse_chat_payload(req):
    raw = decode_body(req.response)
    txt = raw.decode("utf-8", errors="replace")
    return json.loads(txt)


# ---------- seek to end + scroll previous chat panel ----------
DISABLE_AUTOPLAY_JS = r"""
(() => {
  // ---- configuration (adjust if needed) ----
  const THUMB_SELECTOR = ".toggle-switch-_-thumb";
  const LABEL_NEEDLE = "auto play"; // matches the <span class="blind">auto play</span>

  // ---- helpers ----
  const norm = (s) => (s || "").trim().toLowerCase();

  function findAutoplayThumbs(root = document) {
    const thumbs = Array.from(root.querySelectorAll(THUMB_SELECTOR));
    return thumbs.filter((t) => {
      const blind = t.querySelector(".blind");
      return norm(blind?.textContent).includes(norm(LABEL_NEEDLE));
    });
  }

  function getState(thumb) {
    // prefer data-state, fall back to aria-checked if present on parent
    const ds = thumb?.getAttribute("data-state");
    if (ds === "checked" || ds === "unchecked") return ds;

    const host = thumb?.closest('[role="switch"], button, [role="button"]');
    const aria = host?.getAttribute?.("aria-checked");
    if (aria === "true") return "checked";
    if (aria === "false") return "unchecked";

    return null;
  }

  function getClickableHost(thumb) {
    // The "real" click target is usually the switch/button wrapper, not the thumb span itself.
    return thumb.closest('[role="switch"], button, [role="button"], label') || thumb;
  }

  function ensureUncheckedOnce() {
    const thumbs = findAutoplayThumbs();
    if (!thumbs.length) {
      console.warn(
        "[autoplay-toggle] No autoplay toggle thumbs found. You may need to widen selectors."
      );
      return { found: 0, changed: 0 };
    }

    let changed = 0;
    for (const thumb of thumbs) {
      const state = getState(thumb);
      if (state === "checked") {
        const host = getClickableHost(thumb);
        host.click(); // let the app update its own state
        changed++;
      }
    }
    console.log(`[autoplay-toggle] Found ${thumbs.length}. Turned off ${changed}.`);
    return { found: thumbs.length, changed };
  }

  // ---- do it now ----
  ensureUncheckedOnce();

  // ---- keep it off (optional but usually desired) ----
  const observer = new MutationObserver(() => {
    // If the UI/framework flips it back to checked, click it off again.
    const thumbs = findAutoplayThumbs();
    for (const thumb of thumbs) {
      if (getState(thumb) === "checked") {
        getClickableHost(thumb).click();
        console.debug("[autoplay-toggle] flipped back to unchecked");
      }
    }
  });

  observer.observe(document.documentElement, {
    subtree: true,
    childList: true,
    attributes: true,
    attributeFilter: ["data-state", "aria-checked", "class"],
  });

  // Expose a stop function
  window.__disableAutoplayToggleStop = () => {
    observer.disconnect();
    console.log("[autoplay-toggle] observer stopped");
  };

  console.log(
    "[autoplay-toggle] Installed. To stop forcing it off: __disableAutoplayToggleStop()"
  );
})();
"""

SCROLL_PREVIOUS_CHAT_JS = r"""
const done = arguments[0];
(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function findScrollable(el) {
    let cur = el;
    while (cur && cur !== document.body) {
      const cs = getComputedStyle(cur);
      const canScroll =
        (cs.overflowY === "auto" || cs.overflowY === "scroll") &&
        cur.scrollHeight > cur.clientHeight;
      if (canScroll) return cur;
      cur = cur.parentElement;
    }
    return el;
  }

  function rowToObj(r) {
    return {
      name:
        r
          .querySelector(".live-chat-list-item-profile-_-profile_name")
          ?.innerText?.trim() ?? "",
      message:
        r
          .querySelector(".live-chat-list-item-message-_-message_body")
          ?.innerText?.trim() ?? "",
    };
  }

  const video = document.querySelector("video");

  async function seekToVeryEndAndPause(v) {
    if (!v) return false;

    const onPlay = () => {
      try {
        v.pause();
        if (isFinite(v.duration) && v.duration > 0) {
          v.currentTime = v.duration;
        }
      } catch (_) {}
    };
    v.addEventListener("play", onPlay, true);

    for (let i = 0; i < 30; i++) {
      if (isFinite(v.duration) && v.duration > 0) break;
      await sleep(200);
    }

    if (!(isFinite(v.duration) && v.duration > 0)) {
      console.log("No usable video duration found. Continuing without seeking.");
      return false;
    }

    for (let attempt = 1; attempt <= 20; attempt++) {
      try {
        v.currentTime = v.duration;
      } catch (_) {
        try {
          v.currentTime = Math.max(0, v.duration - 0.01);
        } catch (_) {}
      }

      await sleep(150);

      try {
        v.pause();
      } catch (_) {}

      await sleep(100);
      try {
        v.currentTime = v.duration;
      } catch (_) {}

      const ok = v.paused === true;
      console.log(
        `Seek/pause attempt ${attempt}: paused=${v.paused}, t=${v.currentTime.toFixed?.(3) ?? v.currentTime}/${v.duration}`
      );

      if (ok) {
        await sleep(300);
        try {
          v.pause();
          v.currentTime = v.duration;
        } catch (_) {}
        return true;
      }

      await sleep(300);
    }

    console.log("Could not reliably pause video after seeking. Chat script will still run.");
    return false;
  }

  let pausedAtEnd = false;
  if (video) {
    pausedAtEnd = await seekToVeryEndAndPause(video);
    if (pausedAtEnd) console.log("Video forced to end and paused.");
  } else {
    console.log("No <video> tag found. Continuing...");
  }

  const slot = document.querySelector("#wev-previous-chat-list-slot");
  if (!slot) throw new Error("Chat slot not found: #wev-previous-chat-list-slot");

  const scroller = findScrollable(slot);
  console.log("Using scroller:", scroller);

  const seen = new Set();
  let stableRounds = 0;
  let lastRowCount = 0;
  let lastSeenSize = 0;

  const collect = () => {
    const rows = slot.querySelectorAll(".live-chat-list-item-slot-_-container");
    for (const r of rows) {
      const o = rowToObj(r);
      const key = `${o.name}\u0000${o.message}`;
      if (o.name || o.message) seen.add(key);
    }
    return rows.length;
  };

  collect();

  const maxRounds = 600;
  for (let i = 0; i < maxRounds; i++) {
    scroller.scrollTop = scroller.scrollHeight;
    await sleep(500);

    const rowCount = collect();
    const seenSize = seen.size;

    if (rowCount === lastRowCount && seenSize === lastSeenSize) stableRounds++;
    else stableRounds = 0;

    lastRowCount = rowCount;
    lastSeenSize = seenSize;

    if (stableRounds >= 8) break;
  }

  done({
    ok: true,
    pausedAtEnd,
    seenCount: seen.size,
    rowCount: lastRowCount
  });
})().catch((err) => {
  done({ ok: false, error: String(err) });
});
"""


def wait_for_new_chat_request(driver, prev_count: int, timeout_sec: float = 6.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        cur = sum(1 for r in driver.requests if is_chat_messages_request(r))
        if cur > prev_count:
            return True
        time.sleep(0.25)
    return False


def dump_chat(cookie_file: str, target_url: str, out_file: str, headless: bool = True):
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,900")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
    )

    sw_opts = {"verify_ssl": False, "disable_encoding": False}
    driver = webdriver.Chrome(options=options, seleniumwire_options=sw_opts)

    try:
        driver.get("https://weverse.io/")
        load_cookies_from_txt(driver, cookie_file)
        driver.refresh()

        driver.requests.clear()
        driver.get(target_url)

        # Wait for first chat response
        print("Waiting for first chat API response...")
        t0 = time.time()
        while time.time() - t0 < 30:
            if any(is_chat_messages_request(r) for r in driver.requests):
                break
            time.sleep(0.5)

        if not any(is_chat_messages_request(r) for r in driver.requests):
            raise RuntimeError(
                "Did not see any chat messages API responses.\n"
                "Try running with --no-headless and confirm the replay chat is visible."
            )

        try:
            driver.execute_script(DISABLE_AUTOPLAY_JS)
        except Exception as e:
            print(f"Autoplay toggle script error: {e}")

        seen_req_urls = set()
        seen_msgs = set()
        all_msgs = []

        idle_rounds = 0
        max_idle_rounds = 5  # allow more attempts

        print("Scrolling to load older chat pages...")
        while True:
            # 1) harvest any new chat pages we captured since last loop
            new_pages = 0
            for req in driver.requests:
                if not is_chat_messages_request(req):
                    continue
                if req.url in seen_req_urls:
                    continue
                seen_req_urls.add(req.url)

                try:
                    payload = parse_chat_payload(req)
                except Exception as e:
                    print(f"Failed to parse one response: {e}")
                    continue

                data = payload.get("data") or []
                if not data:
                    continue

                new_pages += 1
                for m in data:
                    key = (m.get("messageTime"), m.get("userId"), m.get("content"))
                    if key in seen_msgs:
                        continue
                    seen_msgs.add(key)
                    all_msgs.append(m)

            # 2) decide whether we’re still making progress
            if new_pages == 0:
                idle_rounds += 1
            else:
                idle_rounds = 0

            chat_req_count = sum(1 for r in driver.requests if is_chat_messages_request(r))
            print(f"pages+{new_pages} total_msgs={len(all_msgs)} chat_req_count={chat_req_count} idle={idle_rounds}")

            if idle_rounds >= max_idle_rounds:
                break

            # 3) trigger loading older messages by scrolling the previous chat panel
            prev_count = chat_req_count
            try:
                result = driver.execute_async_script(SCROLL_PREVIOUS_CHAT_JS)
                if isinstance(result, dict) and not result.get("ok", True):
                    print(f"Scroll script error: {result.get('error')}")
            except Exception as e:
                print(f"Scroll script error: {e}")

            # wait for a new network call
            got_new = wait_for_new_chat_request(driver, prev_count, timeout_sec=6.0)

            if not got_new:
                # If scrolling didn’t trigger, try a longer pause; some pages debounce loads
                time.sleep(1.0)

        # sort old -> new
        all_msgs.sort(key=lambda m: m.get("messageTime", 0))
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(all_msgs, f, ensure_ascii=False, indent=2)

        print(f"Saved {len(all_msgs)} messages to {out_file}")

    finally:
        driver.quit()

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cookies", help="Path to cookies.txt")
    ap.add_argument("--url", help="Weverse live/VOD URL")
    ap.add_argument("--out", help="Output JSON path")
    ap.add_argument("cookie_file", nargs="?", help="Cookies txt path (positional fallback)")
    ap.add_argument("target_url", nargs="?", help="Weverse live/VOD URL (positional fallback)")
    ap.add_argument("out_file", nargs="?", help="Output JSON path (positional fallback)")
    ap.add_argument("--no-headless", dest="headless", action="store_false", help="Show browser window")
    ap.set_defaults(headless=True)

    args = ap.parse_args()
    args.cookie_file = args.cookies or args.cookie_file
    args.target_url = args.url or args.target_url
    args.out_file = args.out or args.out_file

    if not args.cookie_file or not args.target_url or not args.out_file:
        ap.error("Missing required inputs. Provide --cookies, --url, --out (or positional equivalents).")

    return args


def main() -> int:
    args = parse_args()
    dump_chat(args.cookie_file, args.target_url, args.out_file, headless=args.headless)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
