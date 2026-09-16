import os
from typing import List

API_ID = int(os.getenv("API_ID", "29453152"))
API_HASH = os.getenv("API_HASH", "2302adc174dbc954ae5081eda5131166")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://gd3251791_db_user:GDPQbmyXAEFDGpbL@cluster0.6jxsnxc.mongodb.net/?appName=Cluster0")

_channel_env = os.getenv("DATABASE_CHANNEL_ID", "-1002517753823 -1003782705533 -1003764718856 -1003734799227 -1003722959589 -1003889132598 -1003855522098 -1003749645073 -1003366943724")
if " " in _channel_env:
    DATABASE_CHANNEL_ID = [int(ch.strip()) for ch in _channel_env.split() if ch.strip().lstrip("-").isdigit()]
else:
    DATABASE_CHANNEL_ID = int(_channel_env)

# ── Multi-admin support ──────────────────────────────────────────────────────
_admin_env = os.getenv("ADMIN_ID", "6541030917 1052054451").strip()
if " " in _admin_env:
    ADMIN_IDS: List[int] = [int(x) for x in _admin_env.split() if x.strip().lstrip("-").isdigit()]
else:
    ADMIN_IDS: List[int] = [int(_admin_env)]
ADMIN_ID = ADMIN_IDS[0]   # kept for legacy use (LOG messages etc.)
# ─────────────────────────────────────────────────────────────────────────────

PICS = (os.environ.get("PICS", "https://envs.sh/iKu.jpg https://envs.sh/iKE.jpg https://envs.sh/iKe.jpg https://envs.sh/iKi.jpg https://envs.sh/iKb.jpg")).split()
LOG_CHNL = int(os.getenv("LOG_CHNL", "-1002412135872"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "Navex_69")  # Without @
IS_FSUB = os.getenv("FSUB", "True").lower() == "true"
_auth_channel_env = os.environ.get("AUTH_CHANNEL", "-1004330032521").strip()
AUTH_CHANNELS = list(map(int, _auth_channel_env.split())) if _auth_channel_env else []
DELETE_TIMER = int(os.getenv("DELETE_TIMER", "1800"))
PROTECT_CONTENT = os.getenv("PROTECT_CONTENT", "True").lower() == "true"
FREE_LIMIT = int(os.getenv("FREE_LIMIT", "10"))
# Free reel plays before the WebApp shows a verification popup — separate
# counter from the DM FREE_LIMIT above, uses the same verification system
# (same "verified" status/expiry as the bot) once completed.
REELS_FREE_LIMIT = int(os.getenv("REELS_FREE_LIMIT", "5"))
POST_CHANNEL = int(os.getenv("POST_CHANNEL", "-1004330032521"))

# Verification Settings
IS_VERIFY = os.getenv("IS_VERIFY", "True").lower() == "true"
LOG_VR_CHANNEL = int(os.getenv("LOG_VR_CHANNEL", "-1002412135872"))
VERIFY_IMG = os.getenv("VERIFY_IMG", "https://graph.org/file/1669ab9af68eaa62c3ca4.jpg")

# Tutorial links
TUTORIAL = os.getenv("TUTORIAL", "https://t.me/Navexdisscussion/33")
TUTORIAL2 = os.getenv("TUTORIAL2", "https://t.me/Navexdisscussion/33")
TUTORIAL3 = os.getenv("TUTORIAL3", "https://t.me/Navexdisscussion/33")

# Shortener settings
SHORTENER_API = os.getenv("SHORTENER_API", "7ef9ed640db12a292b7c33f43922ded1feef2ddb")
SHORTENER_WEBSITE = os.getenv("SHORTENER_WEBSITE", "instantlinks.co")
SHORTENER_API2 = os.getenv("SHORTENER_API2", "bbe02c66b042f605c13ca910a0981014cf02e381")
SHORTENER_WEBSITE2 = os.getenv("SHORTENER_WEBSITE2", "instantlinks.co")
SHORTENER_API3 = os.getenv("SHORTENER_API3", "1423a167dd9dcfed061d49ca390a3c17aae34d24")
SHORTENER_WEBSITE3 = os.getenv("SHORTENER_WEBSITE3", "instantlinks.co")

# Verification expiry times (in seconds)
TWO_VERIFY_GAP = int(os.getenv("TWO_VERIFY_GAP", "21600"))
THREE_VERIFY_GAP = int(os.getenv("THREE_VERIFY_GAP", "21600"))

# ── Category System ──────────────────────────────────────────────────────────
# Channels NOT listed in any category are used for the "All" (default) category.
# Format:
#      "🔥 Viral":    [-1003782705533],
#      "💎 Premium": [-1009876543210, -1001122334455],
CATEGORIES: dict = {
    "🔥 Viral":    [-1003782705533, -1003366943724],
    "🖼️ Photos":   [-1003764718856],
    "🔞 Teen":     [-1003722959589],
    "👩‍🦳 English":  [-1003889132598],
    "👻 Cartoon":  [-1003855522098],
    "🖼️ Trans ":   [-1003734799227],
    "🥸 Desi":     [-1003749645073],
}
# How many category buttons to show per row (admin-configurable)
CATEGORY_BUTTONS_PER_ROW: int = 2
# Allow premium users to download files (disables protect_content for them)
PREMIUM_CAN_DOWNLOAD: bool = os.getenv("PREMIUM_CAN_DOWNLOAD", "True").lower() == "true"
# ─────────────────────────────────────────────────────────────────────────────

# ── Reels WebApp / Cloudflare R2 ─────────────────────────────────────────────
# All of these are optional — if the R2_* credentials are missing, the whole
# feature quietly disables itself (no WebApp button, no mirroring, no crashes).
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "444fb8381f5432bc3ae6a0123b53e4d4")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "01b8738c4439fd941efcd5657e37bc4d")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "4788cb01f879a15daf0d6d8d8e410394a1b0610c1ea98a74b1358bc79d9f576b")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME", "reels-videos")
R2_JURISDICTION = os.getenv("R2_JURISDICTION", "us").strip().lower()
R2_PUBLIC_BASE_URL = os.getenv("R2_PUBLIC_BASE_URL", "https://pub-babd88c1825d4f4c9bb30bcf13f8aa62.r2.dev").rstrip("/")
R2_ENABLED: bool = bool(R2_ACCOUNT_ID and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_BUCKET_NAME)

