import hashlib
import hmac
import json
import os
import urllib.parse

from aiohttp import web
from Database.maindb import mdb
from vars import R2_ACCOUNTS_BY_ID, R2_ACCOUNTS, R2_ENABLED, BOT_TOKEN

WEBAPP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webapp")
WEBAPP_ASSETS_DIR = os.path.join(WEBAPP_DIR, "static")
WEBAPP_INDEX_FILE = os.path.join(WEBAPP_DIR, "index.html")

_UNAVAILABLE_MESSAGE = (
    "We're experiencing a temporary issue loading reels. Please check back in a bit!"
)


def _verify_init_data(init_data: str):
    """
    Validate Telegram WebApp initData per Telegram's documented algorithm
    (https://core.telegram.org/bots/webapps#validating-data-received-via-the-web-app)
    and return the authenticated user dict, or None if missing/invalid.

    This matters for likes specifically: without verifying the HMAC, anyone
    could send a fake user id in a request and inflate/fake likes for any
    video. The check just confirms "this really came from Telegram, for this
    bot, for this user" — nothing here is sensitive to expose client-side.
    """
    if not init_data or not BOT_TOKEN:
        return None
    try:
        pairs = dict(urllib.parse.parse_qsl(init_data, strict_parsing=True))
        received_hash = pairs.pop("hash", None)
        if not received_hash:
            return None
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed_hash, received_hash):
            return None
        user_raw = pairs.get("user")
        if not user_raw:
            return None
        return json.loads(user_raw)
    except Exception:
        return None


async def _index_handler(request: web.Request) -> web.Response:
    if not os.path.isfile(WEBAPP_INDEX_FILE):
        return web.Response(text="Reels WebApp not installed.", status=404)
    return web.FileResponse(WEBAPP_INDEX_FILE)


async def _status_handler(request: web.Request) -> web.Response:
    settings = await mdb.get_bot_settings()
    return web.json_response({
        "enabled": bool(settings.get("webapp_enabled", True)) and R2_ENABLED,
        "message": None if settings.get("webapp_enabled", True) else _UNAVAILABLE_MESSAGE,
    })


async def _feed_handler(request: web.Request) -> web.Response:
    settings = await mdb.get_bot_settings()
    if not settings.get("webapp_enabled", True) or not R2_ENABLED:
        return web.json_response(
            {"enabled": False, "message": _UNAVAILABLE_MESSAGE, "items": []},
            status=503,
        )

    after_id = request.query.get("after")
    try:
        limit = min(max(int(request.query.get("limit", 10)), 1), 25)
    except ValueError:
        limit = 10

    user = _verify_init_data(request.query.get("init_data", ""))
    user_id = user.get("id") if user else None

    if settings.get("is_verify", True):
        # Same verification system as the bot's DM flow (same "verified"
        # status/expiry) — just with its own separate free-play counter.
        if not user_id:
            # Can't safely count/gate someone we can't identify, and they
            # couldn't complete verification without a real Telegram
            # account anyway — treat as gated rather than letting it bypass.
            return web.json_response(
                {"enabled": True, "verification_required": True, "items": [], "next": None}
            )
        from Database.userdb import udb
        verified = await udb.is_user_verified(user_id)
        if not verified:
            usage = await mdb.check_and_increment_reels_usage(user_id, limit)
            if usage["serve"] <= 0:
                return web.json_response(
                    {"enabled": True, "verification_required": True, "items": [], "next": None}
                )
            limit = usage["serve"]  # cap this batch to whatever's left of today's free quota

    if user_id:
        # Verified Telegram user — server tracks what they've already been
        # shown, so every call (including a brand-new WebApp session) gives
        # them videos they haven't seen yet, in a fresh random order, until
        # the whole pool has been shown at least once.
        docs = await mdb.get_fresh_reels_for_user(user_id, limit=limit)
    else:
        # No verified user (e.g. opened outside Telegram) — can't remember
        # what they've seen, so fall back to plain newest-first pagination.
        docs = await mdb.get_reels_feed(after_id=after_id, limit=limit)
    doc_ids = [str(doc["_id"]) for doc in docs if doc.get("r2_key")]
    liked_ids = await mdb.get_liked_video_ids(user_id, doc_ids) if user_id else set()

    def _account_for(doc):
        acct = R2_ACCOUNTS_BY_ID.get(doc.get("r2_account"))
        if acct:
            return acct
        # Legacy doc mirrored before multi-account support existed — it was
        # always account 1 back then, so that's the correct fallback.
        return R2_ACCOUNTS[0] if R2_ACCOUNTS else None

    items = []
    for doc in docs:
        if not doc.get("r2_key"):
            continue
        account = _account_for(doc)
        if not account:
            continue
        base = account["public_base_url"]
        items.append({
            "id": str(doc["_id"]),
            "url": f"{base}/{doc['r2_key']}",
            "poster": f"{base}/{doc['poster_key']}" if doc.get("poster_key") else None,
            "duration": doc.get("duration", 0),
            "likes": doc.get("likes_count", 0),
            "liked": str(doc["_id"]) in liked_ids,
        })
    next_cursor = items[-1]["id"] if items else None
    return web.json_response({"enabled": True, "items": items, "next": next_cursor})


