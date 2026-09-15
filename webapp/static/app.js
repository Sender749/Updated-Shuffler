(function () {
  "use strict";

  const tg = window.Telegram && window.Telegram.WebApp;
  const INIT_DATA = tg ? tg.initData || "" : "";
  if (tg) {
    tg.ready();
    tg.expand();
    try { tg.setHeaderColor("#000000"); tg.setBackgroundColor("#000000"); } catch (e) {}
  }

  const feedEl = document.getElementById("feed");
  const loadingEl = document.getElementById("loading");
  const unavailableEl = document.getElementById("unavailable");
  const unavailableTextEl = document.getElementById("unavailable-text");
  const retryBtn = document.getElementById("retry-btn");

  let nextCursor = null;
  let loadingMore = false;
  let reachedEnd = false;
  // Sticky for this session only: once the user taps unmute, every reel they
  // scroll to next plays unmuted too, until they tap mute again. A fresh
  // page load (new session) always starts back at muted — nothing here is
  // persisted to storage on purpose, matching "always muted by default".
  let userHasUnmuted = false;

  function showUnavailable(message) {
    if (message) unavailableTextEl.textContent = message;
    unavailableEl.classList.remove("hidden");
    loadingEl.classList.add("hidden");
  }

  function hideUnavailable() {
    unavailableEl.classList.add("hidden");
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

  // Only one video plays at a time — whichever is most in view.
  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      const video = entry.target.querySelector("video");
      if (!video) return;
      if (entry.isIntersecting && entry.intersectionRatio >= 0.6) {
        video.play().catch(() => {});
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
    // Start muted — this is what guarantees autoplay actually fires
    // instantly across browsers/WebViews without waiting on a user gesture.
    video.muted = !userHasUnmuted;
    video.preload = "metadata"; // upgraded to "auto" for the front of the feed by updatePreloadWindow
    video.controls = false;

    // Buffering spinner — shown initially AND whenever playback stalls
    // mid-video (not just before the first frame), so a mid-play hiccup
    // doesn't look like the app froze.
    const spinner = document.createElement("div");
    spinner.className = "reel-spinner";
    const hideSpinner = () => spinner.classList.add("hidden");
    const showSpinner = () => spinner.classList.remove("hidden");
    video.addEventListener("canplay", hideSpinner);
    video.addEventListener("playing", hideSpinner);
    video.addEventListener("waiting", showSpinner);
    video.addEventListener("stalled", showSpinner);

    // Thin progress line along the bottom — no numbers, just a bar that
    // fills as the video plays, resetting each loop.
    const progressTrack = document.createElement("div");
    progressTrack.className = "progress-track";
    const progressFill = document.createElement("div");
    progressFill.className = "progress-fill";
    progressTrack.appendChild(progressFill);
    video.addEventListener("timeupdate", () => {
      if (video.duration > 0) {
        progressFill.style.width = `${(video.currentTime / video.duration) * 100}%`;
      }
    });

    // Right-side action buttons — mute toggle + like — transparent
    // background, stacked vertically, mid-right of the screen.
    const actions = document.createElement("div");
    actions.className = "side-actions";

    const muteBtn = document.createElement("button");
    muteBtn.className = "action-btn mute-btn";
    muteBtn.type = "button";
    const renderMuteIcon = () => { muteBtn.textContent = video.muted ? "🔇" : "🔊"; };
    renderMuteIcon();
    muteBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      video.muted = !video.muted;
      if (!video.muted) userHasUnmuted = true;
      renderMuteIcon();
    });

    const likeBtn = document.createElement("button");
    likeBtn.className = "action-btn like-btn";
    likeBtn.type = "button";
    let liked = !!item.liked;
    let count = item.likes || 0;
    const renderLike = () => {
      likeBtn.innerHTML =
        `<span class="like-icon${liked ? " liked" : ""}">${liked ? "❤️" : "🤍"}</span>` +
        `<span class="like-count">${count}</span>`;
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

    // Tapping the video body itself still toggles mute too, same as before —
    // the button is there for a clear visual affordance, this keeps the
    // "tap anywhere" habit working alongside it.
    wrap.addEventListener("click", () => {
      video.muted = !video.muted;
      if (!video.muted) userHasUnmuted = true;
      renderMuteIcon();
    });

    if (!userHasUnmuted) {
      const muteHint = document.createElement("div");
      muteHint.className = "mute-hint";
      muteHint.textContent = "🔇 Tap for sound";
      wrap.appendChild(muteHint);
      video.addEventListener("volumechange", () => {
        if (!video.muted) muteHint.remove();
      });
    }

    wrap.appendChild(video);
    wrap.appendChild(spinner);
    wrap.appendChild(actions);
    wrap.appendChild(progressTrack);
    observer.observe(wrap);
    return wrap;
  }

  async function loadMore() {
    if (loadingMore || reachedEnd) return;
    loadingMore = true;
    try {
      const url = new URL("/webapp/api/feed", window.location.origin);
      if (nextCursor) url.searchParams.set("after", nextCursor);
      if (INIT_DATA) url.searchParams.set("init_data", INIT_DATA);
      const res = await fetch(url);
      const data = await res.json().catch(() => ({}));

      if (res.status === 503 || !data.enabled) {
        showUnavailable(data.message);
        reachedEnd = true;
        return;
      }
      loadingEl.classList.add("hidden");

      if (!data.items || data.items.length === 0) {
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

      // First load: immediately start eager-loading the first two reels
      // rather than waiting for a scroll/intersection event to fire.
      if (wasEmpty) updatePreloadWindow(feedEl.firstElementChild);
    } catch (e) {
      // Network hiccup — don't show the "turned off" popup for this, just
      // let the user retry by scrolling again.
      console.error("[reels] feed load failed", e);
    } finally {
      loadingMore = false;
    }
  }

  feedEl.addEventListener("scroll", () => {
    const nearBottom = feedEl.scrollTop + feedEl.clientHeight >= feedEl.scrollHeight - window.innerHeight * 1.5;
    if (nearBottom) loadMore();
  });

  retryBtn.addEventListener("click", async () => {
    hideUnavailable();
    loadingEl.classList.remove("hidden");
    reachedEnd = false;
    nextCursor = null;
    feedEl.innerHTML = "";
    await loadMore();
  });

  // Single request on load — the feed endpoint itself reports whether the
  // WebApp is enabled, so there's no separate /api/status round trip before
  // anything can appear on screen.
  loadMore();
})();