# Public URL this bot's own web server (bot.py's aiohttp app) is reachable at,
# e.g. your Koyeb service URL "https://your-app.koyeb.app". Used to build the
# WebApp button and to serve the reels feed API + static WebApp files.
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://comparable-noni-naha-cbe8eb81.koyeb.app/").rstrip("/")

# Only videos at or under this length (seconds) get mirrored to R2 for the
# reels feed — longer videos stay DM-only. This is the main lever for keeping
# R2 storage usage (and therefore cost) predictable.
REELS_MAX_DURATION = int(os.getenv("REELS_MAX_DURATION", "90"))

# Videos taller than this (pixels) or with a higher bitrate than this get
# downscaled/re-encoded before mirroring, so playback doesn't stall on a
# typical phone connection. Videos already under both stay untouched (just
# faststart-remuxed) — no quality loss, no re-encode time wasted.
REELS_MAX_HEIGHT = int(os.getenv("REELS_MAX_HEIGHT", "720"))
REELS_TARGET_BITRATE_KBPS = int(os.getenv("REELS_TARGET_BITRATE_KBPS", "1500"))

# R2's free tier is 10GB storage with zero egress fees. We track our own
# cumulative upload size (see Database/maindb.py) and alert the admins in DM
# once usage crosses this percentage, so they can flip the WebApp off before
# any paid usage is incurred.
R2_FREE_STORAGE_GB = float(os.getenv("R2_FREE_STORAGE_GB", "10"))
R2_ALERT_THRESHOLD_PERCENT = float(os.getenv("R2_ALERT_THRESHOLD_PERCENT", "85"))
# ─────────────────────────────────────────────────────────────────────────────