async def _like_handler(request: web.Request) -> web.Response:
    settings = await mdb.get_bot_settings()
    if not settings.get("webapp_enabled", True) or not R2_ENABLED:
        return web.json_response({"error": "unavailable"}, status=503)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad_request"}, status=400)

    video_id = body.get("video_id")
    user = _verify_init_data(body.get("init_data", ""))
    if not video_id or not user:
        # No verified Telegram user = we can't safely attribute a like to
        # anyone, so we just decline rather than guessing/allowing spoofed IDs.
        return web.json_response({"error": "auth_required"}, status=401)

    try:
        result = await mdb.toggle_like(video_id, user["id"])
    except Exception:
        return web.json_response({"error": "invalid_video"}, status=400)

    return web.json_response(result)


async def _verify_info_handler(request: web.Request) -> web.Response:
    """Called by the WebApp once it hits the verification gate — generates a
    fresh shortlink (same shortener/verify-id system as the bot's DM flow)
    and hands back the tutorial link too, for the Verify / How to verify
    buttons in the popup.

    Uses a ?startapp= direct link rather than the classic ?start= one — that
    launches the Mini App itself (full Telegram.WebApp context, real
    initData) instead of landing the user in the bot's DM chat. This is what
    lets the WebApp complete verification and show a toast right there
    instead of sending them back to the bot. Requires the bot to have a Main
    Mini App configured via @BotFather (the "Create Direct Link" step) — if
    that hasn't been set up, this link just won't open the app directly.
    """
    user = _verify_init_data(request.query.get("init_data", ""))
    if not user:
        return web.json_response({"error": "auth_required"}, status=401)

    import random
    import string
    from Database.userdb import udb
    from TechifyBots.utils import get_shortlink
    from vars import TUTORIAL

    user_id = user["id"]
    vid = "".join(random.choices(string.ascii_uppercase + string.digits, k=7))

    from bot import bot  # safe here — only ever called at request time, well after bot.py finishes loading
    bot_info = await bot.get_me()
    deep_link = f"https://t.me/{bot_info.username}?startapp=verify_{vid}"

    await udb.create_verify_id(user_id, vid)
    short = await get_shortlink(deep_link, False, False)  # reels always uses the first-tier shortener

    return web.json_response({"verify_url": short, "tutorial_url": TUTORIAL})


async def _verify_complete_handler(request: web.Request) -> web.Response:
    """Called by the WebApp itself right after it's launched via the
    ?startapp=verify_<vid> link above — completes verification using the
    SAME logic as the bot-DM flow (Database.userdb.complete_verification),
    just triggered from inside the Mini App instead of a bot message."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad_request"}, status=400)

    vid = body.get("vid")
    user = _verify_init_data(body.get("init_data", ""))
    if not vid or not user:
        return web.json_response({"error": "auth_required"}, status=401)

    from Database.userdb import udb
    result = await udb.complete_verification(user["id"], vid)
    if not result:
        return web.json_response({"error": "invalid_or_expired"}, status=400)

    return web.json_response({"verified": True, "tier": result["tier"]})


def register_webapp_routes(routes: web.RouteTableDef) -> None:
    """Call once from bot.py before `app.add_routes(routes)`."""
    routes.get("/webapp/api/status")(_status_handler)
    routes.get("/webapp/api/feed")(_feed_handler)
    routes.post("/webapp/api/like")(_like_handler)
    routes.get("/webapp/api/verify-info")(_verify_info_handler)
    routes.post("/webapp/api/verify-complete")(_verify_complete_handler)
    routes.get("/webapp")(_index_handler)
    routes.get("/webapp/")(_index_handler)
    if os.path.isdir(WEBAPP_ASSETS_DIR):
        routes.static("/webapp/static", WEBAPP_ASSETS_DIR, show_index=False)
