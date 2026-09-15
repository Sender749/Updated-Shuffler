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

  function showUnavailable(message) {
    if (message) unavailableTextEl.textContent = message;
    unavailableEl.classList.remove("hidden");
    loadingEl.classList.add("hidden");
  }

  function hideUnavailable() {
    unavailableEl.classList.add("hidden");
  }

  // Only one video plays at a time — whichever is most in view.
  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      const video = entry.target.querySelector("video");
      if (!video) return;
      if (entry.isIntersecting && entry.intersectionRatio >= 0.6) {
        video.play().catch(() => {});
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
    video.loop = true;
    video.playsInline = true;
    video.muted = false;
    video.preload = "metadata";
    video.controls = false;

    // Tap to mute/unmute rather than pause — keeps the "always playing"
    // reels feel, matching TikTok/Instagram behavior.
    wrap.addEventListener("click", () => { video.muted = !video.muted; });

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
      if (res.status === 503) {
        const data = await res.json().catch(() => ({}));
        showUnavailable(data.message);
        reachedEnd = true;
        return;
      }
      const data = await res.json();
      if (!data.enabled) {
        showUnavailable(data.message);
        reachedEnd = true;
        return;
      }
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
      data.items.forEach((item) => feedEl.appendChild(buildReel(item)));
      nextCursor = data.next;
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
    await init();
  });

  async function init() {
    try {
      const res = await fetch("/webapp/api/status");
      const data = await res.json();
      if (!data.enabled) {
        showUnavailable(data.message);
        return;
      }
      loadingEl.classList.add("hidden");
      await loadMore();
    } catch (e) {
      showUnavailable("Couldn't connect. Please check your connection and try again.");
    }
  }

  init();
})();
