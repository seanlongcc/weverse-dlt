(() => {
  // ---- configuration (adjust if needed) ----
  const THUMB_SELECTOR = '.toggle-switch-_-thumb';
  const LABEL_NEEDLE = 'auto play'; // matches the <span class="blind">auto play</span>

  // ---- helpers ----
  const norm = (s) => (s || '').trim().toLowerCase();

  function findAutoplayThumbs(root = document) {
    const thumbs = Array.from(root.querySelectorAll(THUMB_SELECTOR));
    return thumbs.filter(t => {
      const blind = t.querySelector('.blind');
      return norm(blind?.textContent).includes(norm(LABEL_NEEDLE));
    });
  }

  function getState(thumb) {
    // prefer data-state, fall back to aria-checked if present on parent
    const ds = thumb?.getAttribute('data-state');
    if (ds === 'checked' || ds === 'unchecked') return ds;

    const host = thumb?.closest('[role="switch"], button, [role="button"]');
    const aria = host?.getAttribute?.('aria-checked');
    if (aria === 'true') return 'checked';
    if (aria === 'false') return 'unchecked';

    return null;
  }

  function getClickableHost(thumb) {
    // The "real" click target is usually the switch/button wrapper, not the thumb span itself.
    return thumb.closest('[role="switch"], button, [role="button"], label') || thumb;
  }

  function ensureUncheckedOnce() {
    const thumbs = findAutoplayThumbs();
    if (!thumbs.length) {
      console.warn('[autoplay-toggle] No autoplay toggle thumbs found. You may need to widen selectors.');
      return { found: 0, changed: 0 };
    }

    let changed = 0;
    for (const thumb of thumbs) {
      const state = getState(thumb);
      if (state === 'checked') {
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
      if (getState(thumb) === 'checked') {
        getClickableHost(thumb).click();
        console.debug('[autoplay-toggle] flipped back to unchecked');
      }
    }
  });

  observer.observe(document.documentElement, {
    subtree: true,
    childList: true,
    attributes: true,
    attributeFilter: ['data-state', 'aria-checked', 'class']
  });

  // Expose a stop function
  window.__disableAutoplayToggleStop = () => {
    observer.disconnect();
    console.log('[autoplay-toggle] observer stopped');
  };

  console.log('[autoplay-toggle] Installed. To stop forcing it off: __disableAutoplayToggleStop()');
})();
