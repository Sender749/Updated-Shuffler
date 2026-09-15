"""
aiohttp routes for the Reels WebApp: a JSON status endpoint (admin on/off
switch), a paginated feed endpoint, and the static WebApp files themselves.
All registered onto bot.py's existing aiohttp RouteTableDef, so this reuses
the same web server (and same $PORT) that's already running for health
checks — no separate service, no extra Koyeb resources needed.
"""

import os
from aiohttp import web
from Database.maindb import mdb
from vars import R2_PUBLIC_BASE_URL, R2_ENABLED

WEBAPP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webapp")
WEBAPP_ASSETS_DIR = os.path.join(WEBAPP_DIR, "static")
WEBAPP_INDEX_FILE = os.path.join(WEBAPP_DIR, "index.html")

_UNAVAILABLE_MESSAGE = (
    "We're experiencing a temporary issue loading reels. Please check back in a bit!"
)


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

    docs = await mdb.get_reels_feed(after_id=after_id, limit=limit)
    items = [
        {
            "id": str(doc["_id"]),
            "url": f"{R2_PUBLIC_BASE_URL}/{doc['r2_key']}",
            "poster": f"{R2_PUBLIC_BASE_URL}/{doc['poster_key']}" if doc.get("poster_key") else None,
            "duration": doc.get("duration", 0),
        }
        for doc in docs
        if doc.get("r2_key")
    ]
    next_cursor = items[-1]["id"] if items else None
    return web.json_response({"enabled": True, "items": items, "next": next_cursor})


def register_webapp_routes(routes: web.RouteTableDef) -> None:
    """Call once from bot.py before `app.add_routes(routes)`."""
    routes.get("/webapp/api/status")(_status_handler)
    routes.get("/webapp/api/feed")(_feed_handler)
    routes.get("/webapp")(_index_handler)
    routes.get("/webapp/")(_index_handler)
    if os.path.isdir(WEBAPP_ASSETS_DIR):
        routes.static("/webapp/static", WEBAPP_ASSETS_DIR, show_index=False)
