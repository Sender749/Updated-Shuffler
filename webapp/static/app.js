(function () {
  "use strict";

  const tg = window.Telegram && window.Telegram.WebApp;
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
  let userHasUnmuted = false; // once they unmute once, keep new reels unmuted too

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
    // Once the user taps to unmute once, we carry that preference forward
    // to reels they scroll to next, same as Instagram/TikTok.
    video.muted = !userHasUnmuted;
    video.preload = "metadata"; // upgraded to "auto" for the front of the feed by updatePreloadWindow
    video.controls = false;

    const spinner = document.createElement("div");
    spinner.className = "reel-spinner";
    wrap.appendChild(spinner);
    const hideSpinner = () => spinner.classList.add("hidden");
    video.addEventListener("canplay", hideSpinner);
    video.addEventListener("playing", hideSpinner);

    if (!userHasUnmuted) {
      const muteHint = document.createElement("div");
      muteHint.className = "mute-hint";
      muteHint.textContent = "🔇 Tap for sound";
      wrap.appendChild(muteHint);
      video.addEventListener("volumechange", () => {
        if (!video.muted) muteHint.remove();
      });
    }

    wrap.addEventListener("click", () => {
      video.muted = !video.muted;
      if (!video.muted) userHasUnmuted = true;
    });

    wrap.appendChild(video);
    observer.observe(wrap);
    return wrap;
  }

  async function loadMore() {
    if (loadingMore || reachedEnd) return;
    loadingMore = true;
    try {
      const url = new URL("/webapp/api/feed", window.location.origin);
      if (nextCursor) url.searchParams.set("after", nextCursor);
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
  // WebApp is enabled, so we skip the extra /api/status round trip that used
  // to happen before the first feed fetch. One less network hop before
  // anything can appear on screen.
  loadMore();
})();
