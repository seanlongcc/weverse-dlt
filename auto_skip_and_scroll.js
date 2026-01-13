// Paste into browser console.
// Seeks to the *very end* of the video, then pauses it (with retries) to defeat autoplay.
// Then scrolls chat to bottom, collects unique {name, message}, downloads JSON.

(async () => {
  // ---- helpers ----
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

  // ---- 1) Seek to very end + pause (retry loop) ----
  // Weverse can re-trigger playback. This tries multiple times and also pauses on play events.
  const video = document.querySelector("video");

  async function seekToVeryEndAndPause(v) {
    if (!v) return false;

    // Ensure we pause whenever the site tries to start playback again
    const onPlay = () => {
      try {
        v.pause();
        // keep it pinned at the end in case it jumps
        if (isFinite(v.duration) && v.duration > 0) {
          v.currentTime = v.duration;
        }
      } catch (_) {}
    };
    v.addEventListener("play", onPlay, true);

    // Best effort: if metadata isn't loaded yet, wait a bit
    for (let i = 0; i < 30; i++) {
      if (isFinite(v.duration) && v.duration > 0) break;
      await sleep(200);
    }

    if (!(isFinite(v.duration) && v.duration > 0)) {
      console.log("No usable video duration found. Continuing without seeking.");
      return false;
    }

    // Try multiple times because some players ignore the first seek while buffering
    for (let attempt = 1; attempt <= 20; attempt++) {
      try {
        // Go to absolute end; some players clamp slightly before end.
        v.currentTime = v.duration;
      } catch (_) {
        // Fallback: near-end
        try {
          v.currentTime = Math.max(0, v.duration - 0.01);
        } catch (_) {}
      }

      // Give it a moment to apply
      await sleep(150);

      // Pause (some sites auto-play immediately after seek)
      try {
        v.pause();
      } catch (_) {}

      // Re-assert end position after pausing
      await sleep(100);
      try {
        v.currentTime = v.duration;
      } catch (_) {}

      // Confirm it's paused; if not, keep trying
      const ok = v.paused === true;
      console.log(
        `Seek/pause attempt ${attempt}: paused=${v.paused}, t=${v.currentTime.toFixed?.(3) ?? v.currentTime}/${v.duration}`
      );

      if (ok) {
        // One more short wait to ensure autoplay doesn't resume
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

  if (video) {
    const did = await seekToVeryEndAndPause(video);
    if (did) console.log("Video forced to end and paused.");
  } else {
    console.log("No <video> tag found. Continuing...");
  }

  // ---- 2) locate chat slot + scroll container ----
  const slot = document.querySelector("#wev-previous-chat-list-slot");
  if (!slot) throw new Error("Chat slot not found: #wev-previous-chat-list-slot");

  const scroller = findScrollable(slot);
  console.log("Using scroller:", scroller);

  // ---- 3) scroll-until-stable + collect ----
  const seen = new Set(); // key = name + \u0000 + message
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

  const maxRounds = 600; // bump a bit to be safer on long chats
  for (let i = 0; i < maxRounds; i++) {
    scroller.scrollTop = scroller.scrollHeight;
    await sleep(500);

    const rowCount = collect();
    const seenSize = seen.size;

    // stable means: row count unchanged AND no new unique messages
    if (rowCount === lastRowCount && seenSize === lastSeenSize) stableRounds++;
    else stableRounds = 0;

    lastRowCount = rowCount;
    lastSeenSize = seenSize;

    if (stableRounds >= 8) break;
  }

  const messages = [...seen].map((k) => {
    const [name, message] = k.split("\u0000");
    return { name, message };
  });

  console.log("Unique messages collected:", messages.length);
  console.log(messages);

  // ---- 4) download JSON ----
  const blob = new Blob([JSON.stringify(messages, null, 2)], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `weverse_chat_${Date.now()}.json`;
  a.click();
  URL.revokeObjectURL(url);

  return messages;
})();