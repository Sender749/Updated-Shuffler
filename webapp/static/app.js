(function () {
  "use strict";

  const tg = window.Telegram && window.Telegram.WebApp;
  const INIT_DATA = tg ? tg.initData || "" : "";
  if (tg) {
    tg.ready();
    tg.expand();
    try { tg.setHeaderColor("#000000"); tg.setBackgroundColor("#000000"); } catch (e) {}
  }

  function openExternal(url) {
    if (!url) return;
    if (tg && tg.openLink) tg.openLink(url);
    else window.open(url, "_blank");
  }

  const feedEl = document.getElementById("feed");
  const loadingEl = document.getElementById("loading");
  const unavailableEl = document.getElementById("unavailable");
  const unavailableTextEl = document.getElementById("unavailable-text");
  const retryBtn = document.getElementById("retry-btn");
  const verifyOverlayEl = document.getElementById("verify-overlay");
  const verifyBtn = document.getElementById("verify-btn");
  const verifyHowtoBtn = document.getElementById("verify-howto-btn");
  const verifiedToastEl = document.getElementById("verified-toast");
  const fsubOverlayEl = document.getElementById("fsub-overlay");
  const fsubChannelButtonsEl = document.getElementById("fsub-channel-buttons");
  const fsubRecheckBtn = document.getElementById("fsub-recheck-btn");
  const freeCountBadgeEl = document.getElementById("free-count-badge");

  let nextCursor = null;
  let loadingMore = false;
  let reachedEnd = false;
  let verificationGated = false; // true once the feed says we need to verify — stops further loadMore() calls
  let fsubGated = false; // true once the feed says required channels aren't joined yet

  // Single global source of truth for mute, applied to whichever video is
  // ACTUALLY PLAYING right now (see the IntersectionObserver below) — not
  // just baked into each video once when it's created, since reels further
  // down the feed get built (preloaded) before the user unmutes.
  let globallyMuted = true;

  // ── tiny inline icon set (outline by default, filled when active) ─────────
  const ICON_HEART = '<svg viewBox="0 0 24 24" class="icon-svg"><path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z"/></svg>';
  const ICON_SPEAKER_BASE = '<path d="M4 9v6h4l5 5V4L8 9H4z"/>';
  const ICON_SPEAKER_MUTED = `<svg viewBox="0 0 24 24" class="icon-svg">${ICON_SPEAKER_BASE}<line x1="21" y1="4" x2="4" y2="21" class="icon-slash"/></svg>`;
  const ICON_SPEAKER_ON = `<svg viewBox="0 0 24 24" class="icon-svg">${ICON_SPEAKER_BASE}<path d="M15.5 8.5a5 5 0 010 7" class="icon-wave"/><path d="M18.3 5.7a9 9 0 010 12.6" class="icon-wave"/></svg>`;

  function showUnavailable(message) {
    if (message) unavailableTextEl.textContent = message;
    unavailableEl.classList.remove("hidden");
    loadingEl.classList.add("hidden");
  }

  function hideUnavailable() {
    unavailableEl.classList.add("hidden");
  }

  let verifyInfoCache = null;
  async function fetchVerifyInfo() {
    try {
      const url = new URL("/webapp/api/verify-info", window.location.origin);
      if (INIT_DATA) url.searchParams.set("init_data", INIT_DATA);
      const res = await fetch(url);
      if (!res.ok) return null;
      return await res.json();
    } catch (e) {
      return null;
    }
  }

  async function showVerifyOverlay() {
    verificationGated = true;
    loadingEl.classList.add("hidden");
    verifyOverlayEl.classList.remove("hidden");
    verifyInfoCache = await fetchVerifyInfo();
  }

  function hideVerifyOverlay() {
    verifyOverlayEl.classList.add("hidden");
  }

  function showVerifiedToast() {
    verifiedToastEl.classList.remove("hidden");
    setTimeout(() => verifiedToastEl.classList.add("hidden"), 2500);
  }

  function showFsubOverlay(channels) {
    fsubGated = true;
    loadingEl.classList.add("hidden");
    fsubChannelButtonsEl.innerHTML = "";
    (channels || []).forEach((ch) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "popup-btn popup-btn-secondary";
      btn.textContent = `📢 ${ch.title}`;
      btn.addEventListener("click", () => openExternal(ch.invite_link));
      fsubChannelButtonsEl.appendChild(btn);
    });
    fsubOverlayEl.classList.remove("hidden");
  }

  function hideFsubOverlay() {
    fsubOverlayEl.classList.add("hidden");
  }

  fsubRecheckBtn.addEventListener("click", async () => {
    hideFsubOverlay();
    fsubGated = false;
    loadingEl.classList.remove("hidden");
    await loadMore();
  });

  function updateFreeCountBadge(remaining) {
    if (remaining === null || remaining === undefined) {
      freeCountBadgeEl.classList.add("hidden");
      return;
    }
    freeCountBadgeEl.textContent = remaining === 1 ? "1 free reel left" : `${remaining} free reels left`;
    freeCountBadgeEl.classList.remove("hidden");
  }

  verifyBtn.addEventListener("click", async () => {
    if (!verifyInfoCache) verifyInfoCache = await fetchVerifyInfo();
    if (verifyInfoCache && verifyInfoCache.verify_url) openExternal(verifyInfoCache.verify_url);
  });
  verifyHowtoBtn.addEventListener("click", async () => {
    if (!verifyInfoCache) verifyInfoCache = await fetchVerifyInfo();
    if (verifyInfoCache && verifyInfoCache.tutorial_url) openExternal(verifyInfoCache.tutorial_url);
  });

  function updateMuteIcon(wrap, video) {
    const btn = wrap.querySelector(".mute-btn");
    if (!btn) return;
    btn.innerHTML = video.muted ? ICON_SPEAKER_MUTED : ICON_SPEAKER_ON;
    btn.classList.toggle("active", !video.muted);
  }

  function setGlobalMute(muted) {
    globallyMuted = muted;
    feedEl.querySelectorAll(".reel").forEach((wrap) => {
      const video = wrap.querySelector("video");
      if (video) {
        video.muted = globallyMuted;
        updateMuteIcon(wrap, video);
      }
    });
  }

  // Pause is shown PERSISTENTLY (stays until resumed) so it's always clear
  // the user stopped it on purpose. Resume gets the old brief flash instead,
  // since "now playing" is the normal state and doesn't need to stick around.
  function showPauseIndicator(wrap) {
    if (wrap.querySelector(".pause-indicator")) return;
    const indicator = document.createElement("div");
    indicator.className = "pause-indicator";
    indicator.textContent = "❚❚";
    wrap.appendChild(indicator);
  }

  function hidePauseIndicator(wrap) {
    const indicator = wrap.querySelector(".pause-indicator");
    if (indicator) indicator.remove();
  }

  function flashResumeIcon(wrap) {
    let flash = wrap.querySelector(".tap-flash");
    if (!flash) {
      flash = document.createElement("div");
      flash.className = "tap-flash";
      wrap.appendChild(flash);
    }
    flash.textContent = "▶";
    flash.classList.remove("show");
    void flash.offsetWidth; // force reflow so the animation restarts on rapid repeat taps
    flash.classList.add("show");
  }

  // Preload budget: the currently-visible reel plus the next one get
  // preload="auto" (start fetching real video bytes immediately), everything
  // else stays "metadata" so we're not burning bandwidth/R2 requests on
  // reels the user may never scroll to.
  function updatePreloadWindow(currentWrap) {
    const all = Array.from(feedEl.children);
    const idx = all.indexOf(currentWrap);
    all.forEach((wrap, i) => {
      const video = wrap.querySelector("video");
      if (!video) return;
      const shouldEagerLoad = i === idx || i === idx + 1;
      const wanted = shouldEagerLoad ? "auto" : "metadata";
      if (video.preload !== wanted) {
        video.preload = wanted;
        if (shouldEagerLoad && video.readyState === 0) {
          try { video.load(); } catch (e) {}
        }
      }
    });
  }

  // Only one video plays at a time — whichever is most in view. This is also
  // where the current global mute preference gets (re-)applied every time,
  // regardless of when this particular video element was created.
  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      const video = entry.target.querySelector("video");
      if (!video) return;
      if (entry.isIntersecting && entry.intersectionRatio >= 0.6) {
        video.muted = globallyMuted;
        updateMuteIcon(entry.target, video);
        video.play().catch(() => {});
        hidePauseIndicator(entry.target);
        updatePreloadWindow(entry.target);
      } else {
        video.pause();
      }
    });
  }, { threshold: [0, 0.6, 1] });

  async function sendLike(videoId) {
    try {
      const res = await fetch("/webapp/api/like", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ video_id: videoId, init_data: INIT_DATA }),
      });
      if (!res.ok) return null;
      return await res.json();
    } catch (e) {
      return null;
    }
  }

  function buildReel(item) {
    const wrap = document.createElement("div");
    wrap.className = "reel";
    wrap.dataset.id = item.id;

    const video = document.createElement("video");
    video.src = item.url;
    if (item.poster) video.poster = item.poster;
    video.loop = true;
    video.playsInline = true;
    video.muted = globallyMuted; // corrected again on activation regardless — see observer above
    video.preload = "metadata"; // upgraded to "auto" for the front of the feed by updatePreloadWindow
    video.controls = false;

    // Buffering spinner — shown initially AND whenever playback stalls
    // mid-video, so a mid-play hiccup doesn't look like the app froze.
    const spinner = document.createElement("div");
    spinner.className = "reel-spinner";
    spinner.innerHTML = '<div class="reel-spinner-ring"></div><div class="reel-spinner-label">Loading…</div>';
    const hideSpinner = () => spinner.classList.add("hidden");
    const showSpinner = () => spinner.classList.remove("hidden");
    video.addEventListener("canplay", hideSpinner);
    video.addEventListener("playing", hideSpinner);
    video.addEventListener("waiting", showSpinner);
    video.addEventListener("stalled", showSpinner);
    // 'waiting'/'stalled' can fire even during genuinely smooth playback (a
    // looping video seeking back to frame 0, a microsecond network blip) —
    // if that happens to be the LAST event before the next 'canplay' would
    // have fired, the spinner gets stuck forever even though the video is
    // fine. currentTime actually advancing is the one unambiguous signal
    // that playback is really progressing, so also hide on every
    // timeupdate — this self-corrects any stuck spinner within ~250ms.
    video.addEventListener("timeupdate", hideSpinner);

    // ── seek bar: sits a bit clear of the very bottom edge (so it isn't
    // where phone system gestures/screen-protector dead zones live), with a
    // much taller invisible touch target than its visible thin line, and
    // supports press-and-drag to scrub forward/back.
    const seekWrap = document.createElement("div");
    seekWrap.className = "seek-wrap";
    const seekTrack = document.createElement("div");
    seekTrack.className = "progress-track";
    const seekFill = document.createElement("div");
    seekFill.className = "progress-fill";
    seekTrack.appendChild(seekFill);
    seekWrap.appendChild(seekTrack);

    let scrubbing = false;
    let wasPlayingBeforeScrub = false;

    function fractionFromEvent(e) {
      const rect = seekTrack.getBoundingClientRect();
      const x = (e.touches ? e.touches[0].clientX : e.clientX) - rect.left;
      return Math.min(1, Math.max(0, x / rect.width));
    }

    function applyScrub(e) {
      if (!video.duration) return;
      const frac = fractionFromEvent(e);
      seekFill.style.width = `${frac * 100}%`;
      video.currentTime = frac * video.duration;
    }

    seekWrap.addEventListener("pointerdown", (e) => {
      e.stopPropagation();
      scrubbing = true;
      wasPlayingBeforeScrub = !video.paused;
      video.pause();
      seekWrap.setPointerCapture(e.pointerId);
      applyScrub(e);
    });
    seekWrap.addEventListener("pointermove", (e) => {
      if (!scrubbing) return;
      e.stopPropagation();
      applyScrub(e);
    });
    function endScrub(e) {
      if (e) e.stopPropagation();
      if (!scrubbing) return;
      scrubbing = false;
      if (wasPlayingBeforeScrub) video.play().catch(() => {});
    }
    seekWrap.addEventListener("pointerup", endScrub);
    seekWrap.addEventListener("pointercancel", endScrub);
    // Browsers still fire a synthetic 'click' after a pointerdown/pointerup
    // pair on the same element even when we've handled everything above —
    // without stopping it here, that click bubbles up to the reel's
    // tap-to-play/pause handler and accidentally pauses the video every
    // time someone just meant to scrub. This is the actual fix for that.
    seekWrap.addEventListener("click", (e) => e.stopPropagation());

    video.addEventListener("timeupdate", () => {
      if (!scrubbing && video.duration > 0) {
        seekFill.style.width = `${(video.currentTime / video.duration) * 100}%`;
      }
    });

    // ── right-side action buttons — mute toggle + like — transparent
    // background, stacked vertically, mid-right of the screen.
    const actions = document.createElement("div");
    actions.className = "side-actions";

    const muteBtn = document.createElement("button");
    muteBtn.className = "action-btn mute-btn";
    muteBtn.type = "button";
    muteBtn.innerHTML = video.muted ? ICON_SPEAKER_MUTED : ICON_SPEAKER_ON;
    muteBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      setGlobalMute(!globallyMuted);
    });

    const likeBtn = document.createElement("button");
    likeBtn.className = "action-btn like-btn";
    likeBtn.type = "button";
    let liked = !!item.liked;
    let count = item.likes || 0;
    const renderLike = () => {
      likeBtn.innerHTML = ICON_HEART + `<span class="like-count">${count}</span>`;
      likeBtn.classList.toggle("active", liked);
    };
    renderLike();
    likeBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      // Optimistic update so it feels instant; reconciled against the
      // server's real response right after (and reverted on failure).
      const prevLiked = liked, prevCount = count;
      liked = !liked;
      count += liked ? 1 : -1;
      renderLike();
      const result = await sendLike(item.id);
      if (!result) {
        liked = prevLiked;
        count = prevCount;
        renderLike();
        return;
      }
      liked = result.liked;
      count = result.count;
      renderLike();
    });

    actions.appendChild(muteBtn);
    actions.appendChild(likeBtn);

    // Tapping the video body now toggles play/pause (mute has its own
    // dedicated button above).
    wrap.addEventListener("click", () => {
      if (video.paused) {
        video.play().catch(() => {});
        hidePauseIndicator(wrap);
        flashResumeIcon(wrap);
      } else {
        video.pause();
        showPauseIndicator(wrap);
      }
    });

    if (globallyMuted) {
      const muteHint = document.createElement("div");
      muteHint.className = "mute-hint";
      muteHint.textContent = "🔇 Tap the speaker for sound";
      wrap.appendChild(muteHint);
      video.addEventListener("volumechange", () => {
        if (!video.muted) muteHint.remove();
      });
    }

    wrap.appendChild(video);
    wrap.appendChild(spinner);
    wrap.appendChild(actions);
    wrap.appendChild(seekWrap);
    observer.observe(wrap);
    return wrap;
  }

  async function loadMore() {
    if (loadingMore || reachedEnd || verificationGated || fsubGated) return;
    loadingMore = true;
    try {
      const url = new URL("/webapp/api/feed", window.location.origin);
      if (nextCursor) url.searchParams.set("after", nextCursor);
      if (INIT_DATA) url.searchParams.set("init_data", INIT_DATA);
      // Browsers have no default fetch timeout — if the server is slow to
      // wake up (cold start) this could otherwise hang indefinitely with
      // zero feedback. Bounding it means a genuinely stuck server fails
      // into the retry popup within 20s instead of leaving the spinner
      // spinning forever.
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 20000);
      const res = await fetch(url, { signal: controller.signal });
      clearTimeout(timeoutId);
      const data = await res.json().catch(() => ({}));

      if (res.status === 503 || !data.enabled) {
        showUnavailable(data.message);
        reachedEnd = true;
        return;
      }
      if (data.fsub_required) {
        showFsubOverlay(data.channels);
        return;
      }
      if (data.verification_required) {
        updateFreeCountBadge(data.remaining_free);
        await showVerifyOverlay();
        return;
      }
      updateFreeCountBadge(data.remaining_free);

      if (!data.items || data.items.length === 0) {
        loadingEl.classList.add("hidden");
        reachedEnd = true;
        if (!feedEl.children.length) {
          const empty = document.createElement("div");
          empty.className = "reel";
          empty.innerHTML = '<div class="empty-state">No reels yet — check back soon!</div>';
          feedEl.appendChild(empty);
        }
        return;
      }

      const wasEmpty = feedEl.children.length === 0;
      data.items.forEach((item) => feedEl.appendChild(buildReel(item)));
      nextCursor = data.next;
      loadingEl.classList.add("hidden");

      if (wasEmpty) updatePreloadWindow(feedEl.firstElementChild);
    } catch (e) {
      console.error("[reels] feed load failed", e);
      // A network failure on the very first load used to just log quietly
      // and leave the spinner spinning forever with no way out — exactly
      // the "stuck" feeling this is meant to fix. Show the retry popup
      // instead, at least when there's nothing on screen yet.
      if (!feedEl.children.length) {
        showUnavailable("Couldn't load reels — check your connection and try again.");
      }
    } finally {
      loadingMore = false;
    }
  }

  feedEl.addEventListener("scroll", () => {
    const nearBottom = feedEl.scrollTop + feedEl.clientHeight >= feedEl.scrollHeight - window.innerHeight * 1.5;
    if (!nearBottom) return;
    if (fsubGated) {
      fsubOverlayEl.classList.remove("hidden");
      return;
    }
    if (verificationGated) {
      // Cancelling the popup only closes it — it does NOT lift the gate.
      // Trying to reach a new (not-yet-loaded) video re-shows the same
      // popup every time, using the already-fetched verify/tutorial links
      // so this doesn't hit the server again.
      verifyOverlayEl.classList.remove("hidden");
      return;
    }
    loadMore();
  });

  retryBtn.addEventListener("click", async () => {
    hideUnavailable();
    loadingEl.classList.remove("hidden");
    reachedEnd = false;
    nextCursor = null;
    feedEl.innerHTML = "";
    await loadMore();
  });

  // If this WebApp was opened via the ?startapp=verify_<vid> direct link
  // (see webapp_api.py's _verify_info_handler), Telegram hands us that
  // payload here — a genuine Mini App launch, so real initData/identity is
  // available, unlike a plain external link. This is what lets verification
  // complete right inside the app instead of sending the user to the bot's
  // DM chat.
  async function completeVerificationIfLaunchedForIt() {
    const startParam = tg && tg.initDataUnsafe ? tg.initDataUnsafe.start_param : null;
    if (!startParam || !startParam.startsWith("verify_")) return;
    const vid = startParam.slice("verify_".length);
    try {
      const res = await fetch("/webapp/api/verify-complete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ vid, init_data: INIT_DATA }),
      });
      if (res.ok) {
        const data = await res.json();
        if (data.verified) {
          hideVerifyOverlay();
          verificationGated = false;
          showVerifiedToast();
        }
      }
    } catch (e) {
      // Silent failure here is intentional — if the vid is already used,
      // expired, or the request just fails, the user simply falls through
      // to the normal feed load below as if they'd opened the app plainly.
      // If they're genuinely not verified, the usual gate will catch it.
    }
  }

  async function init() {
    await completeVerificationIfLaunchedForIt();
    // Single request from here — the feed endpoint itself reports whether
    // the WebApp is enabled (and whether verification is needed), so
    // there's no separate round trip before anything can appear on screen.
    await loadMore();
  }

  init();
})();
