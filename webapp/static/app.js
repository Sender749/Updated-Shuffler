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
  let verificationGated = false; 
  let fsubGated = false; 
  let globallyMuted = true;

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
    void flash.offsetWidth;
    flash.classList.add("show");
  }

  function showTapToPlayIndicator(wrap) {
    if (wrap.querySelector(".pause-indicator")) return;
    const indicator = document.createElement("div");
    indicator.className = "pause-indicator tap-to-play-indicator";
    indicator.innerHTML = '<div>▶</div><div class="tap-to-play-label">Tap to play</div>';
    wrap.appendChild(indicator);
  }

  function hideTapToPlayIndicator(wrap) {
    const indicator = wrap.querySelector(".tap-to-play-indicator");
    if (indicator) indicator.remove();
  }

  function attemptAutoplay(wrap, video) {
    const playPromise = video.play();
    if (playPromise && typeof playPromise.then === "function") {
      playPromise
        .then(() => hideTapToPlayIndicator(wrap))
        .catch(() => showTapToPlayIndicator(wrap));
    }
  }

  let currentActiveWrap = null;
  function unlockAutoplayOnFirstGesture() {
    if (currentActiveWrap) {
      const video = currentActiveWrap.querySelector("video");
      if (video && video.paused) attemptAutoplay(currentActiveWrap, video);
    }
  }
  document.addEventListener("pointerdown", unlockAutoplayOnFirstGesture, { once: true, passive: true });
  document.addEventListener("touchstart", unlockAutoplayOnFirstGesture, { once: true, passive: true });

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

  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      const video = entry.target.querySelector("video");
      if (!video) return;
      if (entry.isIntersecting && entry.intersectionRatio >= 0.6) {
        currentActiveWrap = entry.target;
        video.muted = globallyMuted;
        updateMuteIcon(entry.target, video);
        attemptAutoplay(entry.target, video);
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
    video.muted = globallyMuted;
    video.preload = "metadata"; 
    video.controls = false;

    const spinner = document.createElement("div");
    spinner.className = "reel-spinner";
    spinner.innerHTML = '<div class="reel-spinner-ring"></div><div class="reel-spinner-label">Loading…</div>';
    const hideSpinner = () => spinner.classList.add("hidden");
    const showSpinner = () => spinner.classList.remove("hidden");
    video.addEventListener("canplay", hideSpinner);
    video.addEventListener("playing", hideSpinner);
    video.addEventListener("waiting", showSpinner);
    video.addEventListener("stalled", showSpinner);
    video.addEventListener("timeupdate", hideSpinner);
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
    seekWrap.addEventListener("click", (e) => e.stopPropagation());

    video.addEventListener("timeupdate", () => {
      if (!scrubbing && video.duration > 0) {
        seekFill.style.width = `${(video.currentTime / video.duration) * 100}%`;
      }
    });

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


    wrap.addEventListener("click", () => {
      if (video.paused) {
        video.play().catch(() => {});
        hidePauseIndicator(wrap);
        hideTapToPlayIndicator(wrap);
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
    }
  }

  async function init() {
    await completeVerificationIfLaunchedForIt();
    await loadMore();
  }

  init();
})();
